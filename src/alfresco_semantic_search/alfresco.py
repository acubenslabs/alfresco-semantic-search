"""Alfresco REST client — the parts a RAG pipeline actually needs.

Two responsibilities, and the second one is the point of this project:

* **Indexing side** (service account): fetch content bytes and node metadata.
* **Query side** (end user): ask the repository which of a set of node IDs the
  *caller* is allowed to read.

The second one is why this project exists. Permissions are never copied into
the vector store — they are resolved by Alfresco, as the end user, at query
time. See ``docs/FINDINGS.md`` for why the alternative leaks.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

STORE_REF = "workspace://SpacesStore"


class AlfrescoError(RuntimeError):
    """A non-2xx response from the repository."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


@dataclass(frozen=True, slots=True)
class NodeInfo:
    """Node metadata relevant to retrieval and filtering."""

    node_id: str
    name: str
    node_type: str
    mime_type: str | None
    size_bytes: int | None
    modified_at: str
    parent_id: str | None
    #: Readable path, e.g. ``/Company Home/Sites/swsdp/documentLibrary/Contracts``
    path: str | None
    #: Site short name, or ``None`` when the node lives outside a site.
    site: str | None
    properties: dict[str, Any] = field(default_factory=dict)


class AlfrescoClient:
    """Thin REST client over the Alfresco v1 public API."""

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        user: str = "admin",
        password: str = "admin",
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._auth = _basic(user, password)

    # ------------------------------------------------------------------ paths
    @property
    def _api(self) -> str:
        return f"{self.base_url}/alfresco/api/-default-/public/alfresco/versions/1"

    @property
    def _search_api(self) -> str:
        return f"{self.base_url}/alfresco/api/-default-/public/search/versions/1"

    # ------------------------------------------------------------- indexing
    def node(self, node_id: str) -> NodeInfo:
        """Fetch node metadata, including the readable path and its site."""
        data = self._get(f"{self._api}/nodes/{node_id}", params={"include": "path,properties"})
        entry = data["entry"]
        path = (entry.get("path") or {}).get("name")
        elements = [e.get("name") for e in (entry.get("path") or {}).get("elements", [])]
        content = entry.get("content") or {}
        return NodeInfo(
            node_id=entry["id"],
            name=entry["name"],
            node_type=entry["nodeType"],
            mime_type=content.get("mimeType"),
            size_bytes=content.get("sizeInBytes"),
            modified_at=entry["modifiedAt"],
            parent_id=entry.get("parentId"),
            path=path,
            site=site_from_path_elements(elements),
            properties=entry.get("properties") or {},
        )

    def content(self, node_id: str) -> bytes:
        """Download the original bytes of a node."""
        req = urllib.request.Request(
            f"{self._api}/nodes/{node_id}/content",
            headers={"Authorization": self._auth},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise AlfrescoError(exc.code, exc.reason or "") from exc

    def children(self, node_id: str, skip: int = 0, limit: int = 100) -> list[dict]:
        """List a folder's children, one page at a time."""
        data = self._get(
            f"{self._api}/nodes/{node_id}/children",
            params={"skipCount": skip, "maxItems": limit, "include": "path"},
        )
        return [e["entry"] for e in data["list"]["entries"]]

    def search(self, afts: str, max_items: int = 100, skip: int = 0) -> list[dict]:
        """Run an AFTS query as the configured user."""
        body = {
            "query": {"query": afts, "language": "afts"},
            "paging": {"maxItems": max_items, "skipCount": skip},
        }
        data = self._post(f"{self._search_api}/search", body)
        return [e["entry"] for e in data["list"]["entries"]]

    # ------------------------------------------------------- permission check
    def readable_ids(
        self,
        node_ids: Iterable[str],
        as_user: str,
        password: str,
        batch_size: int = 100,
    ) -> set[str]:
        """Return the subset of ``node_ids`` that ``as_user`` may read.

        This is the permission-enforcement point of the whole pipeline. The
        query runs **authenticated as the end user**, so Alfresco applies its
        own ACL semantics — group membership, inheritance, denials and dynamic
        authorities like ``ROLE_OWNER`` — none of which is replicated here.

        Two properties that make this the safe design:

        * The repository re-validates permissions against the database when it
          resolves each node, so it stays correct even when its search index is
          stale. A revoked permission takes effect immediately.
        * Nothing about permissions is stored on our side, so nothing can drift
          out of sync.

        Never call this with an administrative account: an admin can read
        everything, which would silently disable the filter.
        """
        unique = list(dict.fromkeys(node_ids))
        if not unique:
            return set()

        auth = _basic(as_user, password)
        allowed: set[str] = set()

        for start in range(0, len(unique), batch_size):
            batch = unique[start : start + batch_size]
            afts = " OR ".join(f'ID:"{STORE_REF}/{nid}"' for nid in batch)
            body = {
                "query": {"query": afts, "language": "afts"},
                "paging": {"maxItems": len(batch)},
                "fields": ["id"],
            }
            data = self._post(f"{self._search_api}/search", body, auth=auth)
            allowed.update(e["entry"]["id"] for e in data["list"]["entries"])

        log.debug("permission check: %d of %d node ids readable by %s",
                  len(allowed), len(unique), as_user)
        return allowed

    # ---------------------------------------------------------------- probes
    def ready(self) -> bool:
        """Whether the repository can serve requests.

        The repository exposes no metrics endpoint; these probes are the only
        health signal it offers. Note the dashes on both sides of the name —
        ``-health`` is not a valid probe and returns HTTP 400.
        """
        try:
            self._get(f"{self._api}/probes/-ready-")
            return True
        except (AlfrescoError, urllib.error.URLError):
            return False

    def version(self) -> dict:
        """Repository edition, version and schema number."""
        data = self._get(f"{self.base_url}/alfresco/api/discovery")
        return data["entry"]["repository"]

    # -------------------------------------------------------------- internals
    def _get(self, url: str, params: dict | None = None, auth: str | None = None) -> dict:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return self._request(url, None, auth, method="GET")

    def _post(self, url: str, body: dict, auth: str | None = None) -> dict:
        return self._request(url, json.dumps(body).encode(), auth, method="POST")

    def _request(self, url: str, data: bytes | None, auth: str | None, method: str) -> dict:
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": auth or self._auth,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise AlfrescoError(exc.code, detail) from exc


def site_from_path_elements(elements: Sequence[str | None]) -> str | None:
    """Extract the site short name from a node's path elements.

    Site content lives under ``/Company Home/Sites/<site>/documentLibrary/...``,
    so the element right after ``Sites`` is the site. Nodes outside a site —
    ``/Company Home/Shared/...`` for instance — return ``None``.
    """
    try:
        return elements[elements.index("Sites") + 1]
    except (ValueError, IndexError):
        return None


def _basic(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"
