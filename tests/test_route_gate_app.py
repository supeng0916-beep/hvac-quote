"""R2 server-side evidence and revision checks."""
import pytest

from hvac_quote.app import _route_evidence_conflict
from hvac_quote.db import Database
from hvac_quote.new_quote_store import NewQuoteStore


class _FakeQuotes:
    def __init__(self, report):
        self.report = report

    def get_inspection(self, project_id, inspection_id):
        return {
            "id": inspection_id,
            "projectId": project_id,
            "report": self.report,
        }


def _route(**overrides):
    route = {
        "key": "route|one",
        "sourceInspectionId": "insp-1",
        "routeEvidenceKey": "key-1",
        "eligibility": "review",
        "autoInclude": False,
        "medium": "drain",
        "spec": "25",
    }
    route.update(overrides)
    return route


def _report(route):
    return {"routeGroups": [route], "equipmentGroups": []}


def test_authoritative_route_matches():
    route = _route()
    assert (
        _route_evidence_conflict(
            _FakeQuotes(_report(route)),
            "p1",
            {"inspectionId": "insp-1", "routes": [route]},
        )
        is None
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("routeEvidenceKey", "fake"),
        ("eligibility", "eligible"),
        ("autoInclude", True),
        ("medium", "refrigerant"),
        ("spec", "40"),
        ("key", "route|forged"),
    ],
)
def test_client_field_tamper_is_rejected(field, value):
    authoritative = _route()
    forged = dict(authoritative)
    forged[field] = value
    conflict = _route_evidence_conflict(
        _FakeQuotes(_report(authoritative)),
        "p1",
        {"inspectionId": "insp-1", "routes": [forged]},
    )
    assert conflict


def test_manual_customer_input_is_exempt_from_cad_evidence():
    assert (
        _route_evidence_conflict(
            _FakeQuotes({"routeGroups": []}),
            "p1",
            {
                "inspectionId": "insp-1",
                "routes": [
                    {
                        "key": "manual-1",
                        "origin": "customer_input",
                        "manualRecordId": "manual-1",
                        "include": True,
                    }
                ],
            },
        )
        is None
    )


def test_draft_revision_cas_rejects_stale_write(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    now = "2026-10-22T00:00:00+08:00"
    db.execute(
        "INSERT INTO projects(id, owner, name, customer, note, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("p1", "admin", "项目", "", "", now, now),
    )
    db.execute(
        "INSERT INTO project_files(id, project_id, original_name, stored_name, kind, "
        "size, sha256, uploaded_at) VALUES(?,?,?,?,?,?,?,?)",
        ("f1", "p1", "a.dwg", "stored.dwg", "drawing", 1, "a" * 64, now),
    )
    db.execute(
        "INSERT INTO drawing_inspections(id, project_id, file_id, status, report_json, created_at) "
        "VALUES(?,?,?,?,?,?)",
        ("i1", "p1", "f1", "done", "{}", now),
    )
    store = NewQuoteStore(db)
    draft = store.create_draft("p1", "i1", None, {"routes": []})
    assert draft["revision"] == 1
    updated = store.update_draft(
        "p1", draft["id"], data={"routes": []}, expected_revision=1
    )
    assert updated["revision"] == 2
    with pytest.raises(ValueError, match="刷新"):
        store.update_draft(
            "p1", draft["id"], data={"routes": []}, expected_revision=1
        )
    db.close()


def test_proposal_binding_requires_second_confirmation_when_quantity_changes():
    from copy import deepcopy

    from hvac_quote.app import _finalize_route_proposals
    from hvac_quote.generic_quote import (
        route_adoption_error,
        route_proposal_key,
    )

    previous = {
        "routes": [
            {
                "key": "route|1",
                "include": True,
                "eligibility": "review",
                "autoInclude": False,
                "routeEvidenceKey": "cad-key",
                "medium": "drain",
                "spec": "25",
                "lengthM": "0.3",
                "sourceRegionKey": "model|root",
                "segments": [{"physicalSegmentId": "e1:0:1"}],
            }
        ]
    }
    previous["routes"][0]["routeProposalKey"] = route_proposal_key(
        previous["routes"][0]
    )
    previous["routes"][0]["routeReview"] = {
        "confirmed": True,
        "evidenceKey": "cad-key",
        "proposalKey": previous["routes"][0]["routeProposalKey"],
    }
    current = deepcopy(previous)
    current["routes"][0]["lengthM"] = "123.456"
    current["routes"][0]["manualSpec"] = "99.99"
    current["routes"][0]["routeProposalKey"] = previous["routes"][0][
        "routeProposalKey"
    ]
    needs = _finalize_route_proposals(current, previous, {0: True})
    group = current["routes"][0]
    assert needs is True
    assert "routeReview" not in group
    assert group["eligibility"] == "review"
    assert route_adoption_error(group)

    # The next request sees the new proposal and may confirm it explicitly.
    needs = _finalize_route_proposals(current, current, {0: True})
    group = current["routes"][0]
    assert needs is False
    assert group["routeReview"]["proposalKey"] == group["routeProposalKey"]
    assert route_adoption_error(group) is None


def test_review_without_proposal_key_is_not_trusted():
    group = _route(
        include=True,
        routeProposalKey="proposal-1",
        routeReview={
            "confirmed": True,
            "evidenceKey": "key-1",
        },
    )
    from hvac_quote.generic_quote import route_adoption_error

    assert route_adoption_error(group)


def test_authoritative_snapshot_compares_segments_and_source_handles():
    from hvac_quote.generic_quote import _authoritative_route_error

    authoritative = {
        "key": "route|1",
        "routeEvidenceKey": "cad-key",
        "eligibility": "review",
        "autoInclude": False,
        "medium": "drain",
        "spec": "25",
        "sourceRegionKey": "model|root",
        "lengthRaw": 300,
        "reviewReasons": ["PIPE_IDENTITY_UNRESOLVED"],
        "segments": [{"physicalSegmentId": "e1:0:1"}],
        "handles": ["A"],
    }
    group = {
        **authoritative,
        "include": True,
        "routeProposalKey": "proposal-1",
        "sourceHandles": ["A"],
    }
    assert _authoritative_route_error({"routes": [group]}, None)
    auth = {"route|1": authoritative}
    assert _authoritative_route_error({"routes": [group]}, auth) is None
    forged = {**group, "lengthRaw": 999999}
    assert _authoritative_route_error({"routes": [forged]}, auth)
    forged = {**group, "sourceHandles": ["B"]}
    assert _authoritative_route_error({"routes": [forged]}, auth)
    forged = {**group, "segments": []}
    assert _authoritative_route_error({"routes": [forged]}, auth)


def test_app_snapshot_rejects_forged_segments_and_handles():
    authoritative = _route(
        sourceRegionKey="model|root",
        lengthRaw=300,
        reviewReasons=["PIPE_IDENTITY_UNRESOLVED"],
        segments=[{"physicalSegmentId": "e1:0:1"}],
        handles=["A"],
    )
    group = {
        **authoritative,
        "sourceHandles": ["A"],
        "segments": [{"physicalSegmentId": "e1:0:1"}],
        "reviewReasons": ["PIPE_IDENTITY_UNRESOLVED"],
    }
    quotes = _FakeQuotes(_report(authoritative))
    data = {"inspectionId": "insp-1", "routes": [group]}
    assert _route_evidence_conflict(quotes, "p1", data) is None
    for field, value in (
        ("sourceHandles", ["B"]),
        ("segments", []),
        ("reviewReasons", []),
    ):
        forged = {**group, field: value}
        assert _route_evidence_conflict(
            quotes, "p1", {**data, "routes": [forged]}
        )
