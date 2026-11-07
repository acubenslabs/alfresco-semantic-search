"""Alfresco Event2 consumer and change classification.

Event2 is Alfresco's built-in event subsystem. It publishes a CloudEvents 1.0
message to the ActiveMQ topic ``alfresco.repo.event2`` on every node change.
It ships enabled (``repo.event2.enabled=true``) and has existed since ACS 7.0,
so the same code works against 7.x and 26.x.

Two things about the payload are not obvious from the docs and cost real time
to discover:

1. The field holding the previous state is ``data.resourceBefore`` — **not**
   ``resourceBeforeUpdate``.
2. It contains **only the attributes that actually changed**, which is what
   makes it possible to tell a content change from a rename without fetching
   the node.

See ``docs/FINDINGS.md`` for the measurements these rules come from.
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

log = logging.getLogger(__name__)

NULL = b"\x00"

#: Emitted right after a node is created, when Alfresco applies
#: ``cm:versionable``. Indexing on it would embed the same document twice.
_SPURIOUS_CREATE_FOLLOWUP = {"aspectNames"}


class Change(StrEnum):
    """What a repository event means for a downstream index."""

    #: New node. Fetch content, chunk, embed.
    CREATED = "created"
    #: The binary changed. Re-extract and re-embed.
    CONTENT = "content"
    #: Moved to a different parent. Update location only — do not re-embed.
    MOVED = "moved"
    #: Renamed in place. Update the name only — do not re-embed.
    RENAMED = "renamed"
    #: Metadata changed. Update metadata only.
    METADATA = "metadata"
    #: Node removed. Drop its chunks.
    DELETED = "deleted"
    #: A document entered or left a workflow package.
    WORKFLOW = "workflow"
    #: Nothing a content index needs to act on.
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True)
class NodeEvent:
    """A repository event, normalised to what an indexer needs."""

    change: Change
    node_id: str
    name: str | None
    node_type: str | None
    changed_fields: frozenset[str]
    #: Immediate parent after the change, when the event carries it.
    parent_id: str | None = None
    #: Immediate parent before the change. Only set for ``MOVED``.
    previous_parent_id: str | None = None
    #: For ``WORKFLOW``: the association type, e.g. ``bpm:packageContains``.
    assoc_type: str | None = None
    raw: dict | None = None

    @property
    def needs_embedding(self) -> bool:
        """Whether this change invalidates the stored embeddings."""
        return self.change in (Change.CREATED, Change.CONTENT)


def classify(event: dict) -> NodeEvent:
    """Turn a raw Event2 CloudEvent into a :class:`NodeEvent`.

    The rules below are derived from measured behaviour on ACS 26.2, not from
    documentation. Every branch is covered by ``tests/test_events.py``.
    """
    event_type = event.get("type", "")
    data = event.get("data") or {}
    resource = data.get("resource") or {}
    before = data.get("resourceBefore") or {}

    # Association events carry no node — they describe a link between two.
    if ".assoc." in event_type:
        return _classify_assoc(event_type, resource, event)

    node_id = resource.get("id", "")
    name = resource.get("name")
    node_type = resource.get("nodeType")
    hierarchy = resource.get("primaryHierarchy") or []
    parent_id = hierarchy[0] if hierarchy else None

    # ``@type`` and similar envelope keys are not node attributes.
    changed = frozenset(k for k in before if not k.startswith("@"))

    if event_type.endswith(".node.Created"):
        return NodeEvent(Change.CREATED, node_id, name, node_type, changed,
                         parent_id=parent_id, raw=event)

    if event_type.endswith(".node.Deleted"):
        return NodeEvent(Change.DELETED, node_id, name, node_type, changed,
                         parent_id=parent_id, raw=event)

    if not event_type.endswith(".node.Updated"):
        return NodeEvent(Change.IGNORE, node_id, name, node_type, changed, raw=event)

    # --- Updated: the interesting case -------------------------------------
    # Order matters. A content change can coincide with a property change, and
    # re-embedding is the costly, correctness-critical action, so it wins.
    if "content" in changed:
        return NodeEvent(Change.CONTENT, node_id, name, node_type, changed,
                         parent_id=parent_id, raw=event)

    # Both a move and a rename change primaryHierarchy, because the primary
    # association's qualified name embeds the node name. The presence of
    # ``name`` is what separates them.
    if "primaryHierarchy" in changed:
        previous = before.get("primaryHierarchy") or []
        if "name" in changed:
            return NodeEvent(Change.RENAMED, node_id, name, node_type, changed,
                             parent_id=parent_id,
                             previous_parent_id=previous[0] if previous else None,
                             raw=event)
        return NodeEvent(Change.MOVED, node_id, name, node_type, changed,
                         parent_id=parent_id,
                         previous_parent_id=previous[0] if previous else None,
                         raw=event)

    # Alfresco applying cm:versionable right after creation. Embedding here
    # would duplicate the document.
    if changed == _SPURIOUS_CREATE_FOLLOWUP:
        return NodeEvent(Change.IGNORE, node_id, name, node_type, changed, raw=event)

    # Containers get an Updated with just a timestamp whenever a child moves.
    if changed <= {"modifiedAt", "modifiedByUser"}:
        return NodeEvent(Change.IGNORE, node_id, name, node_type, changed, raw=event)

    if changed & {"properties", "localizedProperties", "aspectNames"}:
        return NodeEvent(Change.METADATA, node_id, name, node_type, changed,
                         parent_id=parent_id, raw=event)

    return NodeEvent(Change.IGNORE, node_id, name, node_type, changed, raw=event)


def _classify_assoc(event_type: str, resource: dict, event: dict) -> NodeEvent:
    """Child/peer association events.

    ``bpm:packageContains`` is how a document entering or leaving a workflow
    becomes visible: Event2 emits nothing for tasks or processes themselves.
    """
    assoc_type = resource.get("assocType")
    child = (resource.get("child") or {}).get("id", "")
    parent = (resource.get("parent") or {}).get("id")

    if assoc_type == "bpm:packageContains":
        return NodeEvent(Change.WORKFLOW, child, None, None, frozenset(),
                         parent_id=parent, assoc_type=assoc_type, raw=event)

    return NodeEvent(Change.IGNORE, child, None, None, frozenset(),
                     parent_id=parent, assoc_type=assoc_type, raw=event)


class Event2Listener:
    """Consumes ``alfresco.repo.event2`` over STOMP.

    STOMP 1.2 is a line-based text protocol, so this needs no dependencies.

    .. warning::
       The ``alfresco/alfresco-activemq`` image ships with **only the openwire
       connector enabled** — STOMP, AMQP, MQTT and WS are commented out in
       ``activemq.xml`` to reduce the exposed attack surface. Run
       ``scripts/enable-stomp.sh`` before using this listener.

    .. note::
       A plain topic subscription has no memory: events published while the
       consumer is down are lost, not queued. For production use a durable
       subscription or ActiveMQ's Virtual Topic pattern, and pair it with the
       periodic sweep in :mod:`alfresco_semantic_search.reconcile`.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 61613,
        user: str = "admin",
        password: str = "admin",
        topic: str = "/topic/alfresco.repo.event2",
        subscription_id: str = "alfresco-semantic-search",
    ) -> None:
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.topic = topic
        self.subscription_id = subscription_id
        self._sock: socket.socket | None = None

    def __enter__(self) -> Event2Listener:
        self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=10)
        sock.sendall(
            f"CONNECT\naccept-version:1.2\nhost:/\n"
            f"login:{self.user}\npasscode:{self.password}\n\n".encode() + NULL
        )
        sock.settimeout(10)
        reply = sock.recv(4096)
        if not reply.startswith(b"CONNECTED"):
            sock.close()
            raise ConnectionError(f"STOMP broker refused the connection: {reply[:200]!r}")
        sock.sendall(
            f"SUBSCRIBE\nid:{self.subscription_id}\n"
            f"destination:{self.topic}\nack:auto\n\n".encode() + NULL
        )
        self._sock = sock
        log.info("subscribed to %s", self.topic)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def events(self, timeout: float | None = None) -> Iterator[NodeEvent]:
        """Yield classified events until ``timeout`` seconds elapse.

        With ``timeout=None`` this runs until the connection drops.
        """
        if self._sock is None:
            raise RuntimeError("call connect() first")

        import time

        deadline = None if timeout is None else time.monotonic() + timeout
        self._sock.settimeout(1.0)
        buffer = b""

        while deadline is None or time.monotonic() < deadline:
            try:
                chunk = self._sock.recv(65536)
            except TimeoutError:
                continue
            if not chunk:
                log.warning("broker closed the connection")
                return
            buffer += chunk

            while NULL in buffer:
                frame, buffer = buffer.split(NULL, 1)
                payload = _frame_body(frame)
                if payload is None:
                    continue
                try:
                    yield classify(json.loads(payload))
                except json.JSONDecodeError:
                    log.debug("skipping non-JSON frame body")


def _frame_body(frame: bytes) -> str | None:
    """Extract the body of a STOMP MESSAGE frame, or None if not one."""
    text = frame.decode("utf-8", "replace").lstrip("\n")
    if not text.startswith("MESSAGE") or "\n\n" not in text:
        return None
    body = text.split("\n\n", 1)[1].strip()
    return body if body.startswith("{") else None
