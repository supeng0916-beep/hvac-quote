"""V01：视觉适配层离线测试（无凭据、无真实 API 调用）。"""

import copy
import hashlib
import io
import json

from PIL import Image

from hvac_quote.vision import (
    SCHEMA_VERSION,
    canonical_json,
    VisionAdvisor,
    VisionConfig,
    build_candidate_payload,
    task_fingerprint,
    validate_suggestion,
)


def _config(**overrides):
    values = {
        "provider": "glm",
        "model": "glm-5.3-flash",
        "base_url": "https://gateway.example/v1",
        "api_key": "test-key",
        "enabled": True,
    }
    values.update(overrides)
    return VisionConfig(**values)


def _task():
    return {
        "taskId": "task-1",
        "fileHash": "abc",
        "inspectionId": "insp-1",
        "view": "model",
        "candidates": [
            {"candidateId": "route|1", "category": "pipe", "layer": "0"},
            {"candidateId": "equipment|2", "category": "equipment", "model": "KFR-50GW"},
        ],
        "allowedEdges": ["edge-1", "edge-2"],
    }


def _normalized(payload):
    payload = copy.deepcopy(payload)
    payload.setdefault("unresolved", [])
    for decision in payload.get("decisions") or []:
        if not isinstance(decision, dict):
            continue
        decision.setdefault("medium", "unknown")
        decision.setdefault("role", "unknown")
        decision.setdefault("evidenceCropIds", [])
        decision.setdefault("linkedAnnotationIds", [])
        decision.setdefault("reason", "测试建议")
        decision.setdefault("uncertainty", 0.5)
        decision.setdefault("conflicts", [])
    return payload


def _image_payload(task, crop_name="c1"):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="PNG")
    png = buffer.getvalue()
    manifest = {
        "schemaVersion": "vision-crop-2",
        "renderer": "approximate-linework-v2",
        "sourceHash": task.get("fileHash") or task.get("sourceHash") or "",
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
        "candidateOverlays": [],
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


def _response(payload):
    return {
        "choices": [
            {"message": {"content": json.dumps(_normalized(payload), ensure_ascii=False)}}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def test_unconfigured_advisor_does_not_call_transport():
    calls = []

    def transport(*args):
        calls.append(args)
        return {}

    advisor = VisionAdvisor(
        VisionConfig.from_env({}), transport=transport
    )
    result = advisor.advise(_task())
    assert result.status == "degraded"
    assert result.reason == "not_configured"
    assert calls == []


def test_valid_suggestion_is_accepted_and_usage_recorded():
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "inputFingerprint": task_fingerprint(_task()),
        "decisions": [
            {
                "candidateIds": ["route|1"],
                "category": "pipe",
                "medium": "drain",
                "reason": "邻近 DN25 且与设备不连接",
                "uncertainty": 0.2,
            }
        ],
        "unresolved": [],
    }
    advisor = VisionAdvisor(_config(), transport=lambda *a: _response(payload))
    result = advisor.advise(_task())
    assert result.status == "ok"
    assert result.attempts == 1
    assert result.usage["prompt_tokens"] == 10
    assert result.suggestion["decisions"][0]["medium"] == "drain"


def test_unknown_candidate_and_forbidden_amount_are_rejected():
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "inputFingerprint": task_fingerprint(_task()),
        "decisions": [
            {
                "candidateIds": ["route|unknown"],
                "category": "pipe",
            }
        ],
    }
    advisor = VisionAdvisor(_config(), transport=lambda *a: _response(payload))
    result = advisor.advise(_task())
    assert result.status == "rejected"
    assert result.suggestion.get("decisions", []) == []
    assert any("未知候选" in item for item in result.rejected)

    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "inputFingerprint": task_fingerprint(_task()),
        "decisions": [
            {
                "candidateIds": ["route|1"],
                "category": "pipe",
                "lengthM": 10.5,
            }
        ],
    }
    advisor = VisionAdvisor(_config(), transport=lambda *a: _response(payload))
    result = advisor.advise(_task())
    assert result.status == "rejected"
    assert any("禁止" in item for item in result.rejected)


def test_split_must_reference_known_edges():
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "inputFingerprint": task_fingerprint(_task()),
        "decisions": [
            {
                "candidateIds": ["route|1"],
                "category": "pipe",
                "splitProposal": {"edgeId": "edge-999"},
            }
        ],
    }
    advisor = VisionAdvisor(_config(), transport=lambda *a: _response(payload))
    result = advisor.advise(_task())
    assert result.status == "rejected"
    assert any("splitProposal" in item for item in result.rejected)


def test_timeout_degrades_without_fabricating_result():
    def transport(*args):
        raise TimeoutError("slow")

    advisor = VisionAdvisor(_config(), transport=transport)
    result = advisor.advise(_task())
    assert result.status == "degraded"
    assert result.reason == "timeout"
    assert result.suggestion == {}


def test_budget_limits_images_and_prompt():
    advisor = VisionAdvisor(
        _config(max_images=0), transport=lambda *a: _response({})
    )
    result = advisor.advise(_task(), images=[{"png": b"x", "cropId": "c1"}])
    assert result.status == "degraded"
    assert result.reason == "image_budget_exceeded"


def test_candidate_payload_excludes_lengths_and_amounts():
    inspection = {
        "id": "insp-1",
        "fileId": "f1",
        "counts": {"equipment": 3},
        "equipmentGroups": [
            {
                "key": "equipment|a",
                "modelHint": "KFR-50GW",
                "role": "indoor",
                "layer": "AC",
                "bbox": [0, 0, 10, 10],
                "textHints": ["KFR-50GW"],
                "quantity": 2,
            }
        ],
        "routeGroups": [
            {
                "key": "route|b",
                "spec": "22.23",
                "lengthRaw": 100,
                "lengthM": 0.1,
                "layer": "冷媒",
                "bbox": [0, 0, 5, 5],
            }
        ],
    }
    payload = build_candidate_payload(inspection)
    text = json.dumps(payload, ensure_ascii=False)
    assert "lengthM" not in text
    assert "quantity" not in text
    assert payload["candidates"][0]["candidateId"] == "equipment|a"


def test_anthropic_style_request_and_response():
    captured = {}

    def sent_fingerprint():
        text = captured["payload"]["messages"][0]["content"][0]["text"]
        sent = json.loads(text.split("\n输入：", 1)[1])
        return sent["inputFingerprint"]

    def transport(url, headers, payload, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return {
            "content": [
                {
                    "type": "text",
                    "text": "```json\n"
                    + json.dumps(
                        {
                            "schemaVersion": SCHEMA_VERSION,
                            "inputFingerprint": sent_fingerprint(),
                            "decisions": [
                                {
                                    "candidateIds": ["route|1"],
                                    "category": "pipe",
                                    "medium": "drain",
                                    "reason": "图中文字 DN25",
                                    "role": "unknown",
                                    "evidenceCropIds": [],
                                    "linkedAnnotationIds": [],
                                    "uncertainty": 0.2,
                                    "conflicts": [],
                                }
                            ],
                            "unresolved": [],
                        },
                        ensure_ascii=False,
                    )
                    + "\n```",
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 30},
        }

    config = _config(
        api_style="anthropic",
        base_url="https://gateway.example/anthropic",
        model="qwen3.8-max",
    )
    raw_image, _descriptor = _image_payload(_task())
    image_task = {**_task(), "cropIds": [raw_image["cropId"]]}
    advisor = VisionAdvisor(config, transport=transport)
    result = advisor.advise(image_task, images=[raw_image])
    assert result.status == "ok"
    assert captured["url"].endswith("/anthropic/v1/messages")
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    payload = captured["payload"]
    assert payload["model"] == "qwen3.8-max"
    assert payload["max_tokens"] == 2048
    assert "response_format" not in payload
    blocks = payload["messages"][0]["content"]
    assert blocks[0]["type"] == "text"
    image_block = next(block for block in blocks if block["type"] == "image")
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == "image/png"
    assert result.usage["input_tokens"] == 100
    assert result.suggestion["decisions"][0]["medium"] == "drain"


def test_anthropic_endpoint_can_be_overridden():
    config = _config(
        api_style="anthropic",
        base_url="https://gateway.example/anthropic",
        messages_path="/custom/messages",
    )
    assert config.endpoint == "https://gateway.example/anthropic/custom/messages"


def test_full_endpoint_override():
    config = _config(
        api_style="anthropic",
        base_url="https://gateway.example/anthropic",
        full_url="https://gateway.example/custom/v1/messages",
    )
    assert config.endpoint == "https://gateway.example/custom/v1/messages"


def test_anthropic_thinking_can_be_disabled():
    captured = {}

    def transport(url, headers, payload, timeout):
        captured["payload"] = payload
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "schemaVersion": SCHEMA_VERSION,
                            "inputFingerprint": task_fingerprint(_task()),
                            "decisions": [],
                            "unresolved": [],
                        }
                    ),
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    config = _config(
        api_style="anthropic",
        thinking_disabled=True,
        max_output_tokens=2048,
    )
    advisor = VisionAdvisor(config, transport=transport)
    advisor.advise(_task())
    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert captured["payload"]["max_tokens"] == 2048


def _payload_with(fp, decisions, unresolved=None):
    return _normalized({
        "schemaVersion": SCHEMA_VERSION,
        "inputFingerprint": fp,
        "decisions": decisions,
        "unresolved": unresolved or [],
    })


def test_fingerprint_mismatch_rejects_everything():
    payload = _payload_with(
        "wrong-fingerprint",
        [{"candidateIds": ["route|1"], "category": "pipe"}],
    )
    advisor = VisionAdvisor(_config(), transport=lambda *a: _response(payload))
    result = advisor.advise(_task())
    assert result.status == "rejected"
    assert result.suggestion.get("decisions", []) == []
    assert any("inputFingerprint" in item for item in result.rejected)


def test_unprovided_crop_and_annotation_refs_are_rejected():
    fingerprint = task_fingerprint(_task())
    payload = _payload_with(
        fingerprint,
        [
            {
                "candidateIds": ["route|1"],
                "category": "pipe",
                "evidenceCropIds": ["crop-unknown"],
            }
        ],
    )
    advisor = VisionAdvisor(_config(), transport=lambda *a: _response(payload))
    result = advisor.advise(_task())
    assert any("裁图证据" in item for item in result.rejected)

    payload = _payload_with(
        fingerprint,
        [
            {
                "candidateIds": ["route|1"],
                "category": "pipe",
                "linkedAnnotationIds": ["annotation-unknown"],
            }
        ],
    )
    advisor = VisionAdvisor(_config(), transport=lambda *a: _response(payload))
    result = advisor.advise(_task())
    assert any("注记" in item for item in result.rejected)


def test_empty_allowed_edges_rejects_split():
    task = dict(_task())
    task["allowedEdges"] = []
    payload = _payload_with(
        task_fingerprint(task),
        [
            {
                "candidateIds": ["route|1"],
                "category": "pipe",
                "splitProposal": {
                    "kind": "existingVertex",
                    "edgeId": "edge-1",
                    "vertexId": "vertex-1",
                },
            }
        ],
    )
    advisor = VisionAdvisor(_config(), transport=lambda *a: _response(payload))
    result = advisor.advise(task)
    assert any("未授权边或顶点" in item for item in result.rejected)


def test_conflicting_decisions_for_same_candidate_are_rejected():
    fingerprint = task_fingerprint(_task())
    payload = _payload_with(
        fingerprint,
        [
            {"candidateIds": ["route|1"], "category": "pipe", "medium": "drain"},
            {
                "candidateIds": ["route|1"],
                "category": "equipment",
                "medium": "unknown",
            },
        ],
    )
    advisor = VisionAdvisor(_config(), transport=lambda *a: _response(payload))
    result = advisor.advise(_task())
    assert result.suggestion.get("decisions", []) == []
    assert any("冲突结论" in item for item in result.rejected)
