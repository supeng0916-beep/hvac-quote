"""视觉模型适配层：只产出可回绑图元的建议，不参与算量或计价。

约束：
- 未配置端点/凭据时明确不可用，不做任何网络调用，也不返回模拟结果；
- 模型输出必须通过结构化校验（候选 ID、类别、边界、禁用字段）；
- 长度、数量、套价、保温、税费只由确定性程序计算；
- 超时、轮数、图片体积和用量都有硬上限，失败时降级到人工复核。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from PIL import Image, UnidentifiedImageError

SCHEMA_VERSION = "vision-suggestion-2"
PROMPT_VERSION = "2026-10-22.2"
DECISION_KEYS = {
    "candidateIds",
    "category",
    "medium",
    "role",
    "evidenceCropIds",
    "linkedAnnotationIds",
    "reason",
    "uncertainty",
    "conflicts",
    "splitProposal",
    "modelSuggestion",
}
DECISION_REQUIRED = DECISION_KEYS - {"splitProposal", "modelSuggestion"}
UNRESOLVED_KEYS = {"candidateIds", "reason"}
ENVELOPE_KEYS = {"schemaVersion", "inputFingerprint", "decisions", "unresolved"}

ALLOWED_CATEGORIES = {
    "equipment",
    "pipe",
    "wall",
    "leader",
    "annotation",
    "unknown",
}
ALLOWED_MEDIA = {
    "supply",
    "return",
    "condensate",
    "drain",
    "cooling",
    "makeup",
    "refrigerant",
    "duct",
    "unknown",
}
ALLOWED_ROLES = {"indoor", "outdoor", "unknown"}
FORBIDDEN_KEYS = {
    "length",
    "lengthm",
    "lengthraw",
    "quantity",
    "qty",
    "amount",
    "total",
    "price",
    "material",
    "installation",
    "tax",
    "totalbeforetax",
}

PROVIDER_DEFAULTS = {
    # 官方调用名会随版本变化；用户网关 ID 可能不同，必须在配置中显式指定。
    "glm": {"model": "glm-5.3-flash", "supportsImages": True},
    "deepseek": {"model": "deepseek-flash", "supportsImages": True},
    "qwen": {"model": "qwen3.8-max", "supportsImages": True},
}


class VisionUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class VisionConfig:
    provider: str = "qwen"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    enabled: bool = False
    api_style: str = "openai"
    anthropic_version: str = "2023-06-01"
    max_output_tokens: int = 2048
    thinking_disabled: bool = False
    messages_path: str = ""
    full_url: str = ""
    timeout_seconds: float = 45.0
    max_rounds: int = 1
    max_images: int = 4
    max_image_bytes: int = 4 * 1024 * 1024
    max_prompt_chars: int = 24_000
    response_format_json: bool = True

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "VisionConfig":
        import os

        env = os.environ if env is None else env
        provider = (env.get("HVAC_VISION_PROVIDER") or "qwen").strip().lower()
        explicit_model = (env.get("HVAC_VISION_MODEL") or "").strip()
        # Early deployments used a GLM label for a gateway actually serving
        # Qwen. Keep the explicit model/endpoint while correcting its identity.
        if provider == "glm" and explicit_model.lower().startswith("qwen"):
            provider = "qwen"
        defaults = PROVIDER_DEFAULTS.get(provider, {})
        return cls(
            provider=provider,
            model=explicit_model or str(defaults.get("model") or ""),
            base_url=(env.get("HVAC_VISION_BASE_URL") or "").strip(),
            api_key=(env.get("HVAC_VISION_API_KEY") or "").strip(),
            enabled=(env.get("HVAC_VISION_ENABLED") or "").strip()
            in {"1", "true", "yes", "on"},
            api_style=(env.get("HVAC_VISION_API_STYLE") or "openai")
            .strip()
            .lower(),
            anthropic_version=(
                env.get("HVAC_VISION_ANTHROPIC_VERSION") or "2023-06-01"
            ).strip(),
            max_output_tokens=int(
                env.get("HVAC_VISION_MAX_TOKENS") or 2048
            ),
            thinking_disabled=(
                env.get("HVAC_VISION_THINKING_DISABLED") or ""
            ).strip().lower()
            in {"1", "true", "yes", "on", "disabled"},
            messages_path=(env.get("HVAC_VISION_PATH") or "").strip(),
            full_url=(env.get("HVAC_VISION_ENDPOINT") or "").strip(),
            timeout_seconds=float(env.get("HVAC_VISION_TIMEOUT") or 45),
            max_rounds=int(env.get("HVAC_VISION_MAX_ROUNDS") or 1),
            max_images=int(env.get("HVAC_VISION_MAX_IMAGES") or 4),
            max_image_bytes=int(
                env.get("HVAC_VISION_MAX_IMAGE_BYTES") or 4 * 1024 * 1024
            ),
        )

    @property
    def available(self) -> bool:
        return bool(
            self.enabled and self.base_url and self.api_key and self.model
        )

    @property
    def endpoint(self) -> str:
        if self.full_url:
            return self.full_url
        base = self.base_url.rstrip("/")
        if self.messages_path:
            return base + "/" + self.messages_path.lstrip("/")
        if self.api_style == "anthropic":
            return base + "/v1/messages"
        return base + "/chat/completions"

    def safe_identity(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "apiStyle": self.api_style,
            "thinkingDisabled": self.thinking_disabled,
            "maxTokens": self.max_output_tokens,
            "baseUrlHost": _host(self.base_url),
            "available": self.available,
        }


def _host(url: str) -> str:
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        return urlparse(url).hostname or ""
    except Exception:  # noqa: BLE001
        return ""


def _sha(value: bytes | str) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def build_candidate_payload(
    inspection: dict, *, max_candidates: int = 200
) -> dict:
    """从检查报告抽取可回绑的候选，不含长度/金额。"""
    candidates = []
    for group in (inspection.get("equipmentGroups") or [])[:max_candidates]:
        candidates.append(
            {
                "candidateId": str(group.get("key") or ""),
                "category": "equipment",
                "model": str(group.get("modelHint") or ""),
                "role": str(group.get("role") or ""),
                "kind": str(group.get("equipmentKind") or ""),
                "layer": str(group.get("layer") or ""),
                "bbox": group.get("bbox"),
                "nearbyText": list(group.get("textHints") or [])[:8],
                "blockName": str(group.get("blockName") or ""),
                "sourceEntityIds": [
                    str(value) for value in group.get("handles") or []
                ],
            }
        )
    for group in (inspection.get("routeGroups") or [])[:max_candidates]:
        candidates.append(
            {
                "candidateId": str(group.get("key") or ""),
                "category": "pipe",
                "model": "",
                "role": str(group.get("role") or ""),
                "kind": str(group.get("category") or ""),
                "medium": str(group.get("medium") or ""),
                "layer": str(group.get("layer") or ""),
                "bbox": group.get("bbox"),
                "nearbyText": list(group.get("textHints") or [])[:8],
                "blockName": "",
                "sourceEntityIds": [
                    str(segment.get("entityId"))
                    for segment in group.get("segments") or []
                    if segment.get("entityId")
                ],
            }
        )
    return {
        "inspectionId": str(inspection.get("id") or ""),
        "fileId": str(inspection.get("fileId") or ""),
        "sourceHash": str(inspection.get("sourceHash") or ""),
        "candidates": candidates,
    }


def _prompt(task: dict) -> str:
    return (
        "你是暖通图纸复核助手。只根据给定的 CAD 候选、坐标和邻近文字提出分类建议，"
        "不要计算长度、数量、金额或价格。输出必须是 JSON，字段："
        "schemaVersion='%s'；inputFingerprint；decisions（数组，每项必须含 "
        "candidateIds、category∈%s、medium∈%s、role∈%s、"
        "linkedAnnotationIds、evidenceCropIds、reason、uncertainty、conflicts，"
        "可选 splitProposal={kind:'existingVertex',edgeId,vertexId}，"
        "可选 modelSuggestion={model,annotationId,candidateId,concern}；"
        "modelSuggestion 只在图面有原生文字依据时填写，annotationId 必须来自"
        "输入注记，没有依据就省略或置 null，禁止从 reason 推断型号）；"
        "unresolved（数组，每项只含 candidateIds、reason）。"
        "candidateIds 必须来自输入，禁止发明新 ID。"
        "分界建议只能引用输入中的 edgeId/vertexId；没有证据就放入 unresolved。\n"
        "inputFingerprint 必须原样回填输入中的值；evidenceCropIds 只能使用 "
        "输入提供的 cropId；linkedAnnotationIds 只能使用输入注记；"
        "没有提供边/顶点时不得提出 splitProposal。\n"
        "任务问题：%s\n输入：%s"
    ) % (
        SCHEMA_VERSION,
        sorted(ALLOWED_CATEGORIES),
        sorted(ALLOWED_MEDIA),
        sorted(ALLOWED_ROLES),
        str(task.get("question") or "请复核候选类别与关联"),
        json.dumps(task, ensure_ascii=False, separators=(",", ":")),
    )


def _scan_forbidden(value, path="") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).replace("_", "").lower()
            if normalized in FORBIDDEN_KEYS:
                found.append(f"{path}.{key}")
            found.extend(_scan_forbidden(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_scan_forbidden(item, f"{path}[{index}]"))
    return found


def canonical_json(value: dict | list) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _candidate_descriptor(item: dict) -> dict:
    return {
        "candidateId": str(item.get("candidateId") or ""),
        "category": str(item.get("category") or "unknown"),
        "model": str(item.get("model") or ""),
        "role": str(item.get("role") or "unknown"),
        "kind": str(item.get("kind") or ""),
        "medium": str(item.get("medium") or "unknown"),
        "layer": str(item.get("layer") or ""),
        "bbox": list(item.get("bbox") or []) if item.get("bbox") else [],
        "nearbyText": [str(value) for value in item.get("nearbyText") or []],
        "blockName": str(item.get("blockName") or ""),
        "sourceEntityIds": [
            str(value) for value in item.get("sourceEntityIds") or []
        ],
    }


def _annotation_descriptor(item: dict) -> dict:
    return {
        "annotationId": str(item.get("annotationId") or ""),
        "text": str(item.get("text") or ""),
        "bbox": list(item.get("bbox") or []) if item.get("bbox") else [],
        "sourceEntityIds": [
            str(value) for value in item.get("sourceEntityIds") or []
        ],
    }


def _edge_descriptor(item: dict) -> dict:
    vertices = [str(value) for value in item.get("vertexIds") or []]
    return {
        "edgeId": str(item.get("edgeId") or ""),
        "kind": str(item.get("kind") or ""),
        "candidateId": str(item.get("candidateId") or ""),
        "sourceEntityId": str(item.get("sourceEntityId") or ""),
        "geometryHash": str(item.get("geometryHash") or ""),
        "vertexIds": vertices,
    }


def _vertex_descriptor(item: dict) -> dict:
    position = item.get("position") or []
    return {
        "vertexId": str(item.get("vertexId") or ""),
        "parentEdgeId": str(item.get("parentEdgeId") or ""),
        "vertexIndex": int(item.get("vertexIndex") or 0),
        "position": [float(value) for value in position[:2]]
        if len(position) >= 2
        else [],
    }


def _task_list(task: dict, key: str) -> list:
    value = task.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"TASK_{key.upper()}_TYPE")
    return value


def _unique_ids(items: list, key: str, label: str) -> list[str]:
    values: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"TASK_{label.upper()}_TYPE")
        value = item.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"TASK_{label.upper()}_ID")
        values.append(value)
    if len(set(values)) != len(values):
        raise ValueError(f"TASK_{label.upper()}_DUPLICATE")
    return values


def _finite_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError):
        return False
    return math.isfinite(number)


def _string_list(value, code: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(code)
    return list(value)


def _bbox_value(value, code: str) -> list[float] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 4
        or not all(_finite_number(item) for item in value)
    ):
        raise ValueError(code)
    return list(value)


def _validate_task_inputs(task: dict) -> dict:
    if not isinstance(task, dict):
        raise ValueError("TASK_TYPE")
    for field in ("sourceHash", "fileHash"):
        value = task.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"TASK_{field.upper()}_TYPE")
    source_hash = task.get("sourceHash") or ""
    file_hash = task.get("fileHash") or ""
    if source_hash and file_hash and source_hash != file_hash:
        raise ValueError("SOURCE_HASH_CONFLICT")
    resolved_source = source_hash or file_hash
    if not resolved_source:
        raise ValueError("TASK_SOURCE_HASH_MISSING")
    for field in (
        "fileId",
        "inspectionId",
        "inspectionContentHash",
        "question",
        "view",
    ):
        value = task.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"TASK_{field.upper()}_TYPE")

    scope = task.get("scope")
    if scope is not None:
        if not isinstance(scope, dict):
            raise ValueError("TASK_SCOPE_TYPE")
        unknown_scope = set(scope) - {"view", "floor", "bbox", "layout"}
        if unknown_scope:
            raise ValueError("TASK_SCOPE_FIELD")
        for field in ("view", "floor", "layout"):
            value = scope.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError("TASK_SCOPE_VALUE")
        _bbox_value(scope.get("bbox"), "TASK_SCOPE_BBOX")

    candidates = _task_list(task, "candidates")
    candidate_ids = _unique_ids(candidates, "candidateId", "candidate")
    for item in candidates:
        _bbox_value(item.get("bbox"), "TASK_CANDIDATE_BBOX")
        _string_list(item.get("nearbyText"), "TASK_CANDIDATE_TEXT")
        _string_list(item.get("sourceEntityIds"), "TASK_CANDIDATE_SOURCE")
        for field in (
            "category",
            "medium",
            "role",
            "model",
            "kind",
            "layer",
            "blockName",
        ):
            if item.get(field) is not None and not isinstance(
                item.get(field), str
            ):
                raise ValueError(f"TASK_CANDIDATE_{field.upper()}")

    annotations = _task_list(task, "annotations")
    _unique_ids(annotations, "annotationId", "annotation")
    for item in annotations:
        _bbox_value(item.get("bbox"), "TASK_ANNOTATION_BBOX")
        _string_list(
            item.get("sourceEntityIds"), "TASK_ANNOTATION_SOURCE"
        )
        if item.get("text") is not None and not isinstance(
            item.get("text"), str
        ):
            raise ValueError("TASK_ANNOTATION_TEXT")
    edges = _task_list(task, "edges")
    _unique_ids(edges, "edgeId", "edge")
    for edge in edges:
        _string_list(edge.get("vertexIds"), "TASK_EDGE_VERTEX")
        if edge.get("candidateId") is not None and not isinstance(
            edge.get("candidateId"), str
        ):
            raise ValueError("TASK_EDGE_CANDIDATE_TYPE")
        for field in ("kind", "sourceEntityId", "geometryHash"):
            if edge.get(field) is not None and not isinstance(
                edge.get(field), str
            ):
                raise ValueError("TASK_EDGE_FIELD")
        if (
            edge.get("candidateId")
            and edge.get("candidateId") not in candidate_ids
        ):
            raise ValueError("TASK_EDGE_CANDIDATE")
    vertices = _task_list(task, "vertices")
    _unique_ids(vertices, "vertexId", "vertex")
    for vertex in vertices:
        index = vertex.get("vertexIndex")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("TASK_VERTEX_INDEX")
        if vertex.get("parentEdgeId") is not None and not isinstance(
            vertex.get("parentEdgeId"), str
        ):
            raise ValueError("TASK_VERTEX_PARENT")
        position = vertex.get("position")
        if position is not None and (
            not isinstance(position, list)
            or not all(_finite_number(value) for value in position)
        ):
            raise ValueError("TASK_VERTEX_POSITION")

    allowed_edges = _task_list(task, "allowedEdges")
    allowed_vertices = _task_list(task, "allowedVertices")
    if any(not isinstance(value, str) or not value for value in allowed_edges):
        raise ValueError("TASK_ALLOWED_EDGES")
    if any(
        not isinstance(value, str) or not value for value in allowed_vertices
    ):
        raise ValueError("TASK_ALLOWED_VERTICES")
    if len(set(allowed_edges)) != len(allowed_edges):
        raise ValueError("TASK_ALLOWED_EDGES_DUPLICATE")
    if len(set(allowed_vertices)) != len(allowed_vertices):
        raise ValueError("TASK_ALLOWED_VERTICES_DUPLICATE")
    return {
        "sourceHash": resolved_source,
        "candidates": candidates,
        "annotations": annotations,
        "edges": edges,
        "vertices": vertices,
        "allowedEdges": allowed_edges,
        "allowedVertices": allowed_vertices,
        "scope": scope,
    }


def prepare_task(task: dict, images: list[dict] | None = None) -> dict:
    """Build the exact immutable task sent to the model; never trust caller hashes."""
    validated = _validate_task_inputs(task)
    if images is None:
        images = []
    elif not isinstance(images, list):
        # Falsey non-list containers (0, False, {}, "") are invalid input, not
        # an omitted image list.  Silently coercing them would send a task the
        # caller never assembled.
        raise ValueError("TASK_IMAGES_TYPE")
    image_crop_ids: list[str] = []
    for item in images:
        if not isinstance(item, dict):
            raise ValueError("TASK_IMAGE_TYPE")
        crop_id = item.get("cropId")
        if not isinstance(crop_id, str) or not crop_id:
            raise ValueError("TASK_IMAGE_ID")
        if crop_id in image_crop_ids:
            raise ValueError("TASK_IMAGE_DUPLICATE")
        image_crop_ids.append(crop_id)
        for field in ("pngSha256", "manifestHash"):
            value = item.get(field)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"TASK_IMAGE_{field.upper()}")
        if str(item.get("mediaType") or "image/png") != "image/png":
            raise ValueError("TASK_IMAGE_TYPE")
    declared_crops = task.get("cropIds")
    if declared_crops is not None:
        if (
            not isinstance(declared_crops, list)
            or any(
                not isinstance(value, str) or not value
                for value in declared_crops
            )
            or len(set(declared_crops)) != len(declared_crops)
        ):
            raise ValueError("TASK_CROP_IDS")
    if image_crop_ids:
        if declared_crops is not None and declared_crops != image_crop_ids:
            raise ValueError("TASK_CROP_DECLARATION_MISMATCH")
    elif declared_crops:
        raise ValueError("TASK_CROP_DECLARATION_MISMATCH")

    candidates = [
        _candidate_descriptor(item) for item in validated["candidates"]
    ]
    annotations = [
        _annotation_descriptor(item) for item in validated["annotations"]
    ]
    edges = [_edge_descriptor(item) for item in validated["edges"]]
    vertices = [_vertex_descriptor(item) for item in validated["vertices"]]
    if not edges:
        edges = [
            {
                "edgeId": value,
                "kind": "parentPolyline",
                "candidateId": "",
                "sourceEntityId": "",
                "geometryHash": "",
                "vertexIds": [],
            }
            for value in validated["allowedEdges"]
        ]
    if not vertices:
        vertices = [
            {
                "vertexId": value,
                "parentEdgeId": "",
                "vertexIndex": 0,
                "position": [],
            }
            for value in validated["allowedVertices"]
        ]
    if len({item["edgeId"] for item in edges}) != len(edges):
        raise ValueError("TASK_EDGE_DUPLICATE")
    if len({item["vertexId"] for item in vertices}) != len(vertices):
        raise ValueError("TASK_VERTEX_DUPLICATE")
    scope = validated.get("scope") or {}
    if not scope and task.get("view") is not None:
        scope = {"view": str(task.get("view") or "")}
    return {
        "schemaVersion": SCHEMA_VERSION,
        "promptVersion": PROMPT_VERSION,
        "sourceHash": validated["sourceHash"],
        "fileId": str(task.get("fileId") or ""),
        "inspectionId": str(task.get("inspectionId") or ""),
        "inspectionContentHash": str(
            task.get("inspectionContentHash") or ""
        ),
        "scope": dict(scope),
        "question": str(task.get("question") or "请复核候选类别与关联"),
        "candidates": candidates,
        "annotations": annotations,
        "edges": edges,
        "vertices": vertices,
        "images": [
            {
                "cropId": str(item.get("cropId") or ""),
                "mediaType": str(item.get("mediaType") or "image/png"),
                "pngSha256": str(item.get("pngSha256") or ""),
                "manifestHash": str(item.get("manifestHash") or ""),
            }
            for item in images
        ],
    }


def _fingerprint_prepared(prepared: dict) -> str:
    payload = {
        "fingerprintVersion": "vision-input-2",
        "schemaVersion": SCHEMA_VERSION,
        "promptVersion": PROMPT_VERSION,
        "sourceHash": prepared["sourceHash"],
        "fileId": prepared["fileId"],
        "inspectionId": prepared["inspectionId"],
        "inspectionContentHash": prepared["inspectionContentHash"],
        "scope": prepared["scope"],
        "question": prepared["question"],
        "candidates": sorted(
            prepared["candidates"], key=lambda item: item["candidateId"]
        ),
        "annotations": sorted(
            prepared["annotations"], key=lambda item: item["annotationId"]
        ),
        "edges": sorted(prepared["edges"], key=lambda item: item["edgeId"]),
        "vertices": sorted(
            prepared["vertices"], key=lambda item: item["vertexId"]
        ),
        "images": prepared["images"],
    }
    return _sha(canonical_json(payload))


def _is_prepared_task(task: dict) -> bool:
    return bool(
        isinstance(task, dict)
        and task.get("schemaVersion") == SCHEMA_VERSION
        and task.get("promptVersion") == PROMPT_VERSION
        and isinstance(task.get("candidates"), list)
        and isinstance(task.get("images"), list)
    )


def task_fingerprint(task: dict, *, images: list[dict] | None = None) -> str:
    """Service-side hash of the complete immutable task and ordered images."""
    if _is_prepared_task(task) and images is None:
        prepared = task
    else:
        prepared = prepare_task(task, images)
    return _fingerprint_prepared(prepared)


def validate_suggestion(
    payload,
    *,
    candidate_ids: set[str],
    allowed_edges: set[str] | None = None,
    allowed_vertices: set[str] | None = None,
    allowed_crops: set[str] | None = None,
    allowed_annotations: set[str] | None = None,
    expected_fingerprint: str = "",
    candidate_edges: dict[str, set[str]] | None = None,
    edge_vertices: dict[str, set[str]] | None = None,
) -> tuple[dict, list[str]]:
    """Strict v2 validation; returns a freshly built whitelist object."""
    global_error = lambda code, message: ({}, [f"{code}: {message}"])
    if not isinstance(payload, dict):
        return global_error("ENVELOPE_TYPE", "响应不是 JSON 对象")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        return global_error("SCHEMA_MISMATCH", "schemaVersion 不匹配")
    actual_fingerprint = str(payload.get("inputFingerprint") or "")
    if not expected_fingerprint or actual_fingerprint != expected_fingerprint:
        return global_error(
            "FINGERPRINT_MISMATCH",
            f"inputFingerprint 不匹配（收到 {actual_fingerprint[:16]}）",
        )
    forbidden = _scan_forbidden(payload)
    if forbidden:
        return global_error(
            "FORBIDDEN_FIELD",
            "包含禁止的算量/金额字段：" + "、".join(forbidden[:5]),
        )
    if set(payload) != ENVELOPE_KEYS:
        return global_error(
            "ENVELOPE_KEYS",
            "顶层字段必须且只能是 "
            + "、".join(sorted(ENVELOPE_KEYS)),
        )
    decisions = payload.get("decisions")
    unresolved_payload = payload.get("unresolved")
    if not isinstance(decisions, list) or not isinstance(unresolved_payload, list):
        return global_error("ENVELOPE_TYPE", "decisions/unresolved 不是数组")
    if len(decisions) > 200 or len(unresolved_payload) > 200:
        return global_error("ENVELOPE_LIMIT", "建议或未解决项超过 200 条")

    allowed_edges = allowed_edges or set()
    allowed_vertices = allowed_vertices or set()
    allowed_crops = allowed_crops or set()
    allowed_annotations = allowed_annotations or set()
    candidate_edges = candidate_edges or {}
    edge_vertices = edge_vertices or {}

    def clean_decision(index: int, item) -> tuple[dict | None, list[str]]:
        errors: list[str] = []
        if not isinstance(item, dict):
            return None, [f"decisions[{index}] 不是对象"]
        if set(item) - DECISION_KEYS:
            return None, [
                f"decisions[{index}] 含未知字段："
                + "、".join(sorted(set(item) - DECISION_KEYS))
            ]
        missing = DECISION_REQUIRED - set(item)
        if missing:
            return None, [
                f"decisions[{index}] 缺少字段："
                + "、".join(sorted(missing))
            ]
        ids = item.get("candidateIds")
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 200
            or any(not isinstance(value, str) or not value for value in ids)
            or len(set(ids)) != len(ids)
        ):
            return None, [f"decisions[{index}] candidateIds 无效"]
        if any(value not in candidate_ids for value in ids):
            return None, [f"decisions[{index}] 引用未知候选"]
        category = item.get("category")
        medium = item.get("medium")
        role = item.get("role")
        if not isinstance(category, str) or category not in ALLOWED_CATEGORIES:
            return None, [f"decisions[{index}] 类别无效"]
        if not isinstance(medium, str) or medium not in ALLOWED_MEDIA:
            return None, [f"decisions[{index}] 介质无效"]
        if not isinstance(role, str) or role not in ALLOWED_ROLES:
            return None, [f"decisions[{index}] 角色无效"]
        crops = item.get("evidenceCropIds")
        if (
            not isinstance(crops, list)
            or len(crops) > 4
            or any(not isinstance(value, str) or not value for value in crops)
            or len(set(crops)) != len(crops)
            or any(value not in allowed_crops for value in crops)
        ):
            return None, [f"decisions[{index}] 裁图证据引用无效"]
        annotations = item.get("linkedAnnotationIds")
        if (
            not isinstance(annotations, list)
            or len(annotations) > 200
            or any(
                not isinstance(value, str) or not value
                for value in annotations
            )
            or len(set(annotations)) != len(annotations)
            or any(value not in allowed_annotations for value in annotations)
        ):
            return None, [f"decisions[{index}] 注记引用无效"]
        reason = item.get("reason")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 2000:
            return None, [f"decisions[{index}] reason 无效"]
        uncertainty = item.get("uncertainty")
        if isinstance(uncertainty, bool) or not isinstance(
            uncertainty, (int, float)
        ):
            return None, [f"decisions[{index}] uncertainty 无效"]
        try:
            uncertainty_value = float(uncertainty)
        except (OverflowError, ValueError, TypeError):
            return None, [f"decisions[{index}] uncertainty 无效"]
        if (
            not math.isfinite(uncertainty_value)
            or not 0.0 <= uncertainty_value <= 1.0
        ):
            return None, [f"decisions[{index}] uncertainty 无效"]
        conflicts = item.get("conflicts")
        if (
            not isinstance(conflicts, list)
            or len(conflicts) > 20
            or any(
                not isinstance(value, str) or not 1 <= len(value) <= 500
                for value in conflicts
            )
        ):
            return None, [f"decisions[{index}] conflicts 无效"]
        split = item.get("splitProposal")
        clean_split = None
        if split is not None:
            if not isinstance(split, dict) or set(split) != {
                "kind",
                "edgeId",
                "vertexId",
            }:
                return None, [f"decisions[{index}] splitProposal 结构无效"]
            if split.get("kind") != "existingVertex":
                return None, [f"decisions[{index}] splitProposal 类型无效"]
            edge_id = split.get("edgeId")
            vertex_id = split.get("vertexId")
            if (
                not isinstance(edge_id, str)
                or not isinstance(vertex_id, str)
                or not edge_id
                or not vertex_id
            ):
                return None, [f"decisions[{index}] splitProposal 引用无效"]
            if len(ids) != 1 or category != "pipe":
                return None, [f"decisions[{index}] splitProposal 仅限单管线候选"]
            if (
                not allowed_edges
                or not allowed_vertices
                or edge_id not in allowed_edges
                or vertex_id not in allowed_vertices
                or edge_id not in candidate_edges.get(ids[0], set())
                or vertex_id not in edge_vertices.get(edge_id, set())
            ):
                return None, [f"decisions[{index}] 分界引用未授权边或顶点"]
            clean_split = {
                "kind": "existingVertex",
                "edgeId": edge_id,
                "vertexId": vertex_id,
            }
        model_value = item.get("modelSuggestion")
        clean_model = None
        if model_value is not None:
            if not isinstance(model_value, dict) or set(model_value) != {
                "model",
                "annotationId",
                "candidateId",
                "concern",
            }:
                return None, [
                    f"decisions[{index}] modelSuggestion 结构无效"
                ]
            model_text = model_value.get("model")
            annotation_id = model_value.get("annotationId")
            model_candidate = model_value.get("candidateId")
            concern = model_value.get("concern")
            if (
                not isinstance(model_text, str)
                or not 1 <= len(model_text.strip()) <= 200
            ):
                return None, [
                    f"decisions[{index}] modelSuggestion.model 无效"
                ]
            if (
                annotation_id is not None
                and (
                    not isinstance(annotation_id, str)
                    or annotation_id not in allowed_annotations
                )
            ):
                return None, [
                    f"decisions[{index}] modelSuggestion.annotationId 无效"
                ]
            if (
                not isinstance(model_candidate, str)
                or model_candidate not in ids
            ):
                return None, [
                    f"decisions[{index}] modelSuggestion.candidateId 无效"
                ]
            if not isinstance(concern, str) or len(concern) > 500:
                return None, [
                    f"decisions[{index}] modelSuggestion.concern 无效"
                ]
            clean_model = {
                "model": model_text.strip(),
                "annotationId": annotation_id,
                "candidateId": model_candidate,
                "concern": concern,
            }
        return {
            "candidateIds": list(ids),
            "category": category,
            "medium": medium,
            "role": role,
            "evidenceCropIds": list(crops),
            "linkedAnnotationIds": list(annotations),
            "reason": reason,
            "uncertainty": uncertainty_value,
            "conflicts": list(conflicts),
            "splitProposal": clean_split,
            "modelSuggestion": clean_model,
        }, errors

    accepted: list[dict] = []
    errors: list[str] = []
    for index, item in enumerate(decisions):
        clean, item_errors = clean_decision(index, item)
        errors.extend(item_errors)
        if clean is not None and not item_errors:
            accepted.append(clean)

    unresolved: list[dict] = []
    for index, item in enumerate(unresolved_payload):
        if not isinstance(item, dict) or set(item) - UNRESOLVED_KEYS:
            errors.append(f"unresolved[{index}] 含未知字段或不是对象")
            continue
        ids = item.get("candidateIds")
        reason = item.get("reason")
        if (
            not isinstance(ids, list)
            or len(ids) > 200
            or any(not isinstance(value, str) or not value for value in ids)
            or len(set(ids)) != len(ids)
            or any(value not in candidate_ids for value in ids)
            or not isinstance(reason, str)
            or not 1 <= len(reason) <= 2000
        ):
            errors.append(f"unresolved[{index}] 结构或引用无效")
            continue
        unresolved.append({"candidateIds": list(ids), "reason": reason})

    def semantics(item: dict) -> tuple:
        split = item.get("splitProposal")
        model_value = item.get("modelSuggestion")
        return (
            item.get("category"),
            item.get("medium"),
            item.get("role"),
            canonical_json(split) if split else "",
            canonical_json(model_value) if model_value else "",
        )

    by_candidate: dict[str, set[tuple]] = {}
    for item in accepted:
        for candidate in item["candidateIds"]:
            by_candidate.setdefault(candidate, set()).add(semantics(item))
    conflicted = {
        candidate
        for candidate, values in by_candidate.items()
        if len(values) > 1
    }
    if conflicted:
        accepted = [
            item
            for item in accepted
            if not any(value in conflicted for value in item["candidateIds"])
        ]
        errors.append(
            "CONFLICTING_DECISIONS: 同一候选存在冲突结论，已全部转人工："
            + "、".join(sorted(conflicted)[:5])
        )

    deduped: dict[tuple, dict] = {}
    for item in accepted:
        key = (tuple(item["candidateIds"]), semantics(item))
        if key not in deduped:
            deduped[key] = item
            continue
        existing = deduped[key]
        for field in ("evidenceCropIds", "linkedAnnotationIds"):
            existing[field] = list(
                dict.fromkeys(existing[field] + item[field])
            )
    return (
        {
            "schemaVersion": SCHEMA_VERSION,
            "inputFingerprint": expected_fingerprint,
            "decisions": list(deduped.values()),
            "unresolved": unresolved,
        },
        errors,
    )


PNG_MAX_DIMENSION = 8192
PNG_MAX_PIXELS = 16_000_000


def _manifest_dimension(manifest: dict, key: str) -> int:
    value = manifest.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > PNG_MAX_DIMENSION
    ):
        raise ValueError("image_dimensions_invalid")
    return value


def _decode_png(png: bytes, manifest: dict) -> tuple[int, int]:
    """Decode-check a PNG before it can reach transport.

    A matching SHA-256 only proves the bytes are what the caller described; it
    does not prove the image is a readable, single-frame PNG within budget.
    """
    if len(png) < 33 or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("image_type_mismatch")
    expected_width = _manifest_dimension(manifest, "width")
    expected_height = _manifest_dimension(manifest, "height")
    try:
        with Image.open(io.BytesIO(png)) as probe:
            if probe.format != "PNG":
                raise ValueError("image_type_mismatch")
            width, height = probe.size
            if (
                isinstance(width, bool)
                or isinstance(height, bool)
                or not isinstance(width, int)
                or not isinstance(height, int)
                or width <= 0
                or height <= 0
            ):
                raise ValueError("image_dimensions_invalid")
            if width > PNG_MAX_DIMENSION or height > PNG_MAX_DIMENSION:
                raise ValueError("image_dimensions_invalid")
            if width * height > PNG_MAX_PIXELS:
                raise ValueError("image_pixels_exceeded")
            frames = int(getattr(probe, "n_frames", 1) or 1)
            if frames != 1:
                raise ValueError("image_frames_exceeded")
            if (width, height) != (expected_width, expected_height):
                raise ValueError("image_dimensions_mismatch")
            probe.verify()
        with Image.open(io.BytesIO(png)) as decoded:
            if decoded.format != "PNG":
                raise ValueError("image_type_mismatch")
            if decoded.size != (expected_width, expected_height):
                raise ValueError("image_dimensions_mismatch")
            decoded.load()
    except ValueError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        EOFError,
        Image.DecompressionBombError,
    ) as exc:
        raise ValueError("image_decode_failed") from exc
    return expected_width, expected_height


def _image_descriptors(
    raw_images: list[dict], source_hash: str
) -> list[dict]:
    descriptors: list[dict] = []
    seen_crops: set[str] = set()
    for item in raw_images:
        png = item.get("png")
        crop_id = str(item.get("cropId") or "")
        manifest = item.get("manifest")
        media_type = str(item.get("mediaType") or "image/png")
        if not isinstance(png, (bytes, bytearray)) or not png:
            raise ValueError("image_invalid")
        if not crop_id or not isinstance(manifest, dict):
            raise ValueError("image_identity_missing")
        png_sha = hashlib.sha256(bytes(png)).hexdigest()
        manifest_hash = hashlib.sha256(
            canonical_json(manifest).encode("utf-8")
        ).hexdigest()
        if (
            str(manifest.get("pngSha256") or "") != png_sha
            or str(manifest.get("sourceHash") or "") != source_hash
            or crop_id != manifest_hash
            or crop_id in seen_crops
            or media_type != "image/png"
        ):
            raise ValueError("image_identity_mismatch")
        _decode_png(bytes(png), manifest)
        seen_crops.add(crop_id)
        descriptors.append(
            {
                "cropId": crop_id,
                "mediaType": media_type,
                "pngSha256": png_sha,
                "manifestHash": manifest_hash,
            }
        )
    return descriptors


@dataclass
class VisionResult:
    status: str
    identity: dict
    suggestion: dict = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    attempts: int = 0
    durationMs: int = 0
    reason: str = ""
    rawText: str = ""
    rawMeta: dict = field(default_factory=dict)


class VisionAdvisor:
    """OpenAI 兼容的多模态适配器；端点与模型必须显式配置。"""

    def __init__(
        self,
        config: VisionConfig | None = None,
        *,
        transport: Callable[[str, dict, dict, float], dict] | None = None,
        before_request: Callable[[], None] | None = None,
    ) -> None:
        self.config = config or VisionConfig.from_env()
        self._transport = transport or _http_transport
        self._before_request = before_request

    def advise(
        self,
        task: dict,
        *,
        images: list[dict] | None = None,
    ) -> VisionResult:
        started = time.time()
        identity = self.config.safe_identity()
        if not self.config.available:
            return VisionResult(
                status="degraded",
                identity=identity,
                reason="not_configured",
                durationMs=0,
            )
        if not isinstance(task, dict):
            return VisionResult(
                status="degraded",
                identity=identity,
                reason="invalid_task",
            )
        if images is None:
            raw_images: list = []
        elif not isinstance(images, list):
            return VisionResult(
                status="degraded",
                identity=identity,
                reason="image_invalid",
            )
        else:
            raw_images = list(images)
        if any(not isinstance(item, dict) for item in raw_images):
            return VisionResult(
                status="degraded",
                identity=identity,
                reason="image_invalid",
            )
        for item in raw_images:
            png = item.get("png")
            if not isinstance(png, (bytes, bytearray)) or not png:
                return VisionResult(
                    status="degraded",
                    identity=identity,
                    reason="image_invalid",
                )
        if len(raw_images) > self.config.max_images:
            return VisionResult(
                status="degraded",
                identity=identity,
                reason="image_budget_exceeded",
            )
        if sum(len(item.get("png") or b"") for item in raw_images) > self.config.max_image_bytes:
            return VisionResult(
                status="degraded",
                identity=identity,
                reason="image_bytes_exceeded",
            )
        source_field = str(task.get("sourceHash") or "")
        file_field = str(task.get("fileHash") or "")
        if source_field and file_field and source_field != file_field:
            return VisionResult(
                status="degraded",
                identity=identity,
                reason="source_hash_mismatch",
            )
        source_hash = source_field or file_field
        actual_crops = [
            str(item.get("cropId") or "") for item in raw_images
        ]
        declared_crops = task.get("cropIds")
        if declared_crops is None:
            if actual_crops:
                return VisionResult(
                    status="degraded",
                    identity=identity,
                    reason="crop_declaration_mismatch",
                )
        elif (
            not isinstance(declared_crops, list)
            or declared_crops != actual_crops
        ):
            return VisionResult(
                status="degraded",
                identity=identity,
                reason="crop_declaration_mismatch",
            )
        try:
            descriptors = _image_descriptors(raw_images, source_hash)
        except (ValueError, TypeError, OverflowError, RecursionError) as exc:
            reason = str(exc)
            if reason not in {
                "image_invalid",
                "image_type_mismatch",
                "image_identity_missing",
                "image_identity_mismatch",
                "image_decode_failed",
                "image_dimensions_invalid",
                "image_dimensions_mismatch",
                "image_pixels_exceeded",
                "image_frames_exceeded",
            }:
                reason = f"invalid_image: {reason[:80]}"
            return VisionResult(
                status="degraded",
                identity=identity,
                reason=reason,
            )
        seen_crops = {item["cropId"] for item in descriptors}
        try:
            prepared = prepare_task(task, descriptors)
            fingerprint = _fingerprint_prepared(prepared)
            prepared["inputFingerprint"] = fingerprint
            prompt = _prompt(prepared)
        except (ValueError, TypeError, OverflowError, RecursionError) as exc:
            return VisionResult(
                status="degraded",
                identity=identity,
                reason=f"invalid_task: {str(exc)[:80]}",
            )
        task = prepared
        if len(prompt) > self.config.max_prompt_chars:
            return VisionResult(
                status="degraded",
                identity=identity,
                reason="prompt_too_large",
            )
        content: list[dict] = [{"type": "text", "text": prompt}]
        payload: dict
        endpoint = self.config.endpoint
        if self.config.api_style == "anthropic":
            for item in raw_images:
                data = item.get("png") or b""
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": str(
                                item.get("mediaType") or "image/png"
                            ),
                            "data": base64.b64encode(data).decode("ascii"),
                        },
                    }
                )
            payload = {
                "model": self.config.model,
                "max_tokens": self.config.max_output_tokens,
                "temperature": 0,
                "messages": [{"role": "user", "content": content}],
            }
            if self.config.thinking_disabled:
                payload["thinking"] = {"type": "disabled"}
            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.config.api_key,
                "anthropic-version": self.config.anthropic_version,
            }
        else:
            for item in raw_images:
                data = item.get("png") or b""
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(data).decode("ascii")
                        },
                    }
                )
            payload = {
                "model": self.config.model,
                "messages": [{"role": "user", "content": content}],
                "temperature": 0,
            }
            if self.config.response_format_json:
                payload["response_format"] = {"type": "json_object"}
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            }
        attempts = 0
        last_error = ""
        for _ in range(max(1, self.config.max_rounds)):
            # Recheck mutable authorization at the actual outbound boundary,
            # after image preparation and for every possible request round.
            if self._before_request is not None:
                try:
                    self._before_request()
                except VisionUnavailable as exc:
                    return VisionResult(
                        status="degraded",
                        identity=identity,
                        reason=exc.reason,
                        attempts=attempts,
                        durationMs=int((time.time() - started) * 1000),
                    )
            attempts += 1
            try:
                response = self._transport(
                    endpoint, headers, payload, self.config.timeout_seconds
                )
            except TimeoutError:
                last_error = "timeout"
                break
            except Exception as exc:  # noqa: BLE001
                last_error = type(exc).__name__
                break
            raw_text = _extract_text(response, self.config.api_style)
            raw_meta = _response_meta(response, self.config.api_style)
            raw = _extract_json(response, self.config.api_style)
            if raw is None:
                return VisionResult(
                    status="degraded",
                    identity=identity,
                    reason="invalid_json",
                    attempts=attempts,
                    durationMs=int((time.time() - started) * 1000),
                    rawText=raw_text[:4000],
                    rawMeta=raw_meta,
                )
            candidate_ids = {
                item["candidateId"]
                for item in task.get("candidates") or []
                if item.get("candidateId")
            }
            candidate_edges: dict[str, set[str]] = {}
            edge_vertices: dict[str, set[str]] = {}
            for edge in task.get("edges") or []:
                edge_id = edge.get("edgeId")
                if not edge_id:
                    continue
                edge_vertices[edge_id] = set(edge.get("vertexIds") or [])
                if edge.get("candidateId"):
                    candidate_edges.setdefault(edge["candidateId"], set()).add(
                        edge_id
                    )
            suggestion, rejected = validate_suggestion(
                raw,
                candidate_ids=candidate_ids,
                allowed_edges={
                    edge["edgeId"]
                    for edge in task.get("edges") or []
                    if edge.get("edgeId")
                },
                allowed_vertices={
                    vertex["vertexId"]
                    for vertex in task.get("vertices") or []
                    if vertex.get("vertexId")
                },
                allowed_crops=seen_crops,
                allowed_annotations={
                    annotation["annotationId"]
                    for annotation in task.get("annotations") or []
                    if annotation.get("annotationId")
                },
                expected_fingerprint=str(task["inputFingerprint"]),
                candidate_edges=candidate_edges,
                edge_vertices=edge_vertices,
            )
            if rejected and not suggestion.get("decisions"):
                status = "rejected"
            elif rejected:
                status = "partial"
            else:
                status = "ok"
            return VisionResult(
                status=status,
                identity=identity,
                suggestion=suggestion,
                rejected=rejected,
                usage=_usage(response, self.config.api_style),
                attempts=attempts,
                durationMs=int((time.time() - started) * 1000),
            )
        return VisionResult(
            status="degraded",
            identity=identity,
            reason=last_error or "failed",
            attempts=attempts,
            durationMs=int((time.time() - started) * 1000),
        )


def _usage(response, api_style: str) -> dict:
    if not isinstance(response, dict):
        return {}
    usage = response.get("usage") or {}
    if api_style == "anthropic":
        return {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        }
    return usage


def _extract_text(response, api_style: str = "openai") -> str:
    if not isinstance(response, dict):
        return ""
    if api_style == "anthropic":
        blocks = response.get("content") or []
        return "".join(
            str(part.get("text") or "")
            for part in blocks
            if isinstance(part, dict) and part.get("type") == "text"
        )
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        )
    return content if isinstance(content, str) else ""


def _response_meta(response, api_style: str) -> dict:
    if not isinstance(response, dict):
        return {"type": type(response).__name__}
    if api_style == "anthropic":
        blocks = response.get("content") or []
        return {
            "blockTypes": [
                str(part.get("type"))
                for part in blocks
                if isinstance(part, dict)
            ],
            "stopReason": response.get("stop_reason"),
            "usage": response.get("usage") or {},
        }
    return {"keys": list(response.keys())[:10]}


def _extract_json(response, api_style: str = "openai") -> dict | None:
    content = _extract_text(response, api_style)
    if not isinstance(content, str):
        return None
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _http_transport(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TimeoutError("vision request timeout") from exc
        raise
    except TimeoutError:
        raise


__all__ = [
    "SCHEMA_VERSION",
    "PROMPT_VERSION",
    "VisionAdvisor",
    "VisionConfig",
    "VisionResult",
    "VisionUnavailable",
    "build_candidate_payload",
    "validate_suggestion",
]
