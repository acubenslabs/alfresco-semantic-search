"""The indexing pipeline: repository events in, chunks and embeddings out.

This is where the event classification earns its keep. A naive indexer
re-embeds on every ``node.Updated``, which means paying an embedding bill
every time somebody renames a file or edits a title. The classifier in
:mod:`alfresco_semantic_search.events` tells content changes apart from moves,
renames and metadata edits, so only the first re-embeds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .alfresco import AlfrescoClient, AlfrescoError
from .chunking import chunk_text
from .embeddings import Embedder
from .events import Change, NodeEvent
from .extract import ExtractionError, TransformClient
from .store import ChunkStore

log = logging.getLogger(__name__)

#: Only these node types carry content worth indexing. Alfresco emits events
#: for thumbnails, renditions and system nodes too.
INDEXABLE_TYPES = frozenset({"cm:content"})


@dataclass
class IndexerStats:
    """Running totals, useful to expose as metrics."""

    embedded: int = 0
    relocated: int = 0
    metadata_updated: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    chunks_written: int = 0
    _reasons: dict[str, int] = field(default_factory=dict)

    def note_skip(self, reason: str) -> None:
        self.skipped += 1
        self._reasons[reason] = self._reasons.get(reason, 0) + 1

    @property
    def skip_reasons(self) -> dict[str, int]:
        return dict(self._reasons)


class Indexer:
    """Applies repository events to the vector store."""

    def __init__(
        self,
        alfresco: AlfrescoClient,
        transform: TransformClient,
        store: ChunkStore,
        embedder: Embedder,
        max_chars: int = 1200,
        overlap_chars: int = 150,
    ) -> None:
        self.alfresco = alfresco
        self.transform = transform
        self.store = store
        self.embedder = embedder
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars
        self.stats = IndexerStats()

        if embedder.dims != store.dims:
            raise ValueError(
                f"embedder produces {embedder.dims} dimensions but the store "
                f"expects {store.dims}; they must match"
            )

    def handle(self, event: NodeEvent) -> None:
        """Route one classified event to the right action."""
        if event.change is Change.IGNORE:
            self.stats.note_skip("ignored-event")
            return

        try:
            match event.change:
                case Change.CREATED | Change.CONTENT:
                    self._index(event)
                case Change.MOVED | Change.RENAMED:
                    self._relocate(event)
                case Change.METADATA:
                    self._refresh_metadata(event)
                case Change.DELETED:
                    self._delete(event)
                case Change.WORKFLOW:
                    # A document entered or left a workflow package. Nothing
                    # to do by default — see the note in the README about
                    # using this as an indexing gate.
                    self.stats.note_skip("workflow-association")
                case _:
                    self.stats.note_skip("unhandled-change")
        except AlfrescoError as exc:
            # 404 is expected: the node may have been deleted between the
            # event and our fetch.
            self.store.rollback()
            if exc.status == 404:
                self.stats.note_skip("node-gone")
            else:
                log.warning("repository error on %s: %s", event.node_id, exc)
                self.stats.failed += 1
        except Exception:
            # Roll back before moving on, or every later event fails with
            # InFailedSqlTransaction instead of its own error.
            self.store.rollback()
            log.exception("failed to handle %s for %s", event.change, event.node_id)
            self.stats.failed += 1

    # ------------------------------------------------------------- actions
    def _index(self, event: NodeEvent) -> None:
        """Full path: fetch, extract, chunk, embed, store."""
        node = self.alfresco.node(event.node_id)

        if node.node_type not in INDEXABLE_TYPES:
            self.stats.note_skip(f"type:{node.node_type}")
            return
        if not node.mime_type:
            self.stats.note_skip("no-content")
            return

        raw = self.alfresco.content(node.node_id)
        try:
            text = self.transform.to_text(raw, node.mime_type, filename=node.name)
        except ExtractionError as exc:
            log.debug("no text from %s (%s): %s", node.name, node.mime_type, exc)
            self.stats.note_skip(f"no-text:{node.mime_type}")
            return

        chunks = chunk_text(text, max_chars=self.max_chars, overlap_chars=self.overlap_chars)
        if not chunks:
            self.stats.note_skip("empty-after-chunking")
            return

        vectors = self.embedder.embed(chunks)

        self.store.upsert_document(
            node_id=node.node_id,
            name=node.name,
            site=node.site,
            path=node.path,
            mime_type=node.mime_type,
            modified_at=node.modified_at,
        )
        written = self.store.replace_chunks(node.node_id, chunks, vectors)

        self.stats.embedded += 1
        self.stats.chunks_written += written
        log.info("indexed %s (%d chunks, site=%s)", node.name, written, node.site)

    def _relocate(self, event: NodeEvent) -> None:
        """Content did not change — update location, keep the embeddings.

        The event carries the previous and new parent IDs but no readable
        path, so the site and path come from one metadata call.
        """
        node = self.alfresco.node(event.node_id)
        name = node.name if event.change is Change.RENAMED else None
        if self.store.update_location(node.node_id, node.site, node.path, name):
            self.stats.relocated += 1
            log.info("relocated %s -> site=%s path=%s", node.name, node.site, node.path)
        else:
            # Moved into scope before it was ever indexed.
            self._index(event)

    def _refresh_metadata(self, event: NodeEvent) -> None:
        node = self.alfresco.node(event.node_id)
        if self.store.update_location(node.node_id, node.site, node.path, node.name):
            self.stats.metadata_updated += 1
        else:
            self.stats.note_skip("metadata-for-unknown-node")

    def _delete(self, event: NodeEvent) -> None:
        if self.store.delete_document(event.node_id):
            self.stats.deleted += 1
            log.info("removed %s from the index", event.node_id)
        else:
            self.stats.note_skip("delete-for-unknown-node")
