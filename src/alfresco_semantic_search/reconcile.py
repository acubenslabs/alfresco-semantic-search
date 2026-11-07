"""Periodic reconciliation — the safety net events cannot provide.

A plain topic subscription has no memory. Events published while the consumer
is down are gone, not queued. Durable subscriptions help, but brokers get
restarted, consumers crash mid-message and bugs happen.

So do not rely on events alone. Alfresco does not: ``batch-indexing`` runs
``reindexByDate`` jobs with a watermark over the database *in addition to*
consuming events. Events give you low latency; a periodic sweep gives you
completeness.

This module is the sweep. It walks the repository, compares each document's
``modifiedAt`` against what the store holds, and re-indexes what drifted.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

from .alfresco import AlfrescoClient
from .events import Change, NodeEvent
from .indexer import Indexer
from .store import ChunkStore

log = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    """What a sweep found."""

    scanned: int = 0
    missing: int = 0
    stale: int = 0
    orphaned: int = 0
    reindexed: int = 0

    @property
    def drifted(self) -> int:
        return self.missing + self.stale

    def summary(self) -> str:
        return (
            f"scanned={self.scanned} missing={self.missing} stale={self.stale} "
            f"orphaned={self.orphaned} reindexed={self.reindexed}"
        )


def reconcile(
    alfresco: AlfrescoClient,
    store: ChunkStore,
    indexer: Indexer,
    query: str = 'TYPE:"cm:content"',
    page_size: int = 100,
    max_documents: int | None = None,
    delete_orphans: bool = False,
) -> ReconcileReport:
    """Compare the repository against the store and fix the differences.

    Args:
        query: AFTS query defining what should be indexed. Narrow it to a site
            or a path to sweep incrementally — a full sweep of a large
            repository is expensive.
        max_documents: stop after this many, for bounded runs.
        delete_orphans: also remove stored documents that no longer exist in
            the repository. Off by default: with permissions resolved at query
            time, orphaned chunks are *invisible*, not dangerous — the
            permission check simply never returns them. They waste space, so a
            monthly cleanup is enough.

    Returns:
        A :class:`ReconcileReport`. Call it during a maintenance window and
        log the summary; ``drifted`` consistently above zero means the event
        path is losing messages.
    """
    report = ReconcileReport()
    known = store.known_documents()
    seen: set[str] = set()

    for entry in _walk(alfresco, query, page_size, max_documents):
        node_id = entry["id"]
        seen.add(node_id)
        report.scanned += 1

        stored_modified = known.get(node_id)
        if stored_modified is None:
            report.missing += 1
        elif _is_newer(entry.get("modifiedAt", ""), stored_modified):
            report.stale += 1
        else:
            continue

        # Reuse the indexer so one code path produces the index, whether the
        # trigger was an event or this sweep.
        indexer.handle(
            NodeEvent(
                change=Change.CONTENT,
                node_id=node_id,
                name=entry.get("name"),
                node_type=entry.get("nodeType"),
                changed_fields=frozenset({"content"}),
            )
        )
        report.reindexed += 1

    orphans = set(known) - seen
    report.orphaned = len(orphans)
    if delete_orphans:
        for node_id in orphans:
            store.delete_document(node_id)
        log.info("removed %d orphaned documents", len(orphans))

    log.info("reconciliation: %s", report.summary())
    return report


def _walk(
    alfresco: AlfrescoClient,
    query: str,
    page_size: int,
    max_documents: int | None,
) -> Iterator[dict]:
    """Page through search results.

    Deep paging gets expensive: Alfresco resolves every node from the database
    and re-validates permissions, so page 500 costs the same per row as page 1
    plus the skip. For large repositories, narrow ``query`` by site or date
    range and sweep in slices rather than paging to the end.
    """
    skip = 0
    while True:
        batch = alfresco.search(query, max_items=page_size, skip=skip)
        if not batch:
            return
        for entry in batch:
            yield entry
            skip += 1
            if max_documents is not None and skip >= max_documents:
                return
        if len(batch) < page_size:
            return


def _is_newer(repository_value: str, stored_value: str) -> bool:
    """Whether the repository's timestamp is ahead of the stored one.

    ISO-8601 with the same offset compares correctly as a string, which keeps
    this dependency-free. Alfresco always returns UTC with a ``Z`` suffix.
    """
    if not repository_value:
        return False
    return repository_value > stored_value
