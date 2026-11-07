"""Tests for event classification.

Every payload here is modelled on a real Event2 message captured from ACS
26.2. The discrimination rules are the project's main contribution, so they
are the part that gets tested.
"""

from __future__ import annotations

import pytest

from alfresco_semantic_search.events import Change, classify

NODE = "0f3a1b2c-1111-2222-3333-444455556666"
FOLDER_A = "aaaa1111-0000-0000-0000-000000000000"
FOLDER_B = "bbbb2222-0000-0000-0000-000000000000"


def node_event(event_type: str, before: dict | None = None, **resource) -> dict:
    """Build a CloudEvents envelope the way Alfresco emits one."""
    data: dict = {
        "eventGroupId": "group-1",
        "resource": {
            "@type": "NodeResource",
            "id": NODE,
            "name": "report.pdf",
            "nodeType": "cm:content",
            "primaryHierarchy": [FOLDER_B, "shared", "company-home"],
            **resource,
        },
    }
    if before is not None:
        data["resourceBefore"] = before
    return {
        "specversion": "1.0",
        "type": event_type,
        "dataschema": "https://api.alfresco.com/schema/event/repo/v1/nodeUpdated",
        "data": data,
    }


class TestCreateAndDelete:
    def test_created_needs_embedding(self):
        event = classify(node_event("org.alfresco.event.node.Created"))
        assert event.change is Change.CREATED
        assert event.needs_embedding
        assert event.node_id == NODE
        assert event.parent_id == FOLDER_B

    def test_deleted(self):
        event = classify(node_event("org.alfresco.event.node.Deleted"))
        assert event.change is Change.DELETED
        assert not event.needs_embedding

    def test_versionable_aspect_right_after_create_is_ignored(self):
        """Alfresco applies cm:versionable just after creation.

        Treating it as a real update would embed the document twice.
        """
        event = classify(
            node_event("org.alfresco.event.node.Updated", before={"aspectNames": []})
        )
        assert event.change is Change.IGNORE


class TestContentChange:
    def test_content_triggers_reembedding(self):
        event = classify(
            node_event(
                "org.alfresco.event.node.Updated",
                before={"content": {}, "modifiedAt": "2026-01-01T00:00:00Z", "properties": {}},
            )
        )
        assert event.change is Change.CONTENT
        assert event.needs_embedding

    def test_content_wins_over_a_simultaneous_rename(self):
        """Re-embedding is the costly, correctness-critical action, so it wins."""
        event = classify(
            node_event(
                "org.alfresco.event.node.Updated",
                before={"content": {}, "name": "old.pdf", "primaryHierarchy": [FOLDER_A]},
            )
        )
        assert event.change is Change.CONTENT


class TestMoveVersusRename:
    """Both change primaryHierarchy — the primary association's qualified
    name embeds the node name. ``name`` is what separates them."""

    def test_move_carries_both_parents(self):
        event = classify(
            node_event(
                "org.alfresco.event.node.Updated",
                before={"primaryHierarchy": [FOLDER_A, "shared"], "properties": {}},
            )
        )
        assert event.change is Change.MOVED
        assert not event.needs_embedding
        assert event.previous_parent_id == FOLDER_A
        assert event.parent_id == FOLDER_B

    def test_rename_is_not_a_move(self):
        event = classify(
            node_event(
                "org.alfresco.event.node.Updated",
                before={
                    "name": "old-name.pdf",
                    "primaryHierarchy": [FOLDER_B, "shared"],
                    "properties": {},
                },
            )
        )
        assert event.change is Change.RENAMED
        assert not event.needs_embedding


class TestMetadataAndNoise:
    def test_property_change_is_metadata_only(self):
        event = classify(
            node_event(
                "org.alfresco.event.node.Updated",
                before={"localizedProperties": {}, "modifiedAt": "x", "properties": {}},
            )
        )
        assert event.change is Change.METADATA
        assert not event.needs_embedding

    def test_parent_folder_timestamp_is_noise(self):
        """Containers get an Updated with just a timestamp when a child moves."""
        event = classify(
            node_event(
                "org.alfresco.event.node.Updated",
                before={"modifiedAt": "2026-01-01T00:00:00Z"},
                nodeType="cm:folder",
                name="Shared",
            )
        )
        assert event.change is Change.IGNORE

    def test_envelope_keys_are_not_treated_as_changed_fields(self):
        event = classify(
            node_event(
                "org.alfresco.event.node.Updated",
                before={"@type": "NodeResource", "content": {}},
            )
        )
        assert event.changed_fields == frozenset({"content"})


class TestWorkflowAssociations:
    """Event2 emits nothing for tasks or processes. A document entering a
    workflow is only visible through its package association."""

    def test_package_contains_is_detected(self):
        event = classify(
            {
                "type": "org.alfresco.event.assoc.child.Created",
                "data": {
                    "resource": {
                        "@type": "ChildAssociationResource",
                        "assocType": "bpm:packageContains",
                        "parent": {"id": "pkg-1"},
                        "child": {"id": NODE},
                    }
                },
            }
        )
        assert event.change is Change.WORKFLOW
        assert event.node_id == NODE
        assert event.assoc_type == "bpm:packageContains"

    def test_other_associations_are_ignored(self):
        event = classify(
            {
                "type": "org.alfresco.event.assoc.child.Created",
                "data": {
                    "resource": {
                        "assocType": "cm:contains",
                        "parent": {"id": FOLDER_A},
                        "child": {"id": NODE},
                    }
                },
            }
        )
        assert event.change is Change.IGNORE


@pytest.mark.parametrize(
    "event_type",
    [
        "org.alfresco.event.permission.Updated",
        "org.alfresco.event.node.Something",
        "",
    ],
)
def test_unknown_event_types_are_ignored(event_type):
    assert classify(node_event(event_type)).change is Change.IGNORE
