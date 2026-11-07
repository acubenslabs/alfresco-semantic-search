#!/usr/bin/env python3
"""End-to-end demonstration, including the part that matters.

Indexes a few documents, then runs the *same* semantic query as two different
users and shows that one of them cannot see a restricted document — because
Alfresco said so, not because this code reimplemented its ACL rules.

Uses the deterministic FakeEmbedder, so it needs no API key. Similarity is
meaningless; the point is the permission boundary, which is real.

    scripts/enable-stomp.sh
    docker compose up -d          # wait for alfresco to report healthy
    python scripts/demo.py
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from alfresco_semantic_search import (
    AlfrescoClient,
    Caller,
    ChunkStore,
    FakeEmbedder,
    Indexer,
    TransformClient,
    search,
)
from alfresco_semantic_search.events import Change, NodeEvent

ALFRESCO_URL = os.environ.get("ALFRESCO_URL", "http://localhost:8080")
TRANSFORM_URL = os.environ.get("TRANSFORM_URL", "http://localhost:8090")
PGVECTOR_DSN = os.environ.get("PGVECTOR_DSN", "postgresql://rag:rag@localhost:5433/rag")

PUBLIC = "Quarterly maintenance plan. The pipeline inspection is scheduled for March."
PRIVATE = "Confidential settlement terms. The penalty clause is two million."


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    log = logging.getLogger("demo")

    try:
        import psycopg
    except ImportError:
        return fail("psycopg is required: pip install 'alfresco-semantic-search[postgres]'")

    admin = AlfrescoClient(ALFRESCO_URL, "admin", "admin")
    if not admin.ready():
        return fail(f"Alfresco is not ready at {ALFRESCO_URL}. Is the stack up?")

    repo = admin.version()
    log.info("repository: %s %s (schema %s)", repo["edition"],
             repo["version"]["display"], repo["version"]["schema"])

    transform = TransformClient(TRANSFORM_URL)
    if not transform.ready():
        return fail(f"transform service is not ready at {TRANSFORM_URL}")

    embedder = FakeEmbedder(dims=1536)

    with psycopg.connect(PGVECTOR_DSN) as conn:
        store = ChunkStore(conn, dims=embedder.dims)
        store.init_schema()
        indexer = Indexer(admin, transform, store, embedder)

        log.info("--- setting up two documents with different permissions ---")
        alice, bob = _ensure_users(admin, log)
        public_id = _upload(admin, "-shared-", "maintenance-plan.txt", PUBLIC)
        private_id = _upload_private(admin, alice, "settlement-terms.txt", PRIVATE)
        log.info("public  %s -> readable by everyone", public_id[:8])
        log.info("private %s -> readable by %s only", private_id[:8], alice)

        log.info("--- indexing (no permissions are stored) ---")
        for node_id in (public_id, private_id):
            indexer.handle(NodeEvent(Change.CREATED, node_id, None, None, frozenset()))
        log.info("store: %s", store.stats())
        if indexer.stats.embedded == 0:
            return fail(f"nothing was indexed; skips: {indexer.stats.skip_reasons}")

        log.info("--- waiting for Alfresco to index the nodes for search ---")
        # The permission check runs through Alfresco's search API, which needs
        # the nodes in its own index first. batch-indexing polls every 15s.
        if not _wait_for_search(admin, private_id, log):
            return fail("Alfresco did not index the test nodes in time")

        log.info("--- same query, two users ---")
        query_vector = embedder.embed(["penalty clause settlement"])[0]

        for username in (alice, bob):
            result = search(
                query_vector,
                Caller(username, "demo1234"),
                store,
                admin,
                limit=5,
            )
            names = sorted({hit.name for hit in result.hits})
            log.info(
                "%-6s sees %d chunk(s) from %s  (%d of %d candidates denied)",
                username, len(result.hits), names or "nothing",
                result.candidates_denied, result.candidates_considered,
            )

        log.info("")
        log.info("%s is the owner, so the restricted document is visible.", alice)
        log.info("%s is not, so it never reaches the prompt — Alfresco filtered it,", bob)
        log.info("and this code stores no ACL of any kind.")

        _cleanup(admin, [public_id, private_id], store, log)

    return 0


def _ensure_users(client: AlfrescoClient, log) -> tuple[str, str]:
    """Create two ordinary users. Neither is an administrator."""
    for username in ("alice", "bob"):
        try:
            client._post(
                f"{client._api}/people",
                {
                    "id": username,
                    "firstName": username.capitalize(),
                    "lastName": "Demo",
                    "email": f"{username}@example.invalid",
                    "password": "demo1234",
                },
            )
            log.info("created user %s", username)
        except Exception:
            log.info("user %s already exists", username)
    return "alice", "bob"


def _upload(client: AlfrescoClient, parent: str, name: str, body: str) -> str:
    """Upload a text file as the service account."""
    return _multipart_upload(client, parent, name, body, user=None)


def _upload_private(client: AlfrescoClient, owner: str, name: str, body: str) -> str:
    """Upload into the owner's home folder, which only they can read."""
    return _multipart_upload(client, "-my-", name, body, user=owner)


def _multipart_upload(
    client: AlfrescoClient, parent: str, name: str, body: str, user: str | None
) -> str:
    import json
    import urllib.request

    from alfresco_semantic_search.alfresco import _basic
    from alfresco_semantic_search.extract import _multipart

    payload, content_type = _multipart(
        # autoRename keeps the demo runnable twice in a row: without it a
        # leftover from an interrupted run makes the upload fail with 409.
        fields={"name": name, "autoRename": "true"},
        file_field="filedata",
        filename=name,
        file_bytes=body.encode(),
        file_mime="text/plain",
    )
    auth = _basic(user, "demo1234") if user else client._auth
    request = urllib.request.Request(
        f"{client._api}/nodes/{parent}/children",
        data=payload,
        headers={"Authorization": auth, "Content-Type": content_type},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())["entry"]["id"]


def _wait_for_search(client: AlfrescoClient, node_id: str, log, attempts: int = 12) -> bool:
    for attempt in range(attempts):
        if client.readable_ids([node_id], "admin", "admin"):
            return True
        time.sleep(5)
        if attempt == 2:
            log.info("still waiting for the search index...")
    return False


def _cleanup(client: AlfrescoClient, node_ids: list[str], store: ChunkStore, log) -> None:
    import urllib.request

    log.info("--- cleaning up ---")
    for node_id in node_ids:
        store.delete_document(node_id)
        request = urllib.request.Request(
            f"{client._api}/nodes/{node_id}?permanent=true",
            headers={"Authorization": client._auth},
            method="DELETE",
        )
        with contextlib.suppress(Exception):
            urllib.request.urlopen(request, timeout=30)
    log.info("done. Users alice and bob were left in place.")


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
