"""V048-01/02: strict envelope, decision whitelist and split authorization."""
import copy
import math

import pytest

from hvac_quote.vision import SCHEMA_VERSION, validate_suggestion

FP = "a" * 64
GOOD = {
    "candidateIds": ["route|20019A"],
    "category": "pipe",
    "medium": "drain",
    "role": "unknown",
    "evidenceCropIds": ["crop-A"],
    "linkedAnnotationIds": ["note-A"],
    "reason": "相邻 DN25 注记，仍需人工确认",
    "uncertainty": 0.2,
    "conflicts": [],
}


def envelope(decision=None):
    return {
        "schemaVersion": SCHEMA_VERSION,
        "inputFingerprint": FP,
        "decisions": [copy.deepcopy(GOOD if decision is None else decision)],
        "unresolved": [],
    }


def check(payload, **overrides):
    args = {
        "candidate_ids": {"route|20019A"},
        "expected_fingerprint": FP,
        "allowed_crops": {"crop-A"},
        "allowed_annotations": {"note-A"},
        "allowed_edges": set(),
        "allowed_vertices": set(),
    }
    args.update(overrides)
    return validate_suggestion(payload, **args)


@pytest.mark.parametrize(
    "addition", [{"extra": {"lengthM": 123.45}}, {"amount": 999}]
)
def test_forbidden_anywhere_rejects_whole_response(addition):
    bad = envelope({**GOOD, **addition})
    bad["decisions"].append(copy.deepcopy(GOOD))
    clean, errors = check(bad)
    assert clean == {} and errors


def test_wrong_schema_does_not_launder_decisions():
    bad = envelope()
    bad["schemaVersion"] = "wrong"
    clean, errors = check(bad)
    assert clean == {} and errors


@pytest.mark.parametrize(
    "split",
    [
        "edge-forged",
        [{"edgeId": "forged"}],
        {"point": [20, 30]},
        {},
        {"edgeId": "edge-A"},
        {
            "kind": "existingVertex",
            "edgeId": "edge-A",
            "vertexId": "vertex-A",
        },
    ],
)
def test_empty_topology_never_accepts_split(split):
    bad = envelope({**GOOD, "splitProposal": split})
    clean, errors = check(bad)
    assert errors and clean.get("decisions", []) == []


def test_clean_result_has_no_alias_to_model_object():
    raw = envelope()
    clean, errors = check(raw)
    assert not errors
    raw["decisions"][0]["candidateIds"].append("forged")
    raw["decisions"][0]["reason"] = "changed"
    assert clean["decisions"][0]["candidateIds"] == ["route|20019A"]
    assert clean["decisions"][0]["reason"] != "changed"


def test_top_level_extra_key_rejects_whole_response():
    bad = envelope()
    bad["estimatedTotal"] = 100
    clean, errors = check(bad)
    assert clean == {} and errors


def test_unknown_decision_key_drops_only_that_item():
    first = {**GOOD, "extra": {"harmless": True}}
    second = {
        **GOOD,
        "candidateIds": ["route|20019B"],
        "category": "equipment",
    }
    payload = envelope(first)
    payload["decisions"].append(second)
    clean, errors = check(
        payload, candidate_ids={"route|20019A", "route|20019B"}
    )
    assert errors
    assert [item["candidateIds"] for item in clean["decisions"]] == [
        ["route|20019B"]
    ]


@pytest.mark.parametrize(
    "uncertainty", [True, "0.2", math.nan, math.inf, -0.1, 1.1]
)
def test_uncertainty_never_coerced(uncertainty):
    bad = envelope({**GOOD, "uncertainty": uncertainty})
    clean, errors = check(bad)
    assert errors and clean.get("decisions", []) == []


def test_valid_split_requires_edge_and_vertex_mapping():
    split = {
        "kind": "existingVertex",
        "edgeId": "edge-A",
        "vertexId": "vertex-A",
    }
    clean, errors = check(
        envelope({**GOOD, "splitProposal": split}),
        allowed_edges={"edge-A"},
        allowed_vertices={"vertex-A"},
        candidate_edges={"route|20019A": {"edge-A"}},
        edge_vertices={"edge-A": {"vertex-A"}},
    )
    assert not errors
    assert clean["decisions"][0]["splitProposal"] == split


def test_split_edge_must_belong_to_same_candidate():
    split = {
        "kind": "existingVertex",
        "edgeId": "edge-A",
        "vertexId": "vertex-A",
    }
    clean, errors = check(
        envelope({**GOOD, "splitProposal": split}),
        allowed_edges={"edge-A"},
        allowed_vertices={"vertex-A"},
        candidate_edges={"route|other": {"edge-A"}},
        edge_vertices={"edge-A": {"vertex-A"}},
    )
    assert errors and clean.get("decisions", []) == []


def test_same_candidate_conflicting_split_removes_both():
    first = {
        **GOOD,
        "splitProposal": {
            "kind": "existingVertex",
            "edgeId": "edge-A",
            "vertexId": "vertex-A",
        },
    }
    second = {
        **GOOD,
        "splitProposal": {
            "kind": "existingVertex",
            "edgeId": "edge-A",
            "vertexId": "vertex-B",
        },
    }
    payload = envelope(first)
    payload["decisions"].append(second)
    clean, errors = check(
        payload,
        allowed_edges={"edge-A"},
        allowed_vertices={"vertex-A", "vertex-B"},
        candidate_edges={"route|20019A": {"edge-A"}},
        edge_vertices={"edge-A": {"vertex-A", "vertex-B"}},
    )
    assert errors and clean.get("decisions", []) == []


def test_unresolved_is_rebuilt_and_unknown_ids_rejected():
    payload = envelope()
    payload["unresolved"] = [
        {"candidateIds": ["route|20019A"], "reason": "需要更细裁图"},
        {"candidateIds": ["route|unknown"], "reason": "x", "extra": 1},
    ]
    clean, errors = check(payload)
    assert errors
    assert clean["unresolved"] == [
        {"candidateIds": ["route|20019A"], "reason": "需要更细裁图"}
    ]


@pytest.mark.parametrize("field", ["category", "medium", "role"])
@pytest.mark.parametrize("value", [[], {}])
def test_enum_wrong_json_type_is_rejected_without_exception(field, value):
    bad = envelope({**GOOD, field: value})
    clean, errors = check(bad)
    assert clean.get("decisions", []) == []
    assert errors


def test_huge_json_integer_is_rejected_without_exception():
    bad = envelope({**GOOD, "uncertainty": 10**1000})
    clean, errors = check(bad)
    assert clean.get("decisions", []) == []
    assert errors


def test_unresolved_duplicate_candidate_ids_are_rejected():
    payload = envelope()
    payload["unresolved"] = [
        {"candidateIds": ["route|20019A", "route|20019A"], "reason": "x"}
    ]
    clean, errors = check(payload)
    assert errors
    assert clean["unresolved"] == []
