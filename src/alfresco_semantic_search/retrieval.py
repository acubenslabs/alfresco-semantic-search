"""Permission-aware semantic retrieval.

The whole project exists for the ordering enforced here:

    1. vector search      (over-fetch, no permission filtering)
    2. permission filter  (Alfresco decides, as the end user)
    3. build the prompt   (only with what survived)

Running step 3 before step 2 leaks document content to users who cannot read
it, and no amount of trimming the citation list afterwards undoes that — the
text is already in the prompt. :func:`search` makes the correct order the only
one available, which is why it takes the end user's credentials rather than
accepting a pre-filtered list.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from .alfresco import AlfrescoClient
from .store import ChunkStore, Hit

log = logging.getLogger(__name__)

#: Fetch this many times the requested results before filtering. Permission
#: filtering happens after ranking, so without over-fetching a user with
#: narrow access gets back almost nothing.
DEFAULT_OVERFETCH = 5


@dataclass(frozen=True, slots=True)
class Caller:
    """The end user a query runs on behalf of.

    Never pass an administrative account: an admin can read everything, so the
    permission filter would silently pass every candidate through.
    """

    username: str
    password: str


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Chunks the caller is allowed to read, plus what filtering cost."""

    hits: list[Hit]
    candidates_considered: int
    candidates_denied: int

    @property
    def denied_ratio(self) -> float:
        """Share of candidates the caller could not read.

        Consistently high values mean the over-fetch factor is too low for
        this user's access profile.
        """
        if self.candidates_considered == 0:
            return 0.0
        return self.candidates_denied / self.candidates_considered

    def as_context(self, separator: str = "\n\n---\n\n") -> str:
        """Join the surviving chunk texts into prompt context.

        Safe to call: by construction every chunk here passed the permission
        check.
        """
        return separator.join(hit.text for hit in self.hits)


def search(
    query_embedding: Sequence[float],
    caller: Caller,
    store: ChunkStore,
    alfresco: AlfrescoClient,
    limit: int = 10,
    overfetch: int = DEFAULT_OVERFETCH,
    site: str | None = None,
) -> RetrievalResult:
    """Retrieve chunks the ``caller`` is allowed to read.

    Args:
        query_embedding: the embedded query, same model as the stored chunks.
        caller: end-user credentials. **Not** a service account.
        limit: how many chunks to return after filtering.
        overfetch: multiplier applied to ``limit`` before filtering.
        site: optional restriction to one Alfresco site. A convenience filter,
            not a security boundary — the permission check is.

    Returns:
        A :class:`RetrievalResult` whose ``hits`` are all readable by ``caller``.
    """
    candidates = store.search(query_embedding, limit=limit * overfetch, site=site)
    if not candidates:
        return RetrievalResult(hits=[], candidates_considered=0, candidates_denied=0)

    kept = permission_filter(candidates, caller, alfresco)
    denied = len(candidates) - len(kept)

    if denied and len(kept) < limit:
        log.info(
            "permission filter removed %d/%d candidates; returning %d of %d requested "
            "(consider raising overfetch)",
            denied, len(candidates), min(len(kept), limit), limit,
        )

    return RetrievalResult(
        hits=kept[:limit],
        candidates_considered=len(candidates),
        candidates_denied=denied,
    )


def permission_filter(
    candidates: Sequence[Hit],
    caller: Caller,
    alfresco: AlfrescoClient,
) -> list[Hit]:
    """Drop the chunks whose document the caller cannot read.

    One round trip regardless of how many candidates there are: the node IDs
    go out as a single AFTS ``ID:"..." OR ID:"..."`` query, authenticated as
    the caller. Alfresco answers with the subset it will disclose.

    Relevance order is preserved, so the ranking from the vector search
    survives the filter.
    """
    if not candidates:
        return []

    allowed = alfresco.readable_ids(
        (hit.node_id for hit in candidates),
        as_user=caller.username,
        password=caller.password,
    )
    return [hit for hit in candidates if hit.node_id in allowed]
