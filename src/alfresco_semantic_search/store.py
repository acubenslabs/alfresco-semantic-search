"""pgvector store for document chunks.

The schema is deliberately short on purpose. Note what is **absent**:

    node_id, site, path, name, mime_type, modified_at, chunk_index, text, embedding

No ``READER``. No ``DENIED``. No authorities, groups or ACL of any kind.

That is the central design decision of this project. Copying Alfresco's
resolved permission list into your own index is possible — the field is right
there in Alfresco's search index — and it fails open: when someone revokes
access in the repository, your copy keeps serving the content until the next
sync. What you do not store cannot drift out of sync.

Permissions are resolved by Alfresco, as the end user, at query time. See
:meth:`alfresco_semantic_search.alfresco.AlfrescoClient.readable_ids`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    node_id      TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    site         TEXT,
    path         TEXT,
    mime_type    TEXT,
    modified_at  TIMESTAMPTZ NOT NULL,
    indexed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
    id          BIGSERIAL PRIMARY KEY,
    node_id     TEXT NOT NULL REFERENCES documents(node_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text        TEXT NOT NULL,
    embedding   vector(%(dims)s) NOT NULL,
    UNIQUE (node_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS documents_site_idx ON documents (site);
CREATE INDEX IF NOT EXISTS documents_modified_idx ON documents (modified_at);
"""


@dataclass(frozen=True, slots=True)
class Hit:
    """One retrieved chunk, before permission filtering."""

    node_id: str
    chunk_index: int
    text: str
    score: float
    name: str
    site: str | None
    path: str | None


class ChunkStore:
    """pgvector-backed chunk store.

    Takes an open DB-API connection so the caller owns pooling and
    transactions. Tested with ``psycopg`` (v3).
    """

    def __init__(self, connection, dims: int = 1536) -> None:
        self.conn = connection
        self.dims = dims

    def rollback(self) -> None:
        """Abort the current transaction.

        PostgreSQL refuses every statement after an error until the
        transaction ends, so a handler that swallows a database exception
        without rolling back poisons the connection for everything that
        follows. The indexer calls this on failure.
        """
        try:
            self.conn.rollback()
        except Exception:
            log.exception("rollback failed; the connection may be unusable")

    def init_schema(self) -> None:
        """Create the extension, tables and the HNSW index if absent."""
        with self.conn.cursor() as cur:
            cur.execute(SCHEMA % {"dims": self.dims})
        self.conn.commit()
        log.info("schema ready (vector dims=%d)", self.dims)

    # ----------------------------------------------------------------- writes
    def upsert_document(
        self,
        node_id: str,
        name: str,
        site: str | None,
        path: str | None,
        mime_type: str | None,
        modified_at: str,
    ) -> None:
        """Insert or update a document row, leaving its chunks untouched."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents (node_id, name, site, path, mime_type, modified_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (node_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    site = EXCLUDED.site,
                    path = EXCLUDED.path,
                    mime_type = EXCLUDED.mime_type,
                    modified_at = EXCLUDED.modified_at,
                    indexed_at = now()
                """,
                (node_id, name, site, path, mime_type, modified_at),
            )
        self.conn.commit()

    def replace_chunks(
        self, node_id: str, chunks: Sequence[str], embeddings: Sequence[Sequence[float]]
    ) -> int:
        """Replace all chunks of a document atomically.

        Delete-then-insert in one transaction, so a reader never sees a
        partially re-indexed document.
        """
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings must have the same length")

        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM chunks WHERE node_id = %s", (node_id,))
            for index, (text, vector) in enumerate(zip(chunks, embeddings, strict=True)):
                cur.execute(
                    "INSERT INTO chunks (node_id, chunk_index, text, embedding)"
                    " VALUES (%s, %s, %s, %s)",
                    (node_id, index, text, _as_pgvector(vector)),
                )
        self.conn.commit()
        log.debug("stored %d chunks for %s", len(chunks), node_id)
        return len(chunks)

    def update_location(
        self, node_id: str, site: str | None, path: str | None, name: str | None = None
    ) -> bool:
        """Update where a document lives, without touching its embeddings.

        This is what a ``MOVED`` or ``RENAMED`` event triggers: the content did
        not change, so re-embedding would be wasted spend.
        """
        with self.conn.cursor() as cur:
            if name is None:
                cur.execute(
                    "UPDATE documents SET site = %s, path = %s, indexed_at = now()"
                    " WHERE node_id = %s",
                    (site, path, node_id),
                )
            else:
                cur.execute(
                    "UPDATE documents SET site = %s, path = %s, name = %s,"
                    " indexed_at = now() WHERE node_id = %s",
                    (site, path, name, node_id),
                )
            return cur.rowcount > 0

    def delete_document(self, node_id: str) -> bool:
        """Remove a document and its chunks (cascade)."""
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM documents WHERE node_id = %s", (node_id,))
            deleted = cur.rowcount > 0
        self.conn.commit()
        return deleted

    # ------------------------------------------------------------------ reads
    def search(
        self,
        embedding: Sequence[float],
        limit: int = 50,
        site: str | None = None,
    ) -> list[Hit]:
        """Pure vector search. **Applies no permission filtering.**

        Callers must pass the results through
        :func:`alfresco_semantic_search.retrieval.permission_filter` before putting
        any of this text in a prompt. The deliberate split keeps it obvious
        where enforcement happens: here, nowhere; there, Alfresco.

        Over-fetch — ask for 5x what you need — because filtering happens
        after ranking.
        """
        clause = "WHERE d.site = %(site)s" if site else ""
        query = f"""
            SELECT c.node_id, c.chunk_index, c.text,
                   1 - (c.embedding <=> %(vec)s) AS score,
                   d.name, d.site, d.path
              FROM chunks c
              JOIN documents d ON d.node_id = c.node_id
              {clause}
             ORDER BY c.embedding <=> %(vec)s
             LIMIT %(limit)s
        """
        params = {"vec": _as_pgvector(embedding), "limit": limit, "site": site}
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return [
                Hit(node_id=r[0], chunk_index=r[1], text=r[2], score=float(r[3]),
                    name=r[4], site=r[5], path=r[6])
                for r in cur.fetchall()
            ]

    def known_documents(self) -> dict[str, str]:
        """Map of ``node_id`` to stored ``modified_at``, for reconciliation."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT node_id, modified_at FROM documents")
            return {r[0]: r[1].isoformat() for r in cur.fetchall()}

    def stats(self) -> dict[str, int]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM documents")
            documents = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM chunks")
            chunks = cur.fetchone()[0]
        return {"documents": documents, "chunks": chunks}


def _as_pgvector(vector: Sequence[float]) -> str:
    """pgvector accepts its literal form as a string."""
    return "[" + ",".join(f"{v:.8g}" for v in vector) + "]"
