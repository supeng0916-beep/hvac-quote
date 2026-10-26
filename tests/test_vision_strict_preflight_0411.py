"""0.4.11 R4: strict task preflight before fingerprint/prompt/transport."""
import copy

import pytest

from hvac_quote.vision import VisionAdvisor, VisionConfig

TASK = {
    "sourceHash": "b" * 64,
    "fileId": "f1",
    "inspectionId": "i1",
    "inspectionContentHash": "c" * 64,
    "scope": {"view": "model", "floor": "1"},
    "question": "Classify this candidate",
    "candidates": [
        {
            "candidateId": "r1",
            "category": "pipe",
            "bbox": [0, 0, 10, 10],
            "nearbyText": ["DN25"],
            "sourceEntityIds": ["full-instance-1"],
        }
    ],
}
CONFIG = VisionConfig(
    provider="offline",
    model="offline",
    base_url="https://invalid.example",
    api_key="offline-test-not-a-secret",
    enabled=True,
    max_rounds=1,
)


def _task(field, value, *, target="candidate"):
    task = copy.deepcopy(TASK)
    if target == "candidate":
        task["candidates"][0][field] = value
    elif target == "annotation":
        task["annotations"] = [
            {
                "annotationId": "n1",
                "text": "DN25",
                "bbox": [0, 0, 1, 1],
                "sourceEntityIds": ["text-1"],
                field: value,
            }
        ]
    elif target == "edge":
        task["edges"] = [
            {
                "edgeId": "e1",
                "candidateId": "r1",
                "vertexIds": ["v1"],
                field: value,
            }
        ]
    else:
        task[field] = value
    return task


MALFORMED = {
    "nearbyText_string": _task("nearbyText", "DN25"),
    "sourceEntityIds_string": _task("sourceEntityIds", "instance-1"),
    "nearbyText_object": _task("nearbyText", {"DN25": "ignored"}),
    "annotation_bbox_string": _task("bbox", "0123", target="annotation"),
    "annotation_bbox_nan": _task("bbox", [0, 0, float("nan"), 1], target="annotation"),
    "annotation_source_object": _task(
        "sourceEntityIds", {"text-1": "ignored"}, target="annotation"
    ),
    "annotation_text_object": _task("text", {"bad": "text"}, target="annotation"),
    "edge_vertexIds_string": _task("vertexIds", "v1", target="edge"),
    "source_hash_object": _task("sourceHash", {"bad": "identity"}, target="task"),
    "file_id_object": _task("fileId", {"bad": "identity"}, target="task"),
    "scope_string": _task("scope", "model", target="task"),
    "scope_bbox_inf": _task(
        "scope", {"view": "model", "bbox": [0, 0, float("inf"), 1]}, target="task"
    ),
}

CONTROLS = {
    "bbox_hugeint": _task("bbox", [0, 0, 10**1000, 1]),
    "bbox_nan": _task("bbox", [0, 0, float("nan"), 1]),
    "candidate_id_object": _task("candidateId", {"bad": "id"}),
    "candidates_string": _task("candidates", "bad", target="task"),
    "duplicate_annotations": _task(
        "annotations", [{"annotationId": "n1"}] * 2, target="task"
    ),
    "duplicate_edges": _task("edges", [{"edgeId": "e1"}] * 2, target="task"),
    "duplicate_vertices": _task(
        "vertices", [{"vertexId": "v1"}] * 2, target="task"
    ),
}


def _advise(task):
    calls = []

    def transport(*args, **kwargs):
        calls.append(True)
        raise AssertionError("transport must not be called")

    result = VisionAdvisor(CONFIG, transport=transport).advise(task)
    return result, calls


@pytest.mark.parametrize("name", list(MALFORMED), ids=list(MALFORMED))
def test_malformed_task_is_degraded_without_transport(name):
    result, calls = _advise(MALFORMED[name])
    assert calls == []
    assert result.status == "degraded"
    assert result.reason.startswith("invalid_task")


@pytest.mark.parametrize("name", list(CONTROLS), ids=list(CONTROLS))
def test_rejecting_control_is_degraded_without_transport(name):
    result, calls = _advise(CONTROLS[name])
    assert calls == []
    assert result.status == "degraded"
