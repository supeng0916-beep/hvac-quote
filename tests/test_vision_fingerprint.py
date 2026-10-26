"""V048-03: server-side task fingerprint binds candidate/image content."""
import copy
import hashlib
import io
import json

from PIL import Image

from hvac_quote.vision import (
    SCHEMA_VERSION,
    VisionAdvisor,
    VisionConfig,
    canonical_json,
    prepare_task,
    task_fingerprint,
)


def _config():
    return VisionConfig(
        provider="glm",
        model="glm-5.3-flash",
        base_url="https://gateway.example/v1",
        api_key="test-key",
        enabled=True,
    )


def _task():
    return {
        "sourceHash": "a" * 64,
        "fileId": "f1",
        "inspectionId": "insp-1",
        "inspectionContentHash": "b" * 64,
        "scope": {"view": "model", "floor": "首层"},
        "question": "请复核候选类别",
        "candidates": [
            {
                "candidateId": "route|1",
                "category": "pipe",
                "bbox": [0, 0, 10, 10],
                "nearbyText": ["DN25"],
            }
        ],
    }


def _good_decision():
    return {
        "candidateIds": ["route|1"],
        "category": "pipe",
        "medium": "drain",
        "role": "unknown",
        "evidenceCropIds": [],
        "linkedAnnotationIds": [],
        "reason": "测试",
        "uncertainty": 0.2,
        "conflicts": [],
    }


def _response(payload):
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(payload, ensure_ascii=False)
                }
            }
        ]
    }


def _png():
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _image(source_hash, crop_seed="one"):
    png = _png()
    manifest = {
        "schemaVersion": "vision-crop-2",
        "sourceHash": source_hash,
        "fileId": "f1",
        "cadBBox": [0, 0, 1, 1],
        "width": 8,
        "height": 8,
        "scale": 1,
        "handles": [],
        "maxSize": 800,
        "maxEntities": 100,
        "padding": 0.1,
        "requestedExpandBlocks": False,
        "blockExpansion": "collapsed",
        "candidateOverlays": [{"candidateId": crop_seed, "bbox": [0, 0, 1, 1]}],
        "truncated": False,
        "skippedEntities": 0,
        "drawnEntities": 0,
        "colorPolicy": "ezdxf-aci-white-background-v1",
        "pngSha256": hashlib.sha256(png).hexdigest(),
    }
    crop_id = hashlib.sha256(
        canonical_json(manifest).encode("utf-8")
    ).hexdigest()
    raw = {
        "png": png,
        "cropId": crop_id,
        "mediaType": "image/png",
        "manifest": manifest,
    }
    descriptor = {
        "cropId": crop_id,
        "mediaType": "image/png",
        "pngSha256": manifest["pngSha256"],
        "manifestHash": hashlib.sha256(
            canonical_json(manifest).encode("utf-8")
        ).hexdigest(),
    }
    return raw, descriptor


def test_caller_fingerprint_is_ignored_and_old_response_rejected():
    calls = []

    def transport(url, headers, payload, timeout):
        calls.append(payload)
        return _response(
            {
                "schemaVersion": SCHEMA_VERSION,
                "inputFingerprint": "previous-task-fingerprint",
                "decisions": [_good_decision()],
                "unresolved": [],
            }
        )

    task = {**_task(), "inputFingerprint": "previous-task-fingerprint"}
    result = VisionAdvisor(_config(), transport=transport).advise(task)
    assert len(calls) == 1
    assert result.status == "rejected"
    assert result.suggestion == {}


def test_candidate_content_changes_fingerprint():
    base = task_fingerprint(_task())
    changed_bbox = copy.deepcopy(_task())
    changed_bbox["candidates"][0]["bbox"] = [0, 0, 11, 10]
    changed_category = copy.deepcopy(_task())
    changed_category["candidates"][0]["category"] = "equipment"
    changed_question = {**_task(), "question": "另一个问题"}
    changed_source = {**_task(), "sourceHash": "c" * 64}
    changed_inspection = {**_task(), "inspectionContentHash": "d" * 64}
    values = {
        task_fingerprint(changed_bbox),
        task_fingerprint(changed_category),
        task_fingerprint(changed_question),
        task_fingerprint(changed_source),
        task_fingerprint(changed_inspection),
    }
    assert base not in values
    assert len(values) == 5


def test_candidate_order_does_not_change_fingerprint():
    task = _task()
    task["candidates"].append(
        {
            "candidateId": "route|2",
            "category": "pipe",
            "bbox": [20, 20, 30, 30],
        }
    )
    reordered = copy.deepcopy(task)
    reordered["candidates"] = list(reversed(reordered["candidates"]))
    assert task_fingerprint(task) == task_fingerprint(reordered)


def test_image_identity_mismatch_blocks_before_network():
    calls = []
    raw, _descriptor = _image("a" * 64)
    buffer = io.BytesIO()
    Image.new("RGB", (9, 9), "red").save(buffer, format="PNG")
    raw["png"] = buffer.getvalue()
    result = VisionAdvisor(
        _config(), transport=lambda *args: calls.append(args) or {}
    ).advise(
        {**_task(), "cropIds": [raw["cropId"]]}, images=[raw]
    )
    assert calls == []
    assert result.status == "degraded"
    assert result.reason == "image_identity_mismatch"


def test_image_content_changes_fingerprint_and_identity():
    raw_a, descriptor_a = _image("a" * 64, "one")
    raw_b, descriptor_b = _image("a" * 64, "two")
    assert raw_a["cropId"] != raw_b["cropId"]
    assert task_fingerprint(_task(), images=[descriptor_a]) != task_fingerprint(
        _task(), images=[descriptor_b]
    )


def test_same_bytes_and_manifest_are_stable():
    raw, descriptor = _image("a" * 64)
    assert raw["cropId"] == _image("a" * 64)[0]["cropId"]
    assert task_fingerprint(_task(), images=[descriptor]) == task_fingerprint(
        _task(), images=[_image("a" * 64)[1]]
    )


def test_prepare_task_drops_caller_hash_and_unknown_fields():
    prepared = prepare_task(
        {**_task(), "inputFingerprint": "caller", "extra": "drop"}
    )
    assert "inputFingerprint" not in prepared
    assert "extra" not in prepared
    assert prepared["sourceHash"] == "a" * 64


def _advise_with_capture(task, images, replay=None):
    calls = []

    def transport(url, headers, body, timeout):
        text = body["messages"][0]["content"][0]["text"]
        sent = json.loads(text.split("\n输入：", 1)[1])
        calls.append(sent)
        payload = replay or {
            "schemaVersion": SCHEMA_VERSION,
            "inputFingerprint": sent["inputFingerprint"],
            "decisions": [_good_decision()],
            "unresolved": [],
        }
        return _response(payload)

    result = VisionAdvisor(_config(), transport=transport).advise(
        task, images=images
    )
    return result, calls


def test_actual_advise_rejects_previous_response_after_valid_image_replacement():
    white, _ = _image("a" * 64, "white")
    black, _ = _image("a" * 64, "black")
    first_task = {**_task(), "cropIds": [white["cropId"]]}
    first, calls = _advise_with_capture(first_task, [white])
    assert first.status == "ok" and len(calls) == 1
    replay = {
        "schemaVersion": SCHEMA_VERSION,
        "inputFingerprint": calls[0]["inputFingerprint"],
        "decisions": [_good_decision()],
        "unresolved": [],
    }
    second_task = {**_task(), "cropIds": [black["cropId"]]}
    second, second_calls = _advise_with_capture(
        second_task, [black], replay
    )
    assert len(second_calls) == 1
    assert (
        second_calls[0]["inputFingerprint"]
        != calls[0]["inputFingerprint"]
    )
    assert second.status == "rejected"
    assert second.suggestion == {}


def test_image_order_changes_actual_task_fingerprint():
    first, _ = _image("a" * 64, "first")
    second, _ = _image("a" * 64, "second")
    task = {**_task(), "cropIds": [first["cropId"], second["cropId"]]}
    _, calls_a = _advise_with_capture(task, [first, second])
    reversed_task = {
        **_task(),
        "cropIds": [second["cropId"], first["cropId"]],
    }
    _, calls_b = _advise_with_capture(reversed_task, [second, first])
    assert (
        calls_a[0]["inputFingerprint"]
        != calls_b[0]["inputFingerprint"]
    )


def test_invalid_crop_declaration_never_calls_transport():
    task = {**_task(), "cropIds": ["declared-but-not-sent"]}
    result, calls = _advise_with_capture(task, [])
    assert calls == []
    assert result.status == "degraded"
    assert result.reason == "crop_declaration_mismatch"


def test_duplicate_candidate_ids_never_call_transport():
    task = {
        **_task(),
        "candidates": _task()["candidates"] * 2,
    }
    result, calls = _advise_with_capture(task, [])
    assert calls == []
    assert result.status == "degraded"
