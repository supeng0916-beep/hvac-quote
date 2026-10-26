"""通用图纸检查：从上传的 DWG/DXF 生成候选设备、候选管线和疑点。

这是“可解释的候选”，不是自动采用的工程量：
- 设备先按图元实例确定型号（图面文字/图块属性），再按型号和角色归组；
  同名图块的不同型号不会合并，匿名图块需邻近型号支持才成为候选；
- 管线按图层关键字分组并量取图面长度；
- 比例缺失、无法量取、型号不清都进入疑点，绝不猜数。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from hvac_cad.reader import CadReadError, read_drawing
from hvac_cad.route_segments import (
    RouteSegmentError, extract_triangle_boundary, project_anchor_to_segment,
)

from .vocabulary import VOCABULARY

EQUIPMENT_LAYER_MARKERS = VOCABULARY["equipment_layer_markers"]
EQUIPMENT_TEXT_MARKERS = VOCABULARY["equipment_text_markers"]
PIPE_LAYER_MARKERS = VOCABULARY["pipe_layer_markers"]
WATER_SYSTEM_HINTS = VOCABULARY["water_system_hints"]
WATER_ROUTE_MARKERS = VOCABULARY["water_route_markers"]
ROUTE_TYPES = {
    "LINE",
    "LWPOLYLINE",
    "POLYLINE",
    "ARC",
    "CIRCLE",
    "SPLINE",
    "ELLIPSE",
}
TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB"}
FLOOR_RE = re.compile(
    r"(?:地下|地上)?\s*([一二三四五六七八九十百\d]+)\s*(?:层|[Ff](?![A-Za-z]))"
)
FLOOR_TITLE_MARKERS = VOCABULARY["floor_title_markers"]
FRAME_LAYER_MARKERS = VOCABULARY["frame_layer_markers"]
PAIR_DIAMETER_RE = re.compile(
    r"(?:φ|Φ|DN|D)?\s*(\d+(?:\.\d+)?)\s*(?:mm)?\s*[×xX*]\s*"
    r"(?:φ|Φ|DN|D)?\s*(\d+(?:\.\d+)?)\s*(?:mm)?",
    re.IGNORECASE,
)
SINGLE_DIAMETER_RE = re.compile(
    r"(?:φ|Φ|DN)\s*(\d+(?:\.\d+)?)|(?<!\d)(\d+(?:\.\d+)?)\s*mm",
    re.IGNORECASE,
)
PIPE_TEXT_MARKERS = VOCABULARY["pipe_text_markers"]
MODEL_TEXT_MARKERS = VOCABULARY["model_text_markers"]
MODEL_CODE_MARKERS = VOCABULARY["model_code_markers"]
INSPECTION_ENGINE_VERSION = "2026-10-23.5"
WATER_EQUIPMENT_KINDS = {
    "冷水机组",
    "水泵",
    "冷却塔",
    "风机盘管",
    "空调箱",
    "阀门",
    "过滤器",
    "分水器",
    "集水器",
    "膨胀水箱",
}
EQUIPMENT_KIND_RULES = (
    ("冷水机组", ("冷水机组", "螺杆机组", "离心机组", "CHILLER")),
    ("水泵", ("水泵", "循环泵", "增压泵", "补水泵", "PUMP")),
    ("冷却塔", ("冷却塔", "COOLING TOWER")),
    ("风机盘管", ("FP-", "FP_", "风机盘管", "FAN COIL")),
    ("空调箱", ("空调箱", "组合式空调", "AHU")),
    ("阀门", ("平衡阀", "蝶阀", "闸阀", "截止阀", "电动阀", "阀门", "VALVE")),
    ("过滤器", ("过滤器", "Y型过滤", "FILTER")),
    ("分水器", ("分水器",)),
    ("集水器", ("集水器",)),
    ("膨胀水箱", ("膨胀水箱",)),
)
ROUTE_MEDIUM_RULES = (
    ("condensate", ("冷凝水", "condensate")),
    ("drain", ("排水", "drain", "地漏")),
    ("supply", ("供水", "给水", "supply", "送水")),
    ("return", ("回水", "return")),
    ("cooling", ("冷却水", "cooling water", "cws")),
    ("makeup", ("补水", "makeup")),
    ("refrigerant", ("冷媒", "铜管", "液管", "气管")),
    ("duct", ("风管", "送风", "回风", "排风")),
)


def _floor_label(view: str, texts: list[str]) -> str:
    match = FLOOR_RE.search(view or "")
    if match:
        return match.group(1) + "层"
    counts: Counter[str] = Counter()
    for text in texts:
        for match in FLOOR_RE.finditer(text or ""):
            counts[match.group(1) + "层"] += 1
    if counts:
        return counts.most_common(1)[0][0]
    return view or "model"


def _floor_name(text: str) -> str:
    value = _text(text)
    if not value:
        return ""
    if "首层" in value or "底层" in value:
        return "首层"
    match = FLOOR_RE.search(value)
    if not match:
        return ""
    label = match.group(1) + "层"
    if "地下" in value[: match.end()]:
        label = "地下" + label
    return label


def _floor_anchors(texts: list[dict]) -> list[dict]:
    anchors = []
    for item in texts:
        label = _floor_name(str(item.get("text") or ""))
        bbox = item.get("bbox")
        if not label or not bbox:
            continue
        center = _bbox_center(bbox)
        if center is None:
            continue
        anchors.append(
            {
                "label": label,
                "text": str(item.get("text") or ""),
                "bbox": [float(value) for value in bbox],
                "center": center,
                "handle": item.get("handle"),
            }
        )
    return anchors


def _union_bbox(boxes: list[list[float]]) -> list[float] | None:
    boxes = [box for box in boxes if box and len(box) == 4]
    if not boxes:
        return None
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def _split_floor_regions(
    items: list[dict],
    span_x: float,
    span_y: float,
    *,
    depth: int = 0,
) -> list[dict]:
    """按图面空白递归切分模型空间，得到楼层候选区域。"""
    if len(items) < 8 or depth >= 7:
        bbox = _union_bbox([item["bbox"] for item in items])
        return [{"items": items, "bbox": bbox}] if bbox else []
    best = None
    for axis, span in ((0, span_x), (1, span_y)):
        ordered = sorted(items, key=lambda item: item["center"][axis])
        for previous, current in zip(ordered, ordered[1:]):
            gap = current["center"][axis] - previous["center"][axis]
            if gap <= max(span * 0.07, 1.0):
                continue
            score = gap / max(span, 1.0)
            if best is None or score > best[0]:
                split_at = (previous["center"][axis] + current["center"][axis]) / 2
                left = [
                    item
                    for item in items
                    if item["center"][axis] < split_at
                ]
                right = [
                    item
                    for item in items
                    if item["center"][axis] >= split_at
                ]
                if left and right:
                    best = (score, left, right)
    if best is None:
        bbox = _union_bbox([item["bbox"] for item in items])
        return [{"items": items, "bbox": bbox}] if bbox else []
    return _split_floor_regions(
        best[1], span_x, span_y, depth=depth + 1
    ) + _split_floor_regions(
        best[2], span_x, span_y, depth=depth + 1
    )


def _nearest_anchor(
    point: tuple[float, float], anchors: list[dict]
) -> tuple[dict | None, float, float]:
    distances = sorted(
        (
            (math.dist(point, anchor["center"]), anchor)
            for anchor in anchors
        ),
        key=lambda item: item[0],
    )
    if not distances:
        return None, float("inf"), float("inf")
    nearest = distances[0]
    second = distances[1][0] if len(distances) > 1 else float("inf")
    return nearest[1], nearest[0], second


def _assign_model_floors(
    entities: list[dict],
    texts: list[dict],
) -> tuple[dict[str, str], list[dict], list[dict]]:
    """把同一模型空间内的候选图元按楼层区域归属。

    只做“逻辑裁剪”：不修改原图，不写一份新 DXF。区域或楼层标注不明确时
    保留未识别，不强行归属。
    """
    anchors = _floor_anchors(texts)
    if not anchors:
        return {}, [], []
    anchor_labels = {anchor["label"] for anchor in anchors}
    if len(anchor_labels) == 1:
        only_label = next(iter(anchor_labels))
        bbox = _union_bbox(
            [item.get("bbox") for item in texts if item.get("bbox")]
        )
        floor_map = {}
        for entity in entities:
            handle = _text((entity.get("source") or {}).get("entityHandle"))
            if handle:
                floor_map[handle] = only_label
        return (
            floor_map,
            [
                {
                    "label": only_label,
                    "confidence": "single-floor",
                    "bbox": bbox,
                    "entityCount": len(entities),
                }
            ],
            [],
        )
    geometry = []
    for entity in entities:
        if entity.get("type") not in (ROUTE_TYPES | {"INSERT"}):
            continue
        layer = _text(entity.get("layer"))
        if any(marker.upper() in layer.upper() for marker in FRAME_LAYER_MARKERS):
            continue
        bbox = _entity_bbox(entity)
        center = _bbox_center(bbox)
        if bbox is None or center is None:
            continue
        geometry.append(
            {
                "entity": entity,
                "bbox": bbox,
                "center": center,
            }
        )
    if not geometry:
        return {}, [], []
    xs = [box[0] for item in geometry for box in [item["bbox"]]]
    ys = [box[1] for item in geometry for box in [item["bbox"]]]
    span_x = max(
        max(item["bbox"][2] for item in geometry) - min(xs), 1.0
    )
    span_y = max(
        max(item["bbox"][3] for item in geometry) - min(ys), 1.0
    )
    regions = _split_floor_regions(geometry, span_x, span_y)

    # 每个区域先找内部楼层标注；标注不在区域内时再看最近标注。
    for region in regions:
        bbox = region["bbox"]
        if bbox is None:
            region["label"] = "未识别"
            region["confidence"] = "unresolved"
            continue
        expanded = [
            bbox[0] - span_x * 0.12,
            bbox[1] - span_y * 0.12,
            bbox[2] + span_x * 0.12,
            bbox[3] + span_y * 0.12,
        ]
        inside = [
            anchor
            for anchor in anchors
            if _inside(anchor["center"], expanded)
        ]
        labels = {anchor["label"] for anchor in inside}
        if len(labels) == 1:
            anchor = inside[0]
            region["label"] = anchor["label"]
            region["confidence"] = "region"
            region["anchor"] = anchor
        elif len(labels) > 1:
            # 同一空白区域里出现多个楼层标注，说明自动分区没有把楼层分开；
            # 不按区域中心强行归层，留给人工确认。
            region["label"] = "未识别"
            region["confidence"] = "ambiguous"
        else:
            center = _bbox_center(bbox)
            anchor, distance, second = _nearest_anchor(
                center or (0, 0), anchors
            )
            if (
                anchor is not None
                and distance <= max(span_x, span_y) * 0.55
                and (second == float("inf") or distance < second * 0.7)
            ):
                region["label"] = anchor["label"]
                region["confidence"] = "nearest"
                region["anchor"] = anchor
            else:
                region["label"] = "未识别"
                region["confidence"] = "unresolved"

    # 同楼层区域合并，保留可解释的包围盒。
    merged: dict[str, dict] = {}
    for region in regions:
        label = region["label"]
        if label == "未识别":
            merged.setdefault("未识别", {
                "label": label,
                "confidence": "unresolved",
                "bbox": None,
                "entityCount": 0,
            })
            target = merged[label]
            target["entityCount"] += len(region["items"])
            target["bbox"] = _union_bbox(
                [target["bbox"], region["bbox"]]  # type: ignore[list-item]
            )
            continue
        target = merged.setdefault(
            label,
            {
                "label": label,
                "confidence": region["confidence"],
                "bbox": None,
                "entityCount": 0,
            },
        )
        target["entityCount"] += len(region["items"])
        target["bbox"] = _union_bbox(
            [target["bbox"], region["bbox"]]  # type: ignore[list-item]
        )
    floor_regions = list(merged.values())

    # 每个图元按中心落在哪个区域归属；重叠时取面积更小的区域。
    region_list = sorted(
        regions,
        key=lambda region: (
            (region["bbox"][2] - region["bbox"][0])
            * (region["bbox"][3] - region["bbox"][1])
            if region["bbox"]
            else float("inf")
        ),
    )
    floor_map: dict[str, str] = {}
    for item in geometry:
        handle = _text((item["entity"].get("source") or {}).get("entityHandle"))
        if not handle:
            continue
        center = item["center"]
        label = "未识别"
        for region in region_list:
            if region["bbox"] and _inside(center, region["bbox"]):
                label = region["label"]
                break
        floor_map[handle] = label
    issues = []
    if len({region["label"] for region in floor_regions if region["label"] != "未识别"}) > 1:
        issues.append(
            {
                "code": "MODEL_FLOOR_REGIONS_DETECTED",
                "message": (
                    "同一模型空间内检测到多个楼层区域，已按图面空白和楼层标注"
                    "把候选设备/管线归到对应楼层；边界仍可在草稿中人工调整。"
                ),
                "severity": "review",
                "sources": [],
            }
        )
    unknown = [
        region
        for region in floor_regions
        if region["label"] == "未识别"
    ]
    if unknown:
        issues.append(
            {
                "code": "MODEL_FLOOR_REGION_UNRESOLVED",
                "message": (
                    "模型空间中有一部分区域没有可确认的楼层标注，"
                    "已保留为“未识别”；不会强行归到某一层。"
                ),
                "severity": "review",
                "sources": [],
            }
        )
    return floor_map, floor_regions, issues


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _bbox_center(bbox: list[float] | None) -> tuple[float, float] | None:
    if not bbox or len(bbox) != 4:
        return None
    return ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)


def _inside(point: tuple[float, float], box) -> bool:
    return bool(box) and box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def _entity_text(entity: dict) -> str:
    geometry = entity.get("geometry") or {}
    return _text(geometry.get("text"))


def _valid_bbox(value) -> list[float] | None:
    """Four finite numbers, already ordered; never reorder or clamp bad input."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number):
            return None
        result.append(number)
    if result[0] > result[2] or result[1] > result[3]:
        return None
    if not math.isfinite(result[2] - result[0]) or not math.isfinite(
        result[3] - result[1]
    ):
        return None
    return result


def _xy(point) -> tuple[float, float] | None:
    """读取坐标点；兼容 [x, y] 与 {"x": ..., "y": ...} 两种 reader 输出。"""
    if point is None:
        return None
    try:
        if isinstance(point, dict):
            x = point.get("x", point.get("X"))
            y = point.get("y", point.get("Y"))
            if isinstance(x, bool) or isinstance(y, bool):
                return None
            if x is None or y is None:
                return None
            values = float(x), float(y)
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            if isinstance(point[0], bool) or isinstance(point[1], bool):
                return None
            values = float(point[0]), float(point[1])
        else:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    return values if all(math.isfinite(value) for value in values) else None


def _entity_bbox(entity: dict) -> list[float] | None:
    source = entity.get("source") or {}
    bbox = _valid_bbox(source.get("bbox"))
    if bbox is not None:
        return bbox
    geometry = entity.get("geometry") or {}
    etype = entity.get("type")
    raw_points: list = []
    if etype == "LINE":
        raw_points = [geometry.get("start"), geometry.get("end")]
    elif etype in {"LWPOLYLINE", "POLYLINE"}:
        raw_points = list(geometry.get("vertices") or [])
    points = [point for point in (_xy(raw) for raw in raw_points) if point]
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return _valid_bbox([min(xs), min(ys), max(xs), max(ys)])


def _annotation_radii(
    entities: list[dict],
) -> tuple[dict[str, float], list[dict]]:
    """Per-view annotation radius; one bad layout cannot widen another.

    HATCH/INSERT boxes are containers/fills, not annotation scale evidence.
    Invalid bounds are audited and never replaced by an inferred value.
    """
    contributors: dict[str, list[tuple[float, str]]] = defaultdict(list)
    excluded: dict[str, list[dict]] = defaultdict(list)
    invalid: dict[str, list[dict]] = defaultdict(list)
    for entity in entities:
        source = entity.get("source") or {}
        view = str(source.get("view") or "model")
        etype = str(entity.get("type") or "")
        raw_bbox = source.get("bbox")
        bbox = _entity_bbox(entity)
        if etype in {"HATCH", "INSERT"}:
            if bbox is not None:
                excluded[view].append(
                    {"entityId": entity.get("id"), "type": etype, "bbox": bbox}
                )
            continue
        if not entity.get("supported", True):
            continue
        if bbox is None:
            if raw_bbox is not None:
                invalid[view].append(
                    {"entityId": entity.get("id"), "type": etype, "raw": raw_bbox}
                )
            continue
        span = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
        if not math.isfinite(span) or span <= 0:
            invalid[view].append(
                {"entityId": entity.get("id"), "type": etype, "raw": raw_bbox}
            )
            continue
        contributors[view].append((span, str(entity.get("id") or "")))

    radii: dict[str, float] = {}
    audit: list[dict] = []
    for view in sorted(set(contributors) | set(excluded) | set(invalid)):
        items = contributors.get(view, [])
        if items:
            max_span, max_entity = max(items, key=lambda item: item[0])
            radius = max(max_span / 30.0, 10.0)
            basis = "geometry"
        else:
            max_span, max_entity = 0.0, None
            radius = max(1000.0 / 30.0, 10.0)
            basis = "fallback_no_geometry"
        radii[view] = float(radius)
        excluded_items = excluded.get(view, [])
        invalid_items = invalid.get(view, [])
        audit.append(
            {
                "view": view,
                "radius": float(radius),
                "maxSpan": float(max_span),
                "maxSpanEntityId": max_entity,
                "contributorCount": len(items),
                "basis": basis,
                "excludedBounds": excluded_items[:200],
                "excludedBoundsCount": len(excluded_items),
                "invalidBounds": invalid_items[:200],
                "invalidBoundsCount": len(invalid_items),
            }
        )
    return radii, audit


def _arc_points(
    center: tuple[float, float],
    radius: float,
    start_angle: float,
    end_angle: float,
) -> list[tuple[float, float]]:
    """Sample the real arc, never a circle's four cardinal points."""
    sweep = (float(end_angle) - float(start_angle)) % 360.0
    if sweep <= 0.0:
        point = (
            center[0] + float(radius) * math.cos(math.radians(float(start_angle))),
            center[1] + float(radius) * math.sin(math.radians(float(start_angle))),
        )
        return [point, point]
    steps = max(2, min(96, int(math.ceil(sweep / 5.0)) + 1))
    points = []
    for index in range(steps):
        angle = math.radians(
            float(start_angle) + sweep * index / (steps - 1)
        )
        points.append(
            (
                center[0] + float(radius) * math.cos(angle),
                center[1] + float(radius) * math.sin(angle),
            )
        )
    return points


def _arc_point_distance(
    center: tuple[float, float],
    radius: float,
    start_angle: float,
    end_angle: float,
    point: tuple[float, float],
) -> float:
    """Exact radial/endpoint distance to the actual arc sector."""
    radius = float(radius)
    distance_from_center = math.dist(point, center)
    angle = math.degrees(
        math.atan2(point[1] - center[1], point[0] - center[0])
    ) % 360.0
    sweep = (float(end_angle) - float(start_angle)) % 360.0
    delta = (angle - float(start_angle)) % 360.0
    if delta <= sweep + 1e-12:
        return abs(distance_from_center - radius)
    start = (
        center[0] + radius * math.cos(math.radians(float(start_angle))),
        center[1] + radius * math.sin(math.radians(float(start_angle))),
    )
    end = (
        center[0] + radius * math.cos(math.radians(float(end_angle))),
        center[1] + radius * math.sin(math.radians(float(end_angle))),
    )
    return min(math.dist(point, start), math.dist(point, end))


def _entity_points(entity: dict) -> list[tuple[float, float]]:
    geometry = entity.get("geometry") or {}
    etype = entity.get("type")
    if etype == "LINE":
        points = [_xy(geometry.get("start")), _xy(geometry.get("end"))]
        return [point for point in points if point]
    if etype in {"LWPOLYLINE", "POLYLINE"}:
        return [
            point
            for point in (_xy(raw) for raw in (geometry.get("vertices") or []))
            if point
        ]
    if etype == "ARC":
        center = _xy(geometry.get("center"))
        radius = geometry.get("radius")
        start_angle = geometry.get("startAngle")
        end_angle = geometry.get("endAngle")
        if (
            center is not None
            and radius
            and start_angle is not None
            and end_angle is not None
        ):
            try:
                return _arc_points(
                    center,
                    float(radius),
                    float(start_angle),
                    float(end_angle),
                )
            except (TypeError, ValueError, OverflowError):
                return []
        return []
    if etype == "CIRCLE":
        center = _xy(geometry.get("center"))
        radius = geometry.get("radius")
        if center is not None and radius:
            return [
                (center[0] + float(radius), center[1]),
                (center[0] - float(radius), center[1]),
                (center[0], center[1] + float(radius)),
                (center[0], center[1] - float(radius)),
            ]
    return []


def _point_segment_distance(
    point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]
) -> float:
    px, py = point
    ax, ay = start
    bx, by = end
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _entity_point_distance(entity: dict, point: tuple[float, float]) -> float | None:
    points = _entity_points(entity)
    if not points:
        return None
    if len(points) == 1:
        return math.dist(point, points[0])
    if entity.get("type") == "CIRCLE":
        geometry = entity.get("geometry") or {}
        center = geometry.get("center")
        radius = geometry.get("radius")
        if center is not None and radius:
            return abs(math.dist(point, (float(center[0]), float(center[1]))) - float(radius))
    if entity.get("type") == "ARC":
        geometry = entity.get("geometry") or {}
        center = _xy(geometry.get("center"))
        radius = geometry.get("radius")
        start_angle = geometry.get("startAngle")
        end_angle = geometry.get("endAngle")
        if (
            center is not None
            and radius
            and start_angle is not None
            and end_angle is not None
        ):
            try:
                return _arc_point_distance(
                    center,
                    float(radius),
                    float(start_angle),
                    float(end_angle),
                    point,
                )
            except (TypeError, ValueError, OverflowError):
                return None
    return min(
        _point_segment_distance(point, points[index], points[index + 1])
        for index in range(len(points) - 1)
    )


class _TextIndex:
    """按视图建立文字网格，避免每个图元都全量扫描附近标注。"""

    def __init__(self, texts: list[dict]) -> None:
        self._by_view: dict[str, list[dict]] = defaultdict(list)
        coordinates: dict[str, tuple[list[float], list[float]]] = defaultdict(
            lambda: ([], [])
        )
        for item in texts:
            view = str(item.get("view") or "model")
            self._by_view[view].append(item)
            center = _bbox_center(item.get("bbox"))
            if center:
                coordinates[view][0].append(center[0])
                coordinates[view][1].append(center[1])
        # A cell is a per-view search aid only. One layout's coordinate span
        # must never widen another layout's semantic neighborhood.
        self._cell_by_view: dict[str, float] = {}
        self._grid: dict[tuple[str, int, int], list[dict]] = defaultdict(list)
        for view, (xs, ys) in coordinates.items():
            span = max(
                (max(xs) - min(xs)) if xs else 0.0,
                (max(ys) - min(ys)) if ys else 0.0,
                1.0,
            )
            cell = max(span / 120.0, 1.0)
            self._cell_by_view[view] = cell
            for item in self._by_view[view]:
                center = _bbox_center(item.get("bbox"))
                if center is None:
                    continue
                key = (
                    view,
                    int(math.floor(center[0] / cell)),
                    int(math.floor(center[1] / cell)),
                )
                self._grid[key].append(item)
        for item in texts:
            view = str(item.get("view") or "model")
            self._cell_by_view.setdefault(view, 1.0)

    def nearby(
        self,
        view: str,
        bbox: list[float] | None,
        *,
        layer: str = "",
        expand_cells: int = 2,
    ) -> list[dict]:
        if not bbox:
            return list(self._by_view.get(view, []))
        cell = self._cell_by_view.get(view, 1.0)
        x0, y0, x1, y1 = bbox
        padding = cell * expand_cells
        x0, y0, x1, y1 = x0 - padding, y0 - padding, x1 + padding, y1 + padding
        col_start = int(math.floor(x0 / cell))
        col_end = int(math.floor(x1 / cell))
        row_start = int(math.floor(y0 / cell))
        row_end = int(math.floor(y1 / cell))
        found: list[dict] = []
        seen: set[int] = set()
        for col in range(col_start, col_end + 1):
            for row in range(row_start, row_end + 1):
                for item in self._grid.get((view, col, row), []):
                    if id(item) in seen:
                        continue
                    center = _bbox_center(item.get("bbox"))
                    if not _inside(center or (float("inf"), float("inf")), (x0, y0, x1, y1)):
                        continue
                    seen.add(id(item))
                    found.append(item)
        found.sort(
            key=lambda item: (
                0 if layer and item.get("layer") == layer else 1,
                0 if "×" in item.get("text", "") or "x" in item.get("text", "").lower() else 1,
                len(item.get("text", "")),
            )
        )
        return found


def _parse_pipe_specs(text: str) -> list[dict[str, str]]:
    """从一段标注读出管径；两条管径按液管/气管拆分。

    只接受合理的管径数值，避免把 600×600 之类尺寸当成管径。
    """
    if not text:
        return []
    pair_text = any(
        marker.upper() in text.upper() for marker in PIPE_TEXT_MARKERS
    ) or "." in text or "Φ" in text or "φ" in text
    if not pair_text:
        return []
    pairs: list[tuple[float, float]] = []
    for match in PAIR_DIAMETER_RE.finditer(text):
        try:
            first = float(match.group(1))
            second = float(match.group(2))
        except (TypeError, ValueError):
            continue
        if first == second:
            continue
        if not (4 <= first <= 120 and 4 <= second <= 120):
            continue
        pairs.append((min(first, second), max(first, second)))
    unique_pairs = sorted(set(pairs))
    if len(unique_pairs) > 1:
        return [
            {
                "spec": "",
                "role": "",
                "pairSpec": "",
                "specSource": "ambiguous",
                "annotation": text,
            }
        ]
    if unique_pairs:
        liquid, gas = unique_pairs[0]
        pair_text = f"{liquid:g}×{gas:g}"
        return [
            {
                "spec": f"{liquid:g}",
                "role": "液管",
                "pairSpec": pair_text,
                "specSource": "annotation",
                "annotation": text,
            },
            {
                "spec": f"{gas:g}",
                "role": "气管",
                "pairSpec": pair_text,
                "specSource": "annotation",
                "annotation": text,
            },
        ]
    remainder = PAIR_DIAMETER_RE.sub(" ", text)
    singles: list[float] = []
    for match in SINGLE_DIAMETER_RE.finditer(remainder):
        raw = match.group(1) or match.group(2)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        # 水系统主管常见 DN125/DN200/DN250；单管径上限放宽到 DN600。
        # 成对标注仍按 4-120 限制，避免把 600×320 风管当成冷媒管。
        if 4 <= value <= 600 and value not in singles:
            singles.append(value)
    if len(singles) == 1:
        return [
            {
                "spec": f"{singles[0]:g}",
                "role": "",
                "pairSpec": "",
                "specSource": "annotation",
                "annotation": text,
            }
        ]
    if len(singles) > 1:
        return [
            {
                "spec": "",
                "role": "",
                "pairSpec": "",
                "specSource": "ambiguous",
                "annotation": text,
            }
        ]
    return []


def _specs_from_texts(texts: list[dict], layer: str) -> list[dict[str, str]]:
    options: list[list[dict[str, str]]] = []
    for item in texts:
        value = item.get("text", "")
        if not (
            any(marker.upper() in value.upper() for marker in PIPE_TEXT_MARKERS)
            or PAIR_DIAMETER_RE.search(value)
        ):
            continue
        parsed = _parse_pipe_specs(value)
        if parsed:
            options.append(parsed)
    if not options:
        parsed_layer = _parse_pipe_specs(layer)
        if parsed_layer:
            for item in parsed_layer:
                item["specSource"] = "layer"
                item["annotation"] = layer
            return parsed_layer
        return []
    first = options[0]
    if any(item.get("specSource") == "ambiguous" for item in first):
        return first
    # 同一管段附近若出现两组不同管径，不能猜，交给人工确认。
    signatures = {
        tuple(sorted((entry["spec"], entry["role"]) for entry in option))
        for option in options
        if all(entry.get("spec") for entry in option)
    }
    if len(signatures) > 1:
        return [
            {
                "spec": "",
                "role": "",
                "pairSpec": "",
                "specSource": "ambiguous",
                "annotation": " / ".join(
                    item.get("text", "") for item in texts[:4]
                ),
            }
        ]
    return first


def _apply_layer_spec_fallback(groups, texts: list[dict]) -> None:
    """同系统只有一种 DN 标注时，给未读到管径的管线补上。

    例如客房层水管在 PIPE-空冷供水/回水，DN125 标注在 DIM-空冷供水；
    如果整层该系统的 DN 标注唯一，可以把 DN125 作为这些管段的管径依据。
    多种 DN 同时存在时不猜，仍保留 PIPE_SPEC_UNRESOLVED。
    """
    by_marker: dict[str, set[str]] = {}
    for item in texts:
        value = str(item.get("text") or "")
        if "DN" not in value.upper():
            continue
        specs = {
            entry.get("spec")
            for entry in _parse_pipe_specs(value)
            if entry.get("spec")
        }
        if not specs:
            continue
        layer = str(item.get("layer") or "")
        for marker in WATER_ROUTE_MARKERS:
            if marker.lower() in layer.lower():
                by_marker.setdefault(marker, set()).update(specs)
    for group in groups:
        if group.get("spec"):
            continue
        if group.get("category") not in {
            "水管",
            "冷冻水",
            "冷却水",
            "补水",
            "排水",
        }:
            continue
        layer = str(group.get("layer") or "")
        specs: set[str] = set()
        for marker in WATER_ROUTE_MARKERS:
            if marker.lower() in layer.lower():
                specs.update(by_marker.get(marker, set()))
        if len(specs) != 1:
            continue
        spec = next(iter(specs))
        group["spec"] = spec
        group["role"] = group.get("role") or group.get("category") or ""
        group["specSource"] = "layer_dimension"
        group["annotation"] = f"同系统管径标注 DN{spec}"
        group["issues"] = [
            issue
            for issue in group.get("issues") or []
            if issue not in {"PIPE_SPEC_UNRESOLVED", "PIPE_SPEC_AMBIGUOUS"}
        ]
        if "PIPE_SPEC_INFERRED" not in group["issues"]:
            group["issues"].append("PIPE_SPEC_INFERRED")
        group["key"] = str(group.get("key") or "").replace(
            "|nospec|", f"|{spec}|"
        )
        group["ruleKey"] = str(group.get("ruleKey") or "").replace(
            "|nospec|", f"|{spec}|"
        )


def _visible_model(texts: list[dict]) -> str:
    for item in texts:
        value = str(item.get("text") or "").strip()
        if value and any(
            marker.upper() in value.upper() for marker in MODEL_CODE_MARKERS
        ):
            return value
    return ""


def _xdata_values(entity: dict) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for key, values in (entity.get("xdata") or {}).items():
        items = []
        for value in values or []:
            if isinstance(value, dict):
                text = _text(value.get("value"))
            else:
                text = _text(value)
            if text:
                items.append(text)
        if items:
            result[str(key)] = items
    return result


def _is_equipment(entity: dict) -> bool:
    if entity.get("type") != "INSERT":
        return False
    layer = _text(entity.get("layer"))
    if _is_annotation_layer(layer):
        return False
    block = _text((entity.get("geometry") or {}).get("blockName"))
    xdata = _xdata_values(entity)
    block_haystack = " ".join([block, *sum(xdata.values(), [])]).upper()
    block_marked = any(
        marker.upper() in block_haystack for marker in EQUIPMENT_TEXT_MARKERS
    )
    layer_marked = any(
        marker.upper() in layer.upper() for marker in EQUIPMENT_LAYER_MARKERS
    )
    semantics = _route_layer_semantics(layer)
    if str(semantics.get("rule") or "").startswith("exact_alias:"):
        # A precise pipe alias (e.g. ACS_KT_LN) contains the substring "AC".
        # Layer wording alone must not turn that legitimate pipe container into
        # equipment; require an actual block/model/attribute hint.
        return block_marked
    layer_text_marked = any(
        marker.upper() in layer.upper() for marker in EQUIPMENT_TEXT_MARKERS
    )
    return block_marked or layer_marked or layer_text_marked


ANNOTATION_LAYER_MARKERS = (
    "标注",
    "说明",
    "引线",
    "尺寸",
    "文字",
    "注释",
    "ANNO",
    "DIM",
    "TEXT",
    "LABEL",
)


def _is_annotation_layer(layer: str) -> bool:
    upper = (layer or "").upper()
    return any(marker.upper() in upper for marker in ANNOTATION_LAYER_MARKERS)


def _inherited_context_route_rejection(entity: dict) -> str | None:
    """Reject symbol strokes that only inherit a protected container layer.

    A nested INSERT with its own explicit route layer keeps its own owner path,
    so legitimate routes inside a container are not blanket-excluded.
    """
    source = entity.get("source") or {}
    if not source.get("layerInherited"):
        return None
    owner = source.get("layerOwnerPath")
    for context in source.get("expansionContexts") or []:
        if owner and owner == context.get("instancePath"):
            return "INHERITED_LAYER_CONTEXT_GEOMETRY"
    return None


REFRIGERANT_LAYER_ALIASES = VOCABULARY["refrigerant_layer_aliases"]
CONDENSATE_LAYER_ALIASES = VOCABULARY["condensate_layer_aliases"]


def _route_layer_semantics(layer: str) -> dict:
    """Layer string only. Eligibility is decided later with source provenance."""
    name = str(layer or "").strip()
    folded = name.casefold()
    result = {
        "candidate": False,
        "medium": "unknown",
        "rule": "",
        "strength": "none",
    }
    if _is_annotation_layer(name):
        return result
    aliases = (
        ("refrigerant_layer_aliases", REFRIGERANT_LAYER_ALIASES, "refrigerant"),
        ("condensate_layer_aliases", CONDENSATE_LAYER_ALIASES, "condensate"),
    )
    for key, values, medium in aliases:
        if folded in {str(item).strip().casefold() for item in values}:
            return {
                "candidate": True,
                "medium": medium,
                "rule": f"exact_alias:{key}:{folded}",
                "strength": "alias",
            }
    found = {
        medium
        for medium, markers in ROUTE_MEDIUM_RULES
        if any(marker.casefold() in folded for marker in markers)
    }
    if found == {"duct"}:
        return result
    if found <= {"drain", "condensate"} and found:
        found = {"condensate" if "condensate" in found else "drain"}
    candidate = any(
        str(marker).casefold() in folded for marker in PIPE_LAYER_MARKERS
    )
    if not candidate:
        return result
    medium = (
        next(iter(found))
        if len(found) == 1 and "duct" not in found
        else "unknown"
    )
    return {
        "candidate": True,
        "medium": medium,
        "rule": f"native_layer:{folded}",
        "strength": "explicit" if medium != "unknown" else "generic",
    }


def _is_pipe(entity: dict) -> bool:
    if entity.get("type") not in ROUTE_TYPES:
        return False
    if entity.get("type") == "CIRCLE":
        # A circle is a symbol, not a run length; keep it auditable elsewhere.
        return False
    return bool(
        _route_layer_semantics(_text(entity.get("layer")))["candidate"]
    )


def _polyline_parameter(
    points: list[tuple[float, float]], point: tuple[float, float]
) -> tuple[float, float]:
    """返回点在该折线上的参数（按长度 0..1）与最近距离。"""
    total = sum(
        math.dist(points[index], points[index + 1])
        for index in range(len(points) - 1)
    )
    if total <= 0:
        return 0.0, float("inf")
    best_t = 0.0
    best_distance = float("inf")
    accumulated = 0.0
    for index in range(len(points) - 1):
        start = points[index]
        end = points[index + 1]
        segment = math.dist(start, end)
        if segment <= 0:
            continue
        raw = (
            (point[0] - start[0]) * (end[0] - start[0])
            + (point[1] - start[1]) * (end[1] - start[1])
        ) / (segment * segment)
        local = max(0.0, min(1.0, raw))
        closest = (
            start[0] + local * (end[0] - start[0]),
            start[1] + local * (end[1] - start[1]),
        )
        distance = math.dist(point, closest)
        if distance < best_distance:
            best_distance = distance
            best_t = (accumulated + local * segment) / total
        accumulated += segment
    return best_t, best_distance


def _nearest_vertex_parameter(
    points: list[tuple[float, float]], target: float
) -> float | None:
    total = sum(
        math.dist(points[index], points[index + 1])
        for index in range(len(points) - 1)
    )
    if total <= 0:
        return None
    accumulated = 0.0
    best: float | None = None
    best_diff = float("inf")
    for index, point in enumerate(points):
        if index:
            accumulated += math.dist(points[index - 1], point)
        value = accumulated / total
        diff = abs(value - target)
        if diff < best_diff:
            best, best_diff = value, diff
    return best


def _native_branch_symbols(read, entities: list[dict]) -> dict[str, list[dict]]:
    """Read root triangular symbols without inventing an engineering boundary.

    An equilateral solid triangle must cross one straight native route axis. Its
    axis-aligned apex and base are both retained; neither is an approved fitting
    interface. The nearest unique intersecting route owns the symbol.
    """
    routes = [e for e in entities if e.get("type") == "LWPOLYLINE"
              and len(e.get("instancePath") or []) == 1]
    found: dict[str, list[dict]] = defaultdict(list)
    for item in entities:
        if item.get("type") != "HATCH" or len(item.get("instancePath") or []) != 1:
            continue
        source = item.get("source") or {}
        raw = read.document.entitydb.get(str(source.get("entityHandle") or ""))
        try:
            triangle = extract_triangle_boundary(raw, source=source)
        except RouteSegmentError:
            continue
        vertices = triangle["vertices"]
        sides = [math.dist(a, b) for a, b in zip(vertices, vertices[1:] + vertices[:1])]
        size = max(sides)
        if min(sides) < size * 0.97:
            continue
        center = [sum(p[i] for p in vertices) / 3 for i in range(3)]
        candidates = []
        for original_route in routes:
            # Normalization can retain numpy scalar doubles from ezdxf. The
            # bounded geometry API deliberately accepts only JSON numbers.
            geometry = original_route.get("geometry") or {}
            route = {**original_route, "geometry": {**geometry,
                "vertices": [[float(n) for n in p] for p in geometry.get("vertices") or []],
                "elevation": float(geometry.get("elevation", 0)),
            }}
            ref = route.get("source") or {}
            if ref.get("view") != source.get("view"):
                continue
            try:
                projection = project_anchor_to_segment(route, center, max_residual_du=size * 0.1)
                projected = [project_anchor_to_segment(
                    route, point, max_residual_du=size,
                    segment_index=projection["segmentIndex"],
                ) for point in vertices]
            except RouteSegmentError:
                continue
            # The base is perpendicular to the route. A general triangle near a
            # wall/route is insufficient to establish a branch symbol.
            pairs = [(i, j) for i in range(3) for j in range(i + 1, 3)
                     if math.dist(projected[i]["point"], projected[j]["point"]) <= size * 0.02]
            if len(pairs) != 1:
                continue
            i, j = pairs[0]
            k = next(k for k in range(3) if k not in {i, j})
            base_anchor = [(vertices[i][axis] + vertices[j][axis]) / 2 for axis in range(3)]
            try:
                base = project_anchor_to_segment(route, base_anchor, max_residual_du=size,
                                                 segment_index=projection["segmentIndex"])
            except RouteSegmentError:
                continue
            apex = projected[k]
            if math.dist(apex["point"], base["point"]) < size * 0.5:
                continue
            points = _entity_points(route)
            for anchor in (apex, base):
                anchor["routeParameter"] = _polyline_parameter(points, tuple(anchor["point"][:2]))[0]
            candidates.append((projection["residualDU"], str(route["id"]), {
                "symbolHandle": source.get("entityHandle"), "source": source,
                "vertices": vertices, "segmentIndex": projection["segmentIndex"],
                "apex": apex, "base": base,
                "engineeringApproval": False,
                "boundaryStatus": "proposal_requires_confirmation",
                "rule": "native_triangle_axis_apex_projection_proposal",
                "geometryQualification": {"relativeSideAgreement": 0.03,
                    "centerResidualBoundDU": size * 0.1,
                    "baseProjectionAgreementDU": size * 0.02,
                    "symbolAxisLengthDU": math.dist(apex["point"], base["point"])},
                "basis": "Native solid equilateral branch symbol crossing the original route axis; apex/base are alternative measurement rules, not approved fitting interfaces.",
            }))
        candidates.sort(key=lambda candidate: candidate[0])
        if candidates and (len(candidates) == 1 or candidates[1][0] - candidates[0][0] > 1e-8):
            _, entity_id, symbol = candidates[0]
            found[entity_id].append(symbol)
    return found


def _native_style_key(entity: dict) -> tuple | None:
    geometry = entity.get("geometry") or {}
    color = geometry.get("renderColor") or {}
    aci = color.get("effectiveAciColor")
    rgb = color.get("effectiveTrueColor")
    if rgb is None and (aci is None or aci in {0, 252}):
        return None
    style = geometry.get("renderStyle") or {}
    return (rgb, aci, style.get("effectiveLineTypeName"), style.get("ltypeScale"))


def _native_branch_connections(entities: list[dict], symbols: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """A native symbol's drawn port supports identity, never adds pipe length."""
    routes = [e for e in entities if e.get("type") == "LWPOLYLINE"
              and len(e.get("instancePath") or []) == 1 and not (e.get("geometry") or {}).get("closed")]
    by_id = {str(e["id"]): e for e in routes}
    lines = [e for e in entities if e.get("type") == "LINE" and len(e.get("instancePath") or []) == 1]
    result: dict[str, list[dict]] = defaultdict(list)
    for owner, entries in symbols.items():
        main = by_id[owner]
        for symbol in entries:
            apex = symbol["apex"]["anchor"]
            base = [p for p in symbol["vertices"] if p != apex]
            size = math.dist(base[0], base[1])
            tolerance = size * 0.03
            ports = []
            for line in lines:
                if (line.get("source") or {}).get("view") != (main.get("source") or {}).get("view"):
                    continue
                points = _entity_points(line)
                if len(points) != 2 or not (0 < math.dist(*points) <= size):
                    continue
                a, b = points
                if min(_point_segment_distance(p, tuple(base[0][:2]), tuple(base[1][:2])) for p in points) > tolerance:
                    continue
                axis = [symbol["base"]["anchor"][i] - apex[i] for i in range(2)]
                cross = abs((b[0] - a[0]) * axis[1] - (b[1] - a[1]) * axis[0])
                if cross > math.dist(a, b) * math.hypot(*axis) * 0.03:
                    continue
                ports.append((line, points))
            for route in routes:
                if str(route["id"]) == owner or _native_style_key(route) != _native_style_key(main) or _native_style_key(main) is None:
                    continue
                if (route.get("source") or {}).get("view") != (main.get("source") or {}).get("view"):
                    continue
                points = _entity_points(route)
                for endpoint in (points[0], points[-1]):
                    candidates = [(None, _point_segment_distance(endpoint, tuple(base[0][:2]), tuple(base[1][:2])))]
                    candidates.extend(((line.get("source") or {}).get("entityHandle"), _point_segment_distance(endpoint, *path)) for line, path in ports)
                    connector, residual = min(candidates, key=lambda pair: pair[1])
                    if residual <= tolerance:
                        result[str(route["id"])].append({
                            "kind": "native_branch_port_proposal", "symbolHandle": symbol["symbolHandle"],
                            "targetId": owner, "targetHandle": (main.get("source") or {}).get("entityHandle"),
                            "connectorHandle": connector, "endpoint": list(endpoint),
                            "residualDU": residual, "residualBoundDU": tolerance,
                            "engineeringApproval": False, "connectorLengthAdded": False,
                        })
    return result


def _split_route_by_annotations(
    entity: dict,
    nearby_annotations: list[dict],
    length_raw: float | None,
    medium: str = "",
    min_segment: float = 0.0,
    boundary_symbols: list[dict] | None = None,
) -> list[dict] | None:
    """一条折线跨两种管径标注时，优先按原生分歧符号投影提出分段。

    只在两个不同成对标注都能落到各自区段时启用；分界是推断值，
    调用方必须标记待人工确认。
    """
    if entity.get("type") not in {"LWPOLYLINE", "POLYLINE"}:
        return None
    if not length_raw or length_raw <= 0:
        return None
    points = _entity_points(entity)
    if len(points) < 3:
        return None
    by_pair: dict[str, dict] = {}
    positioned_annotations: list[dict] = []
    for item in nearby_annotations:
        pair_specs = [
            entry for entry in item.get("specs") or [] if entry.get("pairSpec")
        ]
        if not pair_specs:
            continue
        center = _bbox_center(item.get("bbox"))
        if center is None:
            continue
        parameter, distance = _polyline_parameter(points, center)
        pair = str(pair_specs[0].get("pairSpec") or "")
        if not pair:
            continue
        positioned = {
            "pair": pair, "t": parameter, "distance": distance,
            "specs": item.get("specs") or [],
            "annotation": str(item.get("text") or ""), "annotationId": item.get("id"),
        }
        positioned_annotations.append(positioned)
        existing = by_pair.get(pair)
        if existing is None or distance < existing["distance"]:
            by_pair[pair] = {
                "pair": pair,
                "t": parameter,
                "distance": distance,
                "specs": item.get("specs") or [],
                "annotation": str(item.get("text") or ""),
                "annotationId": item.get("id"),
            }
    if len(by_pair) < 2:
        return None
    chosen = sorted(by_pair.values(), key=lambda item: item["t"])[:2]
    boundary_evidence = None
    if boundary_symbols:
        ordered = sorted(positioned_annotations, key=lambda item: item["t"])
        transitions = [(left, right) for left, right in zip(ordered, ordered[1:])
                       if left["pair"] != right["pair"]]
        if len(transitions) != 1:
            return None
        chosen = list(transitions[0])
        matching = [symbol for symbol in boundary_symbols
                    if chosen[0]["t"] < min(symbol["apex"]["routeParameter"], symbol["base"]["routeParameter"])
                    and max(symbol["apex"]["routeParameter"], symbol["base"]["routeParameter"]) < chosen[1]["t"]]
        if len(matching) != 1:
            # Several native branches between the two notes do not identify
            # which branch changes this diameter. Do not fall back to a bend.
            return None
        boundary_evidence = {**matching[0], "annotationSides": [
            {"annotationId": item.get("annotationId"), "pairSpec": item["pair"],
             "routeParameter": item["t"], "side": side}
            for side, item in zip(("before", "after"), chosen)
        ]}
        boundary = boundary_evidence["apex"]["routeParameter"]
        base = boundary_evidence["base"]["routeParameter"]
        boundary_evidence["baseVariant"] = {
            "routeParameter": base, "beforeLengthRaw": length_raw * base,
            "afterLengthRaw": length_raw * (1 - base),
        }
        boundary_evidence["apexBaseLengthEnvelopeRaw"] = sorted([length_raw * boundary, length_raw * base])
    else:
        if chosen[1]["t"] - chosen[0]["t"] < 0.1:
            return None
        boundary = _nearest_vertex_parameter(
            points, (chosen[0]["t"] + chosen[1]["t"]) / 2
        )
    lower, upper = (0.0, 1.0) if boundary_evidence else (0.05, 0.95)
    if boundary is None or not (lower < boundary < upper):
        return None
    if not (chosen[0]["t"] < boundary < chosen[1]["t"]):
        return None
    # 分段必须两段都有实际长度：至少占全长 10%，且不小于调用方给出的
    # 最小图面尺度；避免把 0.16 m 的小支路强行切成“主管规格”。
    floor_length = 0.0 if boundary_evidence else max(length_raw * 0.1, min_segment)
    if (
        length_raw * boundary < floor_length
        or length_raw * (1 - boundary) < floor_length
    ):
        return None
    return [
        {
            "t0": 0.0,
            "t1": boundary,
            "lengthRaw": length_raw * boundary,
            "specs": chosen[0]["specs"],
            "annotation": chosen[0]["annotation"],
            "medium": medium,
            "boundaryInferred": True,
            "boundaryEvidence": boundary_evidence,
        },
        {
            "t0": boundary,
            "t1": 1.0,
            "lengthRaw": length_raw * (1 - boundary),
            "specs": chosen[1]["specs"],
            "annotation": chosen[1]["annotation"],
            "medium": medium,
            "boundaryInferred": True,
            "boundaryEvidence": boundary_evidence,
        },
    ]


def _line_length(entity: dict) -> tuple[float | None, str | None]:
    geometry = entity.get("geometry") or {}
    etype = entity.get("type")
    try:
        if etype == "LINE":
            start, end = geometry["start"], geometry["end"]
            return math.dist(start[:2], end[:2]), None
        if etype in {"LWPOLYLINE", "POLYLINE"}:
            vertices = geometry.get("vertices") or []
            if len(vertices) < 2:
                return None, "NO_VERTICES"
            length = sum(
                math.dist(vertices[i][:2], vertices[i + 1][:2])
                for i in range(len(vertices) - 1)
            )
            if geometry.get("closed") and len(vertices) > 2:
                length += math.dist(vertices[-1][:2], vertices[0][:2])
            return length, "POLYLINE_SEGMENTS_ONLY"
        if etype == "ARC":
            radius = geometry.get("radius")
            start_angle = geometry.get("startAngle")
            end_angle = geometry.get("endAngle")
            if radius and start_angle is not None and end_angle is not None:
                sweep = (float(end_angle) - float(start_angle)) % 360
                return math.radians(sweep) * float(radius), "ARC_APPROXIMATED"
        if etype == "CIRCLE":
            radius = geometry.get("radius")
            if radius:
                return 2 * math.pi * float(radius), "CIRCLE_AS_ROUTE"
        if etype == "SPLINE":
            return None, "SPLINE_LENGTH_UNVERIFIED"
        if etype == "ELLIPSE":
            return None, "ELLIPSE_LENGTH_UNVERIFIED"
    except (KeyError, TypeError, ValueError, AttributeError):
        return None, "GEOMETRY_INVALID"
    return None, "GEOMETRY_UNSUPPORTED"


def _accumulate_route_length(
    group: dict, segment_length: float | None, scale: Decimal | None
) -> None:
    """Accumulate only measured segments; an unmeasured part keeps the total null."""
    if segment_length is not None:
        group["lengthKnownRaw"] += segment_length
        if scale is not None and group["lengthKnownM"] is not None:
            group["lengthKnownM"] += float(
                Decimal(str(segment_length)) * scale
            )
    else:
        group["unmeasuredSegmentCount"] += 1
    if group["unmeasuredSegmentCount"]:
        group["lengthRaw"] = None
        group["lengthM"] = None
        group["lengthStatus"] = (
            "partial" if group["lengthKnownRaw"] > 0 else "unmeasured"
        )
    else:
        group["lengthRaw"] = group["lengthKnownRaw"]
        group["lengthM"] = group["lengthKnownM"]
        group["lengthStatus"] = "complete"


def _category_from_medium(medium: str) -> str:
    return {
        "refrigerant": "冷媒管",
        "condensate": "排水",
        "drain": "排水",
        "supply": "水管",
        "return": "水管",
        "cooling": "水管",
        "makeup": "水管",
        "duct": "风管",
    }.get(str(medium or ""), "unknown")


def _route_category(layer: str, texts: list[str]) -> str:
    semantics = _route_layer_semantics(layer)
    medium = str(semantics.get("medium") or "unknown")
    if medium != "unknown":
        return _category_from_medium(medium)
    haystack = (layer + " " + " ".join(texts)).lower()
    if any(marker.lower() in haystack for marker in ("冷媒", "铜管", "液管", "气管")):
        return "冷媒管"
    if (
        "排水" in haystack
        or "drain" in haystack
        or "冷凝水" in haystack
        or "condensate" in haystack
    ):
        return "排水"
    if any(marker.lower() in haystack for marker in WATER_ROUTE_MARKERS):
        return "水管"
    if "风管" in haystack or "duct" in haystack:
        return "风管"
    return "unknown"


def _model_hint(xdata: dict[str, list[str]], block: str) -> str:
    for key in ("内机型号", "外机型号", "型号"):
        values = xdata.get(key) or []
        if values:
            return values[0]
    return block


def _instance_path(entity: dict) -> tuple[str, ...]:
    return tuple(str(item) for item in (entity.get("instancePath") or []))


def _equipment_kind(*texts: str) -> str:
    haystack = " ".join(item or "" for item in texts).upper()
    for kind, markers in EQUIPMENT_KIND_RULES:
        if any(marker.upper() in haystack for marker in markers):
            return kind
    return ""


def _medium_from_category(category: str) -> str:
    return {
        "冷媒管": "refrigerant",
        "液管": "refrigerant",
        "气管": "refrigerant",
        "排水": "drain",
        "风管": "duct",
    }.get(str(category or ""), "unknown")


def _route_medium(layer: str, texts: list[str]) -> str:
    semantics = _route_layer_semantics(layer)
    if semantics.get("medium") not in (None, "", "unknown"):
        return str(semantics["medium"])
    haystack = " ".join(texts).lower()
    for medium, markers in ROUTE_MEDIUM_RULES:
        if any(marker.lower() in haystack for marker in markers):
            return medium
    return "unknown"


def _is_equipment_note(text: str) -> bool:
    """说明引线常见的设备注记，不能当作管线规格。"""
    upper = (text or "").upper()
    return any(
        marker.upper() in upper
        for marker in (
            "室内机",
            "室外机",
            "220V",
            "380V",
            "A/C",
            "风量",
            "型号",
            *MODEL_CODE_MARKERS,
        )
    )


def _equipment_role(*texts: str) -> str:
    """从型号/文字推断设备角色；先判明确的室内机，再判室外机。

    KFR-50GW 末尾的 W 不是室外机证据；GW 是挂机内机标识。只有明确
    “室外机”、RMX、W/SN 或 KFR-xxW（不带 G）才按室外机处理。
    """
    haystack = " ".join(item or "" for item in texts).upper()
    indoor_markers = ("室内机", "Q4", "GW", "LW", "FP-", "MMD", "FXD", "柜机")
    if any(marker in haystack for marker in indoor_markers):
        return "indoor"
    if (
        "室外机" in haystack
        or "RMX" in haystack
        or "W/SN" in haystack
        or re.search(r"KFR-?\d+(?:\.\d+)?W(?!G)", haystack)
        or re.search(r"(?:^|[^A-Z])W/", haystack)
    ):
        return "outdoor"
    return ""


def _drawing_extent(entities: list[dict]) -> float:
    xs: list[float] = []
    ys: list[float] = []
    for entity in entities:
        bbox = _entity_bbox(entity)
        if not bbox:
            continue
        xs.extend((bbox[0], bbox[2]))
        ys.extend((bbox[1], bbox[3]))
    if not xs:
        return 0.0
    return max(max(xs) - min(xs), max(ys) - min(ys))


def _nearest_model_text(
    entity: dict,
    view: str,
    bbox: list[float] | None,
    layer: str,
    text_index: "_TextIndex",
    fallback: bool = False,
) -> tuple[str, bool, float | None]:
    """只取该实例自己的邻近型号文字，返回（型号, 是否多型号, 最近距离）。"""
    nearby = text_index.nearby(view, bbox, layer=layer, expand_cells=10)
    if not nearby and fallback and bbox:
        # 图面文字离图块较远或网格过细时，退化为本视图最近型号文字；
        # 只接受设备自身尺度附近的文字，避免借用其他设备的型号。
        max_dim = max(
            float(bbox[2]) - float(bbox[0]),
            float(bbox[3]) - float(bbox[1]),
            1.0,
        )
        limit = max_dim * 3.0
        candidates = []
        bbox_center = (
            (float(bbox[0]) + float(bbox[2])) / 2.0,
            (float(bbox[1]) + float(bbox[3])) / 2.0,
        )
        for item in text_index._by_view.get(view, []):
            center = _bbox_center(item.get("bbox"))
            if center is None:
                continue
            distance = _entity_point_distance(entity, center)
            if distance is None:
                distance = math.dist(bbox_center, center)
            if distance <= limit:
                candidates.append(item)
        nearby = candidates
    found: list[tuple[float, str]] = []
    for item in nearby:
        text = str(item.get("text") or "").strip()
        if not text or not any(
            marker.upper() in text.upper() for marker in MODEL_CODE_MARKERS
        ):
            continue
        center = _bbox_center(item.get("bbox"))
        if center is None:
            continue
        distance = _entity_point_distance(entity, center)
        if distance is None and bbox:
            distance = math.dist(
                ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0),
                center,
            )
        found.append((float(distance or 0.0), text))
    if not found:
        return "", False, None
    found.sort(key=lambda item: item[0])
    distinct: list[str] = []
    for _, text in found:
        if text not in distinct:
            distinct.append(text)
    return distinct[0], len(distinct) > 1, found[0][0]


def _anonymous_equipment_candidate(
    entity: dict,
    *,
    view: str,
    layer: str,
    bbox: list[float] | None,
    extent: float,
    text_index: "_TextIndex",
) -> dict | None:
    """块名/图层没有设备关键词，但邻近明确型号且尺度像设备的块。"""
    block = _text((entity.get("geometry") or {}).get("blockName"))
    if not bbox:
        return None
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    if width <= 0 or height <= 0:
        return None
    # 整层平面块、图框、图例不进入设备候选。
    if extent > 0 and max(width, height) > extent * 0.1:
        return None
    if any(
        marker.upper() in f"{layer} {block}".upper()
        for marker in FRAME_LAYER_MARKERS
    ):
        return None
    model, ambiguous, distance = _nearest_model_text(
        entity, view, bbox, layer, text_index
    )
    if not model or distance is None:
        return None
    # 型号文字必须在设备自身尺度附近（3 倍最大边长内），避免图例/说明文字。
    if distance > max(width, height) * 3:
        return None
    role = _equipment_role(model)
    kind = _equipment_kind(model, block, layer) or (
        "室内机" if role == "indoor" else "室外机" if role == "outdoor" else ""
    )
    return {
        "modelText": model,
        "ambiguous": ambiguous,
        "blockName": block,
        "detectedBy": "annotation",
        "modelSource": "annotation",
        "role": role,
        "kind": kind,
    }



def _mark_cross_view_duplicates(groups: list[dict]) -> int:
    """同型号多视图只标记候选重复，不合并；返回涉及组数。"""
    by_model: dict[str, list[dict]] = defaultdict(list)
    for group in groups:
        model = str(group.get("modelHint") or "").strip()
        if model:
            by_model[model].append(group)
    count = 0
    for model_groups in by_model.values():
        views = sorted({str(group.get("view")) for group in model_groups})
        if len(views) < 2:
            continue
        count += len(model_groups)
        for group in model_groups:
            if "CROSS_VIEW_DUPLICATE_CANDIDATE" not in group["issues"]:
                group["issues"].append("CROSS_VIEW_DUPLICATE_CANDIDATE")
            group["crossViewPeerViews"] = [
                view for view in views if view != group.get("view")
            ]
    return count



def _weak_route_rejection(
    entity: dict,
    bbox: list[float] | None,
    annotation_radius: float,
    equipment_boxes: list[tuple[float, float, float, float, float]],
    *,
    has_endpoint_connection: bool = False,
    preserve_pair_candidate: bool = False,
) -> tuple[str, str] | None:
    """弱证据管线实体级排除：闭合轮廓、紧凑符号、贴设备的短轮廓。

    只排除单个实体，不整组排除；被排除实体写入报告审计清单，用户仍可在
    原图核对后手工补录。
    """
    if not bbox or len(bbox) != 4:
        return None
    geometry = entity.get("geometry") or {}
    width = float(bbox[2]) - float(bbox[0])
    height = float(bbox[3]) - float(bbox[1])
    max_dim = max(width, height)
    if geometry.get("closed"):
        return (
            "PIPE_GEOMETRY_CLOSED",
            "闭合折线通常是建筑/图例轮廓，默认不计入；确认是环状管线时请人工补录。",
        )
    if max_dim < annotation_radius * 0.15 and not has_endpoint_connection:
        return (
            "PIPE_COMPACT_SYMBOL",
            "图元尺寸很小，可能是设备符号/图例轮廓，默认不计入；确认是短管段时请人工补录。",
        )
    render = geometry.get("renderColor") or {}
    color_source = str(render.get("effectiveSource") or "")
    aci = render.get("effectiveAciColor")
    if aci is None:
        aci = render.get("aciColor")
    weak_color = color_source in {"unresolved_byblock", "invalid_color"} or aci in {
        0,
        252,
    }
    vertices = list(geometry.get("vertices") or [])
    if (
        weak_color
        and len(vertices) <= 6
        and not has_endpoint_connection
        and not preserve_pair_candidate
    ):
        for ex0, ey0, ex1, ey1, eq_dim in equipment_boxes:
            margin_x = max((ex1 - ex0) * 0.15, annotation_radius * 0.05)
            margin_y = max((ey1 - ey0) * 0.15, annotation_radius * 0.05)
            overlaps = not (
                float(bbox[2]) < ex0 - margin_x
                or float(bbox[0]) > ex1 + margin_x
                or float(bbox[3]) < ey0 - margin_y
                or float(bbox[1]) > ey1 + margin_y
            )
            if overlaps and max_dim <= max(eq_dim * 1.5, annotation_radius):
                return (
                    "PIPE_EQUIPMENT_OUTLINE",
                    "该图元贴着设备轮廓、尺寸相近且颜色不明确，可能是设备/建筑轮廓；"
                    "默认不计入，确认是连接管时请人工补录。",
                )
    return None


def _source_region_key(entity: dict, view: str) -> str:
    path = _instance_path(entity)
    domain = f"insert:{path[0]}" if len(path) > 1 and path[0] else "root"
    return f"{view}|{domain}"


def _route_evidence_key(
    *,
    source_hash: str,
    engine_version: str,
    source_region_key: str,
    segments: list[dict],
    medium: str,
    spec: str,
    eligibility: str,
    review_reasons: list[str],
) -> str:
    payload = {
        "sourceHash": source_hash,
        "engineVersion": engine_version,
        "sourceRegionKey": source_region_key,
        "segments": [
            {
                "physicalSegmentId": item.get("physicalSegmentId"),
                "t0": item.get("t0"),
                "t1": item.get("t1"),
                "lengthRaw": item.get("lengthRaw"),
            }
            for item in segments
        ],
        "medium": medium,
        "spec": spec,
        "eligibility": eligibility,
        "reviewReasons": sorted(set(review_reasons)),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _annotation_semantics(item: dict) -> dict | None:
    text = str(item.get("text") or "")
    specs = list(item.get("specs") or [])
    if any(entry.get("pairSpec") for entry in specs):
        return {"kind": "pair", "medium": "refrigerant", "specs": specs}
    if "DN" in text.upper() or any(entry.get("spec") for entry in specs):
        low = text.lower()
        medium = "unknown"
        if "冷凝" in text or "condensate" in low:
            medium = "condensate"
        elif "排水" in text or "地漏" in text or "drain" in low:
            medium = "drain"
        return {"kind": "dn", "medium": medium, "specs": specs}
    return None


def _weak_route_style(entity: dict) -> bool:
    style = (entity.get("geometry") or {}).get("renderStyle") or {}
    name = str(
        style.get("effectiveLineTypeName") or style.get("lineTypeName") or ""
    ).upper()
    return any(
        marker in name
        for marker in ("DASH", "虚线", "HIDDEN", "DOT", "点划", "CENTER")
    )


def _endpoint_connections(
    entity: dict,
    equipment_boxes: list[tuple[float, float, float, float, float]],
    strong_segments: list[dict],
    *,
    view: str,
    source_region_key: str,
    candidate_medium: str,
) -> list[dict]:
    points = _entity_points(entity)
    if len(points) < 2:
        return []
    endpoints = [points[0], points[-1]]
    connections: list[dict] = []
    for point in endpoints:
        tolerance = max(1e-9, max(abs(point[0]), abs(point[1]), 1.0) * 1e-9)
        for x0, y0, x1, y1, _ in equipment_boxes:
            if (
                x0 - tolerance <= point[0] <= x1 + tolerance
                and y0 - tolerance <= point[1] <= y1 + tolerance
            ):
                connections.append(
                    {
                        "kind": "equipment_bbox_contact",
                        "point": [point[0], point[1]],
                        "distance": 0.0,
                    }
                )
                break
        for item in strong_segments:
            if item.get("entityId") == entity.get("id"):
                continue
            if (
                item.get("view") != view
                or item.get("sourceRegionKey") != source_region_key
            ):
                continue
            segment_points = item.get("points") or []
            distance = min(
                (
                    _point_segment_distance(point, start, end)
                    for start, end in zip(segment_points, segment_points[1:])
                ),
                default=float("inf"),
            )
            if distance <= tolerance:
                target_medium = str(item.get("medium") or "unknown")
                connections.append(
                    {
                        "kind": "route_endpoint_contact",
                        "point": [point[0], point[1]],
                        "targetId": item.get("entityId"),
                        "targetMedium": target_medium,
                        "compatible": bool(
                            candidate_medium not in {"", "unknown"}
                            and target_medium == candidate_medium
                        ),
                        "distance": distance,
                    }
                )
                break
    return connections


def _same_geometry_under_translation(
    first: list[tuple[float, float]], second: list[tuple[float, float]]
) -> bool:
    if not first or len(first) != len(second):
        return False
    left = sorted((float(x), float(y)) for x, y in first)
    right = sorted((float(x), float(y)) for x, y in second)
    dx = right[0][0] - left[0][0]
    dy = right[0][1] - left[0][1]
    magnitude = max(
        (abs(value) for point in left + right for value in point),
        default=1.0,
    )
    tolerance = max(1e-9, magnitude * 1e-9)
    return all(
        abs(x + dx - u) <= tolerance and abs(y + dy - v) <= tolerance
        for (x, y), (u, v) in zip(left, right)
    )


def _route_exclusion(
    entity: dict,
    *,
    view: str,
    layer: str,
    code: str,
    message: str,
    length_raw: float | None = None,
    annotation: str = "",
    category: str = "unknown",
    bbox: list[float] | None = None,
) -> dict:
    source = entity.get("source") or {}
    return {
        "entityId": entity.get("id"),
        "handle": source.get("entityHandle"),
        "rootHandle": source.get("entityHandle"),
        "definitionHandle": entity.get("definitionHandle"),
        "instancePath": list(entity.get("instancePath") or []),
        "view": view,
        "type": entity.get("type"),
        "layer": layer,
        "originalLayer": source.get("originalLayer"),
        "layerInherited": source.get("layerInherited"),
        "layerOwnerPath": list(source.get("layerOwnerPath") or []),
        "expansionContexts": [
            {**item, "instancePath": list(item.get("instancePath") or [])}
            for item in (source.get("expansionContexts") or [])
        ],
        "lengthRaw": length_raw,
        "annotation": annotation,
        "category": category,
        "code": code,
        "message": message,
        "bbox": list(bbox) if bbox else None,
    }


def inspect_drawing(
    path: Path,
    file_id: str,
    workdir: Path,
    *,
    scope: dict | None = None,
) -> dict:
    read = read_drawing(
        Path(path), file_id, Path(workdir), scope=scope
    )
    report = read.report()
    units = report.get("units") or {}
    scale_text = units.get("metresPerDrawingUnit")
    scale = Decimal(str(scale_text)) if scale_text else None

    texts: list[dict] = []
    for entity in report.get("entities", []):
        if entity.get("type") in TEXT_TYPES:
            text = _entity_text(entity)
            if text:
                texts.append(
                    {
                        "text": text,
                        "layer": _text(entity.get("layer")),
                        "view": _text((entity.get("source") or {}).get("view")) or "model",
                        "bbox": (entity.get("source") or {}).get("bbox"),
                        "handle": (entity.get("source") or {}).get("entityHandle"),
                        "id": entity.get("id"),
                        "instancePath": list(entity.get("instancePath") or []),
                    }
                )

    equipment_groups: dict[tuple[str, str], dict] = {}
    route_groups: dict[tuple[str, str], dict] = {}
    unmeasured_routes: list[dict] = []
    view_texts: dict[str, list[str]] = defaultdict(list)
    for item in texts:
        view_texts[item["view"]].append(item["text"])
    floor_map, floor_regions, floor_issues = _assign_model_floors(
        report.get("entities", []), texts
    )
    text_index = _TextIndex(texts)
    annotations: list[dict] = []
    for item in texts:
        value = item.get("text", "")
        if not (
            any(marker.upper() in value.upper() for marker in PIPE_TEXT_MARKERS)
            or PAIR_DIAMETER_RE.search(value)
        ):
            continue
        parsed = _parse_pipe_specs(value)
        if parsed and any(entry.get("spec") for entry in parsed):
            annotations.append({**item, "specs": parsed})
    annotation_radii, annotation_radius_audit = _annotation_radii(
        report.get("entities", [])
    )

    def annotation_radius_for(view: str) -> float:
        return annotation_radii.get(view, max(1000.0 / 30.0, 10.0))

    drawing_extent = _drawing_extent(report.get("entities", []))

    # 先按“实例”确定每个物理设备的型号，再按型号归组；
    # 同名图块的多个实例不会互相投票，也不会把内部嵌套块算成设备。
    equipment_instances: dict[int, dict] = {}
    for entity in report.get("entities", []):
        if entity.get("type") != "INSERT":
            continue
        path = _instance_path(entity)
        if len(path) > 1:
            # 嵌套块是设备内部图形细节，不是独立设备。
            continue
        source = entity.get("source") or {}
        view = _text(source.get("view")) or "model"
        layer = _text(entity.get("layer"))
        bbox = _entity_bbox(entity)
        block = _text((entity.get("geometry") or {}).get("blockName"))
        if _is_equipment(entity):
            model_text, ambiguous, _ = _nearest_model_text(
                entity, view, bbox, layer, text_index, fallback=True
            )
            layer_marked = any(
                marker.upper() in layer.upper()
                for marker in EQUIPMENT_LAYER_MARKERS
            )
            block_role = _equipment_role(block)
            text_role = _equipment_role(model_text)
            nearby_conflict = ""
            if block_role and text_role and block_role != text_role:
                nearby_conflict = model_text
                model_text = ""
            kind = _equipment_kind(model_text, block, layer)
            role = block_role or _equipment_role(model_text, block)
            if not kind:
                kind = (
                    "室内机"
                    if role == "indoor"
                    else "室外机"
                    if role == "outdoor"
                    else ""
                )
            equipment_instances[id(entity)] = {
                "modelText": model_text,
                "ambiguous": ambiguous,
                "blockName": block,
                "instancePath": path,
                "view": view,
                "detectedBy": "layer" if layer_marked else "block",
                "modelSource": "visible_text" if model_text else "block",
                "role": role,
                "kind": kind,
                "nearbyModelConflict": nearby_conflict,
            }
            continue
        anonymous = _anonymous_equipment_candidate(
            entity,
            view=view,
            layer=layer,
            bbox=bbox,
            extent=drawing_extent,
            text_index=text_index,
        )
        if anonymous is not None:
            anonymous["instancePath"] = path
            anonymous["view"] = view
            equipment_instances[id(entity)] = anonymous

    equipment_boxes_by_view: dict[
        str, list[tuple[float, float, float, float, float]]
    ] = defaultdict(list)
    for entity in report.get("entities", []):
        if id(entity) not in equipment_instances:
            continue
        box = _entity_bbox(entity)
        if not box or len(box) != 4:
            continue
        view = _text((entity.get("source") or {}).get("view")) or "model"
        equipment_boxes_by_view[view].append(
            (
                float(box[0]),
                float(box[1]),
                float(box[2]),
                float(box[3]),
                max(float(box[2]) - float(box[0]), float(box[3]) - float(box[1])),
            )
        )
    excluded_routes: list[dict] = []

    route_entities = [
        entity
        for entity in report.get("entities", [])
        if entity.get("type") in ROUTE_TYPES and entity.get("type") != "CIRCLE"
    ]
    native_branch_symbols = _native_branch_symbols(read, report.get("entities", []))
    native_branch_connections = _native_branch_connections(report.get("entities", []), native_branch_symbols)
    topology_proposals: dict[str, dict] = {}
    route_semantic_hints = {}
    for candidate in route_entities:
        candidate_view = _text((candidate.get("source") or {}).get("view")) or "model"
        semantic = _route_layer_semantics(_text(candidate.get("layer")))["medium"]
        if (native_branch_symbols.get(str(candidate.get("id"))) or native_branch_connections.get(str(candidate.get("id")))) and semantic == "unknown":
            if any(any(entry.get("pairSpec") for entry in item.get("specs") or [])
                   and item.get("view") == candidate_view
                   and (center := _bbox_center(item.get("bbox"))) is not None
                   and (distance := _entity_point_distance(candidate, center)) is not None
                   and distance <= annotation_radius_for(candidate_view)
                   for item in annotations):
                semantic = "refrigerant"
        route_semantic_hints[str(candidate.get("id"))] = semantic

    medium_seed_candidates = [candidate for candidate in route_entities
                              if route_semantic_hints[str(candidate.get("id"))] != "unknown"]
    for candidate in route_entities:
        candidate_id = str(candidate.get("id"))
        if route_semantic_hints[candidate_id] != "unknown" or _native_style_key(candidate) is None:
            continue
        view = _text((candidate.get("source") or {}).get("view")) or "model"
        seeds = [{"entityId": other.get("id"), "view": view,
                  "sourceRegionKey": _source_region_key(other, view),
                  "medium": route_semantic_hints[str(other.get("id"))], "points": _entity_points(other)}
                 for other in medium_seed_candidates if other.get("id") != candidate.get("id")
                 and (_text((other.get("source") or {}).get("view")) or "model") == view
                 and _native_style_key(other) == _native_style_key(candidate)
                 and route_semantic_hints[str(other.get("id"))] != "unknown"]
        contacts = _endpoint_connections(candidate, [], seeds, view=view,
            source_region_key=_source_region_key(candidate, view), candidate_medium="unknown")
        media = {contact["targetMedium"] for contact in contacts}
        if len(media) == 1:
            route_semantic_hints[candidate_id] = next(iter(media))
            topology_proposals[candidate_id] = {"kind": "same_style_exact_route_contact",
                "medium": next(iter(media)), "connections": contacts, "specs": [], "engineeringApproval": False}

    topology_seeds = []
    for candidate in route_entities:
        ref = candidate.get("source") or {}
        semantics = _route_layer_semantics(_text(candidate.get("layer")))
        length, issue = _line_length(candidate)
        if (semantics.get("strength") == "explicit" and not ref.get("layerInherited")
                and issue is None and length and _parse_pipe_specs(_text(candidate.get("layer")))):
            view = _text(ref.get("view")) or "model"
            topology_seeds.append({"entityId": candidate.get("id"), "view": view,
                "sourceRegionKey": _source_region_key(candidate, view),
                "medium": semantics["medium"], "points": _entity_points(candidate)})
    topology_supported = set()
    topology_route_supported = set()
    for candidate in route_entities:
        view = _text((candidate.get("source") or {}).get("view")) or "model"
        contacts = _endpoint_connections(candidate, equipment_boxes_by_view.get(view, []),
            topology_seeds, view=view, source_region_key=_source_region_key(candidate, view),
            candidate_medium=route_semantic_hints.get(str(candidate.get("id")), "unknown"))
        if contacts:
            topology_supported.add(str(candidate.get("id")))
        if any(contact.get("kind") == "route_endpoint_contact" for contact in contacts):
            topology_route_supported.add(str(candidate.get("id")))

    annotation_owner: dict[str, str | None] = {}
    for item in annotations:
        text_center = _bbox_center(item.get("bbox"))
        if text_center is None:
            continue
        best_id: str | None = None
        best_distance = float("inf")
        tie = False
        best_rank = 2
        annotation_kind = (_annotation_semantics(item) or {}).get("kind")
        for candidate in route_entities:
            candidate_view = _text((candidate.get("source") or {}).get("view")) or "model"
            if candidate_view != item.get("view"):
                continue
            hint = route_semantic_hints.get(str(candidate.get("id")), "unknown")
            if ((annotation_kind == "dn" and hint == "refrigerant")
                    or (annotation_kind == "pair" and hint not in {"unknown", "refrigerant"})):
                continue
            distance = _entity_point_distance(candidate, text_center)
            if distance is None or distance > annotation_radius_for(candidate_view) * 6:
                continue
            render = (candidate.get("geometry") or {}).get("renderColor") or {}
            weak = (render.get("effectiveSource") in {"unresolved_byblock", "invalid_color"}
                    or render.get("effectiveAciColor") in {0, 252})
            rank = int(weak and not _is_pipe(candidate)
                       and not native_branch_symbols.get(str(candidate.get("id")))
                       and str(candidate.get("id")) not in topology_supported)
            # A weak wall must not steal a nearby diameter from a corroborated
            # route. If no corroborated route is close, keep its raw annotation
            # ownership for audit; material identity is still unresolved below.
            if ((annotation_kind == "pair" and hint == "refrigerant")
                    or (annotation_kind == "dn" and hint not in {"unknown", "refrigerant"})
                    or str(candidate.get("id")) in topology_route_supported):
                rank = -1
            if distance > annotation_radius_for(candidate_view):
                rank = 1
            if rank < best_rank or (rank == best_rank and distance < best_distance - 1e-9):
                best_rank = rank
                best_id = str(candidate.get("id"))
                best_distance = distance
                tie = False
            elif rank == best_rank and abs(distance - best_distance) <= 1e-9:
                tie = True
        annotation_owner[str(item.get("id"))] = None if tie else best_id
    annotation_specs_by_owner: dict[str, list[dict]] = defaultdict(list)
    for item in annotations:
        owner = annotation_owner.get(str(item.get("id")))
        if not owner:
            continue
        for entry in item.get("specs") or []:
            if entry.get("spec"):
                annotation_specs_by_owner[owner].append(entry)

    air_equipment_boxes: dict[str, list] = defaultdict(list)
    for equipment in report.get("entities", []):
        instance = equipment_instances.get(id(equipment))
        box = _entity_bbox(equipment)
        if instance and instance.get("role") in {"indoor", "outdoor"} and box:
            equipment_view = _text((equipment.get("source") or {}).get("view")) or "model"
            air_equipment_boxes[equipment_view].append((*box, max(box[2] - box[0], box[3] - box[1])))
    dn_seeds = []
    for candidate in route_entities:
        view = _text((candidate.get("source") or {}).get("view")) or "model"
        owned = annotation_specs_by_owner.get(str(candidate.get("id")), [])
        values = {str(entry.get("spec")) for entry in owned if entry.get("spec")}
        if len(values) != 1 or any(entry.get("pairSpec") for entry in owned):
            continue
        contacts = _endpoint_connections(candidate, air_equipment_boxes.get(view, []), [],
            view=view, source_region_key=_source_region_key(candidate, view), candidate_medium="unknown")
        if contacts and _native_style_key(candidate) is not None:
            dn_seeds.append({"entityId": candidate.get("id"), "view": view,
                "sourceRegionKey": _source_region_key(candidate, view), "medium": "drain",
                "points": _entity_points(candidate), "specs": owned,
                "styleKey": _native_style_key(candidate)})
    for candidate in route_entities:
        candidate_id = str(candidate.get("id"))
        if route_semantic_hints[candidate_id] != "unknown" or _native_style_key(candidate) is None:
            continue
        view = _text((candidate.get("source") or {}).get("view")) or "model"
        seeds = [seed for seed in dn_seeds if seed["styleKey"] == _native_style_key(candidate)]
        contacts = _endpoint_connections(candidate, [], seeds, view=view,
            source_region_key=_source_region_key(candidate, view), candidate_medium="drain")
        contacted = [seed for seed in seeds if any(c["targetId"] == seed["entityId"] for c in contacts)]
        specs = {str(entry.get("spec")) for seed in contacted for entry in seed["specs"]}
        if contacts and len(specs) == 1:
            topology_proposals[candidate_id] = {"kind": "same_style_exact_dn_contact",
                "medium": "drain", "connections": contacts,
                "specs": list({(entry.get("spec"), entry.get("role")): entry
                               for entry in contacted[0]["specs"]}.values()),
                "engineeringApproval": False}

    def nearest_annotation_any(entity: dict, view: str) -> dict | None:
        points = _entity_points(entity)
        if not points or not annotations:
            return None
        center = (
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
        )
        best = None
        best_distance = annotation_radius_for(view)
        for item in annotations:
            if item.get("view") != view:
                continue
            text_center = _bbox_center(item.get("bbox"))
            if text_center is None:
                continue
            distance = _entity_point_distance(entity, text_center)
            if distance is None:
                distance = math.dist(center, text_center)
            if distance < best_distance:
                best, best_distance = item, distance
        return best

    def nearest_annotation(entity: dict, view: str) -> dict | None:
        points = _entity_points(entity)
        if not points or not annotations:
            return None
        center = (
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
        )
        best = None
        best_distance = annotation_radius_for(view)
        for item in annotations:
            if item.get("view") != view:
                continue
            if annotation_owner.get(str(item.get("id"))) != entity.get("id"):
                continue
            text_center = _bbox_center(item.get("bbox"))
            if text_center is None:
                continue
            distance = _entity_point_distance(entity, text_center)
            if distance is None:
                distance = math.dist(center, text_center)
            if distance < best_distance:
                best, best_distance = item, distance
        return best

    def nearest_any_text(entity: dict, view: str) -> tuple[dict | None, float]:
        points = _entity_points(entity)
        if not points or not texts:
            return None, float("inf")
        best = None
        best_distance = float("inf")
        for item in texts:
            if item.get("view") != view:
                continue
            text_center = _bbox_center(item.get("bbox"))
            if text_center is None:
                continue
            distance = _entity_point_distance(entity, text_center)
            if distance is None:
                continue
            if distance < best_distance:
                best, best_distance = item, distance
        return best, best_distance

    def annotations_near(entity: dict, view: str) -> list[dict]:
        points = _entity_points(entity)
        if not points or not annotations:
            return []
        center = (
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
        )
        found: list[dict] = []
        for item in annotations:
            if item.get("view") != view:
                continue
            text_center = _bbox_center(item.get("bbox"))
            if text_center is None:
                continue
            distance = _entity_point_distance(entity, text_center)
            if distance is None:
                distance = math.dist(center, text_center)
            if distance <= annotation_radius_for(view) * 6:
                found.append(item)
        return found

    equipment_roots = {
        (view, tuple(instance["instancePath"]))
        for instance in equipment_instances.values()
        if (view := _text(instance.get("view"))) and instance.get("instancePath")
    }
    strong_segments: list[dict] = []
    for entity in report.get("entities", []):
        if entity.get("type") not in ROUTE_TYPES or entity.get("type") == "CIRCLE":
            continue
        source = entity.get("source") or {}
        if source.get("layerInherited"):
            continue
        semantics = _route_layer_semantics(_text(entity.get("layer")))
        # An alias restores candidate coverage but its drawing/scope is still
        # unconfirmed. Only a native explicit system layer can seed topology.
        if semantics.get("strength") != "explicit":
            continue
        entity_view = _text(source.get("view")) or "model"
        entity_path = _instance_path(entity)
        if any(
            entity_view == root_view
            and entity_path[: len(root_path)] == root_path
            for root_view, root_path in equipment_roots
        ):
            continue
        # Topology may only be seeded by a route whose own geometry and source
        # are already verifiable. Unsupported curves, closed outlines and
        # approximated arcs stay review-only and must not upgrade a weak branch.
        length_raw, length_issue = _line_length(entity)
        if length_issue is not None or length_raw is None or length_raw <= 0:
            continue
        layer_specs = _parse_pipe_specs(_text(entity.get("layer")))
        owner_specs = annotation_specs_by_owner.get(str(entity.get("id")), [])
        if not any(entry.get("spec") for entry in [*layer_specs, *owner_specs]):
            continue
        points = _entity_points(entity)
        if len(points) >= 2:
            strong_segments.append(
                {
                    "entityId": entity.get("id"),
                    "view": entity_view,
                    "sourceRegionKey": _source_region_key(entity, entity_view),
                    "medium": semantics.get("medium"),
                    "points": points,
                }
            )

    for entity in report.get("entities", []):
        source = entity.get("source") or {}
        layer = _text(entity.get("layer"))
        view = _text(source.get("view")) or "model"
        handle = _text(source.get("entityHandle"))
        floor = floor_map.get(handle) or _floor_label(
            view, view_texts.get(view, [])
        )
        entity_path = _instance_path(entity)
        is_route_type = entity.get("type") in ROUTE_TYPES
        symbol_geometry = bool(
            is_route_type
            and len(entity_path) > 1
            and any(
                view == root_view
                and entity_path[: len(root_path)] == root_path
                for root_view, root_path in equipment_roots
            )
        )
        layer_owner_path = list(source.get("layerOwnerPath") or [])
        native_route_leaf = bool(
            str(source.get("originalLayer") or "") not in {"", "0"}
            and not source.get("layerInherited")
        )
        nested_owner_route = bool(
            source.get("layerInherited")
            and len(layer_owner_path) > 1
            and _route_layer_semantics(layer).get("candidate")
        )
        equipment_internal_route = bool(
            symbol_geometry and (native_route_leaf or nested_owner_route)
        )
        if symbol_geometry and not equipment_internal_route:
            # 设备符号内部的线/弧/多段线只用于显示，不是敷设管线；保留审计。
            length_raw, _ = _line_length(entity)
            excluded_routes.append(
                _route_exclusion(
                    entity,
                    view=view,
                    layer=layer,
                    code="EQUIPMENT_SYMBOL_GEOMETRY",
                    message="该图元位于已识别设备图块内部，仅用于图形显示，不计入管线。",
                    length_raw=length_raw,
                    bbox=_entity_bbox(entity),
                )
            )
            continue
        if is_route_type:
            inherited_code = _inherited_context_route_rejection(entity)
            if inherited_code:
                length_raw, _ = _line_length(entity)
                excluded_routes.append(
                    _route_exclusion(
                        entity,
                        view=view,
                        layer=layer,
                        code=inherited_code,
                        message=(
                            "该图元继承自被保留展开的图块图层，属于符号/背景几何，"
                            "默认不计入管线；如确认是块内真实管线，请人工补录。"
                        ),
                        length_raw=length_raw,
                        bbox=_entity_bbox(entity),
                    )
                )
                continue
        instance = equipment_instances.get(id(entity))
        if instance is not None:
            block = instance["blockName"]
            model_text = str(instance.get("modelText") or "")
            role = str(instance.get("role") or "")
            key = (
                view,
                floor,
                layer,
                block,
                model_text,
                role,
                instance["detectedBy"],
            )
            group = equipment_groups.setdefault(
                key,
                {
                    "kind": "equipment",
                    "key": (
                        f"equipment|{view}|{floor}|{layer}|{block}|"
                        f"{model_text or '-'}|{role or '-'}|"
                        f"{instance['detectedBy']}"
                    ),
                    "ruleKey": (
                        f"equipment|{layer}|{block}|"
                        f"{model_text or '-'}|{role or '-'}"
                    ),
                    "view": view,
                    "floorLabel": floor,
                    "layer": layer,
                    "blockName": block,
                    "role": role,
                    "equipmentKind": str(instance.get("kind") or ""),
                    "detectedBy": instance["detectedBy"],
                    "quantity": 0,
                    "unit": "台",
                    "modelHint": "",
                    "modelFromXdata": "",
                    "modelFromText": model_text,
                    "modelSource": instance["modelSource"],
                    "modelConflict": False,
                    "textHints": [],
                    "handles": [],
                    "instances": [],
                    "bbox": None,
                    "instanceBboxes": [],
                    "issues": (
                        ["EQUIPMENT_MODEL_FROM_ANNOTATION"]
                        if instance["detectedBy"] == "annotation"
                        else []
                    ),
                },
            )
            group["quantity"] += 1
            conflict_text = str(instance.get("nearbyModelConflict") or "")
            if conflict_text:
                message = (
                    f"邻近文字“{conflict_text}”与图块“{block}”的角色不一致，"
                    "未采用该型号；保留图块型号/角色待人工确认。"
                )
                if message not in group["issues"]:
                    group["issues"].append(message)
            group["handles"].append(source.get("entityHandle"))
            bbox = _entity_bbox(entity)
            if bbox:
                group["instanceBboxes"].append(list(bbox))
                if group["bbox"] is None:
                    group["bbox"] = list(bbox)
                else:
                    group["bbox"] = [
                        min(group["bbox"][0], bbox[0]),
                        min(group["bbox"][1], bbox[1]),
                        max(group["bbox"][2], bbox[2]),
                        max(group["bbox"][3], bbox[3]),
                    ]
            xdata = _xdata_values(entity)
            xdata_model = _model_hint(xdata, "")
            instance_conflict = bool(
                model_text
                and xdata_model
                and _text(model_text).upper() != _text(xdata_model).upper()
            )
            if xdata_model and not group["modelFromXdata"]:
                group["modelFromXdata"] = xdata_model
            if instance_conflict:
                group["modelConflict"] = True
            group["instances"].append(
                {
                    "handle": source.get("entityHandle"),
                    "instancePath": list(instance["instancePath"]),
                    "definitionHandle": entity.get("definitionHandle"),
                    "bbox": list(bbox) if bbox else None,
                    "model": model_text or xdata_model,
                    "modelFromText": model_text,
                    "modelFromXdata": xdata_model,
                    "modelSource": instance["modelSource"],
                    "modelConflict": instance_conflict,
                    "detectedBy": instance["detectedBy"],
                    "role": role,
                }
            )
            group["category"] = (
                (xdata.get("产品大类") or [""])[0]
                or (xdata.get("型式") or [""])[0]
            )
            nearby = [
                item
                for item in text_index.nearby(view, bbox, layer=layer)
                if item.get("view") == view
            ]
            for item in nearby:
                text = item.get("text", "")
                if text and text not in group["textHints"] and len(group["textHints"]) < 12:
                    group["textHints"].append(text)

        is_pipe_layer = _is_pipe(entity)
        if (
            entity.get("type") == "CIRCLE"
            and _route_layer_semantics(layer)["candidate"]
        ):
            excluded_routes.append(
                _route_exclusion(
                    entity,
                    view=view,
                    layer=layer,
                    code="ROUTE_SYMBOL_CIRCLE",
                    message="圆/弧符号用于表达管件或设备，不作为敷设长度累加。",
                    length_raw=None,
                    bbox=_entity_bbox(entity),
                )
            )
            continue
        nearby_annotation = (
            nearest_annotation_any(entity, view)
            if entity.get("type") in ROUTE_TYPES
            else None
        )
        owned_annotation = (
            nearest_annotation(entity, view)
            if entity.get("type") in ROUTE_TYPES
            else None
        )
        is_annotation_pipe = bool(
            not is_pipe_layer
            and (nearby_annotation or str(entity.get("id")) in topology_proposals)
            and layer in {"0", ""}
            and entity.get("type") in ROUTE_TYPES
        )
        if is_annotation_pipe:
            length_raw, _ = _line_length(entity)
            if (
                length_raw is None
                or (length_raw < annotation_radius_for(view) * 0.2
                    and str(entity.get("id")) not in topology_proposals)
            ):
                is_annotation_pipe = False
            elif nearby_annotation is not None:
                ann_center = _bbox_center(nearby_annotation.get("bbox"))
                ann_distance = (
                    _entity_point_distance(entity, ann_center)
                    if ann_center is not None
                    else None
                )
                any_item, any_distance = nearest_any_text(entity, view)
                if (
                    ann_distance is None
                    or ann_distance > annotation_radius_for(view) * 0.6
                ):
                    # 图层 0 的弱候选必须紧邻管径标注，避免把墙线/长引线算入。
                    is_annotation_pipe = False
                elif (
                    any_item is not None
                    and any_distance < ann_distance
                    and _is_equipment_note(str(any_item.get("text") or ""))
                ):
                    # 更近的是设备/说明注记（如“室内机146W/220V”），
                    # 该线是引线而不是敷设管线。
                    is_annotation_pipe = False
        if not (is_pipe_layer or is_annotation_pipe):
            continue
        bbox = _entity_bbox(entity)
        length_raw, length_issue = _line_length(entity)
        source_region_key = _source_region_key(entity, view)
        layer_semantics = _route_layer_semantics(layer)
        native_layer = bool(
            str(source.get("originalLayer") or "") not in {"", "0"}
            and not source.get("layerInherited")
        )
        semantic_medium = (
            str(layer_semantics.get("medium") or "unknown")
            if native_layer
            else "unknown"
        )
        semantic_strength = (
            str(layer_semantics.get("strength") or "none")
            if native_layer
            else ("generic" if is_pipe_layer else "none")
        )
        nearby_is_pair = bool(
            nearby_annotation
            and any(
                entry.get("pairSpec")
                for entry in nearby_annotation.get("specs") or []
            )
        )
        candidate_medium = semantic_medium
        if candidate_medium == "unknown" and nearby_is_pair:
            candidate_medium = "refrigerant"
        if candidate_medium == "unknown" and nearby_annotation is not None:
            candidate_medium = _route_medium(
                "", [str(nearby_annotation.get("text") or "")]
            )
        connections = _endpoint_connections(
            entity,
            equipment_boxes_by_view.get(view, []),
            strong_segments,
            view=view,
            source_region_key=source_region_key,
            candidate_medium=candidate_medium,
        )
        equipment_contact = any(
            item.get("kind") == "equipment_bbox_contact"
            for item in connections
        )
        route_contacts = [
            item
            for item in connections
            if item.get("kind") == "route_endpoint_contact"
        ]
        connected = any(item.get("compatible") for item in route_contacts)
        incompatible_route_contact = bool(route_contacts) and not connected
        if is_annotation_pipe:
            rejection = _weak_route_rejection(
                entity,
                bbox,
                annotation_radius_for(view),
                equipment_boxes_by_view.get(view, []),
                has_endpoint_connection=connected or str(entity.get("id")) in topology_proposals,
                preserve_pair_candidate=nearby_is_pair,
            )
            if rejection is not None:
                code, message = rejection
                excluded_routes.append(
                    _route_exclusion(
                        entity,
                        view=view,
                        layer=layer,
                        code=code,
                        message=message,
                        length_raw=length_raw,
                        annotation=str(
                            (nearby_annotation or {}).get("text") or ""
                        ),
                        category=_route_category(
                            layer,
                            [
                                str(
                                    (nearby_annotation or {}).get("text")
                                    or ""
                                )
                            ],
                        ),
                        bbox=bbox,
                    )
                )
                continue
        annotation_unassigned = bool(
            is_annotation_pipe and owned_annotation is None
        )
        nearby_items = text_index.nearby(
            view, bbox, layer=layer, expand_cells=4
        )
        annotation = owned_annotation or (
            nearby_annotation if annotation_unassigned else None
        )
        if annotation is not None and annotation not in nearby_items:
            nearby_items.append(annotation)
        nearby_texts = [item.get("text", "") for item in nearby_items]
        near_annotations = [
            item
            for item in annotations_near(entity, view)
            if item.get("view") == view
            and annotation_owner.get(str(item.get("id"))) == entity.get("id")
        ]

        def annotation_distance(item: dict) -> float:
            center = _bbox_center(item.get("bbox"))
            if center is None:
                return float("inf")
            value = _entity_point_distance(entity, center)
            return float("inf") if value is None else value

        classified = [
            (item, _annotation_semantics(item)) for item in near_annotations
        ]
        pair_items = [
            item for item, semantics in classified
            if semantics and semantics["kind"] == "pair"
        ]
        dn_items = [
            item for item, semantics in classified
            if semantics and semantics["kind"] == "dn"
        ]
        pair_distance = min(
            (annotation_distance(item) for item in pair_items),
            default=float("inf"),
        )
        dn_distance = min(
            (annotation_distance(item) for item in dn_items),
            default=float("inf"),
        )
        layer_specs = (
            _parse_pipe_specs(layer) if native_layer else []
        )
        for entry in layer_specs:
            entry["specSource"] = "layer"
            entry["annotation"] = layer

        def specs_from(items: list[dict]) -> list[dict]:
            for item in sorted(items, key=annotation_distance):
                found = list(item.get("specs") or [])
                if found:
                    return found
            return []

        pair_specs = specs_from(pair_items)
        dn_specs = specs_from(dn_items)
        pair_signatures = {
            tuple(
                sorted(
                    (
                        str(entry.get("spec") or ""),
                        str(entry.get("role") or ""),
                        str(entry.get("pairSpec") or ""),
                    )
                    for entry in item.get("specs") or []
                    if entry.get("spec")
                )
            )
            for item in pair_items
        }
        pair_signatures.discard(())
        dn_counts = Counter(
            str(entry.get("spec") or "")
            for item in dn_items
            for entry in item.get("specs") or []
            if entry.get("spec")
        )
        dn_values = set(dn_counts)
        dominant_dn = None
        if dn_counts:
            ordered = dn_counts.most_common()
            if len(ordered) == 1 or ordered[0][1] > ordered[1][1]:
                dominant_dn = ordered[0][0]
        spec_ambiguous = (
            len(pair_signatures) > 1 and dominant_dn is None
        ) or (len(dn_values) > 1 and dominant_dn is None)

        review_reasons: list[str] = []
        alternatives: list[dict] = []
        if equipment_internal_route:
            review_reasons.append("EQUIPMENT_INTERNAL_ROUTE_REVIEW")
        if incompatible_route_contact:
            review_reasons.append("PIPE_CROSS_MEDIUM_CONTACT")
        conflict = bool(
            pair_items and dn_items and semantic_medium == "unknown"
        )
        medium = semantic_medium
        native_symbols = native_branch_symbols.get(str(entity.get("id")), [])
        native_ports = native_branch_connections.get(str(entity.get("id")), [])
        topology_proposal = topology_proposals.get(str(entity.get("id"))) or {}
        symbol_refrigerant = bool((native_symbols or native_ports) and pair_items and semantic_medium == "unknown")
        if symbol_refrigerant:
            medium = "refrigerant"
        elif topology_proposal:
            medium = topology_proposal["medium"]
        if native_ports or topology_proposal:
            review_reasons.append("PIPE_TOPOLOGY_PROPOSAL")
        if medium == "unknown" and not conflict:
            text_medium = _route_medium("", nearby_texts)
            if text_medium not in {"", "unknown"}:
                medium = text_medium
        if medium == "unknown" and pair_items and not dn_items:
            medium = "refrigerant"
        dn_equipment_proposal = bool(
            medium == "unknown"
            and (equipment_contact or connected)
            and dn_items
            and not pair_items
        )
        if dn_equipment_proposal:
            # DN alone is not a global medium rule. A single DN branch that
            # actually touches equipment is kept as a drain proposal, review.
            medium = "drain"
        forced_specs: list[dict] = []
        if conflict:
            review_reasons.append("PIPE_MEDIUM_CONFLICT")
            if dominant_dn and not symbol_refrigerant:
                # Several independent DN notes can outvote one nearby pair
                # note; this remains a proposal and never automatic.
                medium = "drain"
                forced_specs = [
                    entry
                    for entry in dn_specs
                    if str(entry.get("spec") or "") == dominant_dn
                ][:1]
            for item, semantics in classified:
                if semantics is None:
                    continue
                alternatives.append(
                    {
                        "annotationId": item.get("id"),
                        "text": str(item.get("text") or ""),
                        "medium": semantics["medium"],
                        "specs": list(semantics["specs"]),
                    }
                )
        if forced_specs:
            specs = forced_specs
        elif medium in {"drain", "condensate", "supply", "return", "cooling", "makeup"}:
            specs = dn_specs or layer_specs or topology_proposal.get("specs", [])
            if not specs:
                specs = layer_specs
        elif medium == "refrigerant":
            specs = layer_specs or pair_specs or dn_specs
        else:
            specs = [] if conflict else (layer_specs or pair_specs or dn_specs)
        if not specs:
            specs = [
                {
                    "spec": "",
                    "role": "",
                    "pairSpec": "",
                    "specSource": "none",
                    "annotation": "",
                }
            ]
        if spec_ambiguous:
            review_reasons.append("PIPE_SPEC_AMBIGUOUS")
            specs = [
                {
                    "spec": "",
                    "role": "",
                    "pairSpec": "",
                    "specSource": "ambiguous",
                    "annotation": " / ".join(
                        str(item.get("text") or "") for item in near_annotations[:4]
                    ),
                }
            ]
        if not any(str(entry.get("spec") or "") for entry in specs):
            review_reasons.append("PIPE_SPEC_UNRESOLVED")
        if medium == "unknown" or dn_equipment_proposal:
            review_reasons.append("PIPE_IDENTITY_UNRESOLVED")
        if annotation_unassigned:
            review_reasons.append("PIPE_ANNOTATION_UNASSIGNED")
        if length_raw is None:
            review_reasons.append("GEOMETRY_UNVERIFIED")
        if is_pipe_layer and not native_layer:
            review_reasons.append("PIPE_LAYER_INHERITED")
        if (
            is_annotation_pipe
            and not connected
        ):
            review_reasons.append("PIPE_IDENTITY_UNRESOLVED")
        if (
            is_annotation_pipe
            and _weak_route_style(entity)
            and medium in {"unknown", "refrigerant"}
        ):
            review_reasons.append("PIPE_STYLE_REVIEW")
        route_medium = medium
        category = (
            _category_from_medium(medium)
            if medium != "unknown"
            else "unknown"
        )
        annotation_text = str((annotation or {}).get("text") or "")
        render = (entity.get("geometry") or {}).get("renderColor") or {}
        unresolved_identity = bool(
            is_annotation_pipe and not connected and not equipment_contact and not native_symbols
            and (render.get("effectiveSource") in {"unresolved_byblock", "invalid_color"}
                 or render.get("effectiveAciColor") in {0, 252})
        )
        if unresolved_identity:
            medium = route_medium = "unknown"
            category = "unknown"
            roles = list(dict.fromkeys(str(entry.get("role") or "") for entry in specs)) or [""]
            specs = [{"spec": "", "role": role, "pairSpec": "", "specSource": "none"} for role in roles]
            if "PIPE_IDENTITY_UNRESOLVED" not in review_reasons:
                review_reasons.append("PIPE_IDENTITY_UNRESOLVED")
        pair_annotation = bool(
            medium == "refrigerant" and pair_items
            and (native_symbols or pair_distance <= dn_distance * 0.8)
        )
        split = (
            _split_route_by_annotations(
                entity,
                pair_items,
                length_raw,
                route_medium,
                annotation_radius_for(view) * 0.5,
                boundary_symbols=native_symbols,
            )
            if pair_annotation
            else None
        )
        segments = [
            {
                "t0": 0.0,
                "t1": 1.0,
                "lengthRaw": length_raw,
                "specs": specs,
                "annotation": annotation_text,
                "medium": route_medium,
                "boundaryInferred": False,
            }
        ]
        if split:
            segments = split
            review_reasons = [reason for reason in review_reasons
                              if reason not in {"PIPE_SPEC_AMBIGUOUS", "PIPE_SPEC_UNRESOLVED"}]
            if "PIPE_SEGMENT_BOUNDARY_INFERRED" not in review_reasons:
                review_reasons.append("PIPE_SEGMENT_BOUNDARY_INFERRED")
        elif medium == "refrigerant" and len(pair_signatures) > 1:
            # Preserve a shared liquid diameter while leaving the differing gas
            # diameter unknown. Multiple symbols/notes are not a license to put
            # the nearest diameter on the complete physical run.
            by_role: dict[str, dict[str, dict]] = defaultdict(dict)
            for item in pair_items:
                for entry in item.get("specs") or []:
                    by_role[str(entry.get("role") or "")][str(entry.get("spec") or "")] = entry
            specs = []
            for role, values in by_role.items():
                if len(values) == 1:
                    specs.append(dict(next(iter(values.values()))))
                else:
                    specs.append({"spec": "", "role": role, "pairSpec": "",
                                  "specSource": "ambiguous", "annotation": " / ".join(values)})
            segments[0]["specs"] = specs
            for reason in ("PIPE_SEGMENT_BOUNDARY_AMBIGUOUS", "PIPE_SPEC_UNRESOLVED"):
                if reason not in review_reasons:
                    review_reasons.append(reason)
        if connected and is_annotation_pipe and not conflict and not spec_ambiguous:
            # A tight endpoint contact plus one assigned annotation is the only
            # layer-0 branch allowed to keep automatic eligibility.
            review_reasons = [
                reason
                for reason in review_reasons
                if reason not in {"PIPE_IDENTITY_UNRESOLVED", "PIPE_LAYER_UNCERTAIN"}
            ]
        review_reasons = list(dict.fromkeys(review_reasons))
        eligible = bool(
            length_raw is not None
            and medium in {
                "refrigerant",
                "condensate",
                "drain",
                "supply",
                "return",
                "cooling",
                "makeup",
            }
            and any(str(entry.get("spec") or "") for entry in specs)
            and not conflict
            and not spec_ambiguous
            and not review_reasons
            and (
                (native_layer and semantic_strength in {"explicit", "alias"})
                or (is_annotation_pipe and connected)
            )
        )
        eligibility = "eligible" if eligible else "review"
        if native_layer and semantic_strength == "generic":
            if "PIPE_LAYER_UNCERTAIN" not in review_reasons:
                review_reasons.append("PIPE_LAYER_UNCERTAIN")
        if native_layer and semantic_strength == "alias":
            # ACS aliases restore candidate coverage, but the professional
            # layer's drawing/duplicate scope still needs human confirmation.
            review_reasons.append("PIPE_SOURCE_SCOPE_UNCONFIRMED")
        if (
            is_annotation_pipe
            and not connected
            and "PIPE_LAYER_UNCERTAIN" not in review_reasons
        ):
            review_reasons.append("PIPE_LAYER_UNCERTAIN")
        if review_reasons:
            eligibility = "review"
        evidence = {
            "layerRule": layer_semantics.get("rule") or None,
            "layerStrength": semantic_strength,
            "annotationIds": [
                item.get("id") for item in near_annotations if item.get("id")
            ],
            "annotationAlternatives": alternatives,
            "connections": connections,
            "nativeBranchSymbols": native_symbols,
            "nativeBranchConnections": native_ports,
            "topologyProposal": topology_proposal,
            "renderColor": (entity.get("geometry") or {}).get("renderColor") or {},
            "renderStyle": (entity.get("geometry") or {}).get("renderStyle") or {},
        }
        diameter_kind = (
            "od_pair"
            if any(entry.get("pairSpec") for entry in specs)
            else "dn"
            if any(entry.get("spec") for entry in specs)
            else "unknown"
        )
        for segment in segments:
            segment_length = segment.get("lengthRaw")
            segment_medium = str(segment.get("medium") or medium)
            for spec_entry in segment.get("specs") or specs:
                spec = str(spec_entry.get("spec") or "")
                role = str(spec_entry.get("role") or (category if spec else ""))
                route_category = category
                review_signature = tuple(sorted(set(review_reasons)))
                key = (
                    view,
                    floor,
                    source_region_key,
                    layer,
                    entity.get("type", ""),
                    route_category,
                    segment_medium,
                    diameter_kind,
                    spec,
                    role,
                    eligibility,
                    review_signature,
                )
                physical_segment_id = (
                    f"{entity.get('id')}:{segment.get('t0')}:{segment.get('t1')}"
                )
                group = route_groups.setdefault(
                    key,
                    {
                        "kind": "route",
                        "key": (
                            f"route|{view}|{floor}|{source_region_key}|{layer}|"
                            f"{entity.get('type', '')}|{route_category}|"
                                f"{segment_medium}|{diameter_kind}|"
                                f"{spec or 'nospec'}|{role or 'unknown'}|"
                                f"{eligibility}|"
                                + hashlib.sha256(
                                    "|".join(review_signature).encode("utf-8")
                                ).hexdigest()[:8]
                            ),
                        "ruleKey": (
                            f"route|{layer}|{entity.get('type', '')}|"
                            f"{route_category}|{spec or 'nospec'}|"
                            f"{role or 'unknown'}"
                        ),
                        "view": view,
                        "floorLabel": floor,
                        "sourceRegionKey": source_region_key,
                        "layer": layer,
                        "entityType": entity.get("type"),
                        "count": 0,
                        "lengthRaw": None,
                        "lengthM": None,
                        "lengthKnownRaw": 0.0,
                        "lengthKnownM": (
                            0.0 if scale is not None else None
                        ),
                        "unmeasuredSegmentCount": 0,
                        "lengthStatus": "unmeasured",
                        "category": route_category,
                        "medium": segment_medium,
                        "diameterKind": diameter_kind,
                        "role": role,
                        "spec": spec,
                        "pairSpec": str(spec_entry.get("pairSpec") or ""),
                        "specSource": str(spec_entry.get("specSource") or "none"),
                        "annotation": str(
                            segment.get("annotation")
                            or spec_entry.get("annotation")
                            or ""
                        ),
                        "detectedBy": "layer" if is_pipe_layer else "annotation",
                        "layerCertain": bool(
                            is_pipe_layer
                            and native_layer
                            and semantic_strength in {"explicit", "alias"}
                        ),
                        "weakEvidence": (not is_pipe_layer),
                        "closed": bool(
                            (entity.get("geometry") or {}).get("closed")
                        ),
                        "bbox": (
                            list(bbox) if (bbox := _entity_bbox(entity)) else None
                        ),
                        "autoInclude": eligibility == "eligible",
                        "eligibility": eligibility,
                        "reviewReasons": list(review_reasons),
                        "routeEvidenceKey": "",
                        "evidence": {
                            **evidence,
                            "annotationIds": list(
                                dict.fromkeys(evidence["annotationIds"])
                            ),
                        },
                        "textHints": [],
                        "issues": list(review_reasons),
                        "handles": [],
                        "segments": [],
                    },
                )
                group["count"] += 1
                group["handles"].append(source.get("entityHandle"))
                if segment.get("boundaryInferred"):
                    issue = "PIPE_SEGMENT_BOUNDARY_INFERRED"
                    if issue not in group["issues"]:
                        group["issues"].append(issue)
                _accumulate_route_length(group, segment_length, scale)
                group["segments"].append(
                    {
                        "handle": source.get("entityHandle"),
                        "entityId": entity.get("id"),
                        "rootHandle": source.get("entityHandle"),
                        "definitionHandle": entity.get("definitionHandle"),
                        "instancePath": list(entity.get("instancePath") or []),
                        "sourceRegionKey": source_region_key,
                        "physicalSegmentId": physical_segment_id,
                        "t0": segment.get("t0"),
                        "t1": segment.get("t1"),
                        "lengthRaw": segment_length,
                        "spec": spec,
                        "role": role,
                        "annotation": str(segment.get("annotation") or ""),
                        "medium": segment_medium,
                        "diameterKind": diameter_kind,
                        "eligibility": eligibility,
                        "reviewReasons": list(review_reasons),
                        "evidence": evidence,
                        "boundaryInferred": bool(
                            segment.get("boundaryInferred")
                        ),
                        "boundaryEvidence": segment.get("boundaryEvidence"),
                    }
                )
                if length_issue and length_issue not in group["issues"]:
                    group["issues"].append(length_issue)
                if spec_entry.get("specSource") == "ambiguous":
                    issue = "PIPE_SPEC_AMBIGUOUS"
                    if issue not in group["issues"]:
                        group["issues"].append(issue)
                elif not spec:
                    issue = "PIPE_SPEC_UNRESOLVED"
                    if issue not in group["issues"]:
                        group["issues"].append(issue)
                for text in nearby_texts:
                    if text and text not in group["textHints"] and len(group["textHints"]) < 8:
                        group["textHints"].append(text)
        if length_raw is None:
            unmeasured_routes.append(
                {
                    "layer": layer,
                    "type": entity.get("type"),
                    "handle": source.get("entityHandle"),
                    "reason": length_issue or "GEOMETRY_UNSUPPORTED",
                }
            )

    route_points_by_entity = {
        str(entity.get("id")): _entity_points(entity)
        for entity in report.get("entities", [])
        if entity.get("type") in ROUTE_TYPES
    }

    def group_points(group: dict) -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []
        seen: set[tuple[float, float]] = set()
        for segment in group.get("segments") or []:
            for point in route_points_by_entity.get(
                str(segment.get("entityId")), []
            ):
                key = (round(point[0], 9), round(point[1], 9))
                if key not in seen:
                    seen.add(key)
                    points.append(point)
        return points

    root_groups = [
        group
        for group in route_groups.values()
        if str(group.get("sourceRegionKey") or "").endswith("|root")
    ]
    nested_groups = [
        group
        for group in route_groups.values()
        if "|insert:" in str(group.get("sourceRegionKey") or "")
    ]
    for first in root_groups:
        for second in nested_groups:
            if (
                first.get("medium") != second.get("medium")
                or first.get("spec") != second.get("spec")
                or not first.get("spec")
            ):
                continue
            if not _same_geometry_under_translation(
                group_points(first), group_points(second)
            ):
                continue
            for group in (first, second):
                if "DUPLICATE_SCOPE_UNCONFIRMED" not in group["reviewReasons"]:
                    group["reviewReasons"].append("DUPLICATE_SCOPE_UNCONFIRMED")
                if "DUPLICATE_SCOPE_UNCONFIRMED" not in group["issues"]:
                    group["issues"].append("DUPLICATE_SCOPE_UNCONFIRMED")
                group["eligibility"] = "review"
                group["autoInclude"] = False

    _apply_layer_spec_fallback(route_groups.values(), texts)
    for group in route_groups.values():
        if group.get("issues") or group.get("eligibility") != "eligible":
            group["eligibility"] = "review"
            group["autoInclude"] = False
        group["routeEvidenceKey"] = _route_evidence_key(
            source_hash=str(report.get("sourceHash") or ""),
            engine_version=INSPECTION_ENGINE_VERSION,
            source_region_key=str(group.get("sourceRegionKey") or ""),
            segments=list(group.get("segments") or []),
            medium=str(group.get("medium") or "unknown"),
            spec=str(group.get("spec") or ""),
            eligibility=str(group.get("eligibility") or "review"),
            review_reasons=list(group.get("reviewReasons") or []),
        )

    for group in equipment_groups.values():
        candidates: list[dict] = []
        seen_handles: set[str] = set()
        boxes = group.get("instanceBboxes") or [group.get("bbox")]
        for box in boxes:
            for item in text_index.nearby(
                group["view"],
                box,
                layer=group.get("layer", ""),
                expand_cells=10,
            ):
                handle = str(item.get("handle") or "")
                if handle and handle in seen_handles:
                    continue
                if handle:
                    seen_handles.add(handle)
                candidates.append(item)
        for item in candidates:
            text = item.get("text", "")
            if text and text not in group["textHints"] and len(group["textHints"]) < 16:
                group["textHints"].append(text)
    for group in equipment_groups.values():
        if not group.get("floorLabel"):
            group["floorLabel"] = _floor_label(
                group["view"], view_texts.get(group["view"], [])
            )
        visible_model = group.get("modelFromText") or ""
        xdata_model = group.get("modelFromXdata") or ""
        group["modelHint"] = (
            visible_model or xdata_model or group["blockName"]
        )
        if group.get("detectedBy") != "annotation":
            group["modelSource"] = (
                "visible_text"
                if visible_model
                else "xdata"
                if xdata_model
                else "block"
            )
        if (
            visible_model
            and xdata_model
            and _text(visible_model).upper() != _text(xdata_model).upper()
        ):
            group["modelConflict"] = True
            group["issues"].append(
                f"图面标注“{visible_model}”与图块属性“{xdata_model}”不一致；"
                "当前按图面标注，请人工确认。"
            )
        kind = str(group.get("equipmentKind") or "")
        haystack = (
            f"{group.get('layer', '')}{group.get('modelHint', '')}"
            f"{group.get('category', '')}"
        )
        if kind in WATER_EQUIPMENT_KINDS or any(
            marker in haystack for marker in WATER_SYSTEM_HINTS
        ):
            group["systemHint"] = "water"
        elif kind in {"室内机", "室外机"} or _equipment_role(
            str(group.get("modelHint") or "")
        ):
            group["systemHint"] = "refrigerant"
        else:
            # 没有证据时保留未知，不能默认按冷媒设备套价。
            group["systemHint"] = "unknown"
        group["displayName"] = group["modelHint"]
        group["displaySpec"] = group["category"]
    # 同一型号出现在多个视图/楼层时，只提示可能的重复表达，不自动合并。
    cross_view_count = _mark_cross_view_duplicates(
        list(equipment_groups.values())
    )
    for group in route_groups.values():
        if not group.get("floorLabel"):
            group["floorLabel"] = _floor_label(
                group["view"], view_texts.get(group["view"], [])
            )
        group["displayName"] = (
            f"{group['layer']} {group.get('spec')}".strip()
            if group.get("spec")
            else group["layer"]
        )
        group["displaySpec"] = (
            f"{group.get('category')} {group.get('spec')}".strip()
            if group.get("spec")
            else group.get("category", "")
        )

    issues: list[dict] = []
    if excluded_routes:
        issues.append(
            {
                "code": "WEAK_ROUTE_CANDIDATES_EXCLUDED",
                "message": (
                    f"有 {len(excluded_routes)} 条弱证据图元因闭合/紧凑/贴设备轮廓"
                    "默认不计入；逐条清单见 report.excludedRouteCandidates，"
                    "确认是管线时可人工补录。"
                ),
                "severity": "review",
                "sources": [
                    {"entityHandle": item.get("handle")}
                    for item in excluded_routes[:5]
                ],
            }
        )
    if cross_view_count:
        issues.append(
            {
                "code": "CROSS_VIEW_DUPLICATE_CANDIDATE",
                "message": (
                    f"有 {cross_view_count} 个设备候选的型号同时出现在多个视图/楼层，"
                    "可能是平面图与系统图的重复表达；已分别列出，未自动合并，"
                    "请在草稿中选择本次范围。"
                ),
                "severity": "review",
                "sources": [],
            }
        )
    for item in report.get("issues", []):
        issues.append(
            {
                "code": item.get("code", "CAD_ISSUE"),
                "message": item.get("message", ""),
                "severity": item.get("severity", "review"),
                "sources": item.get("sources", []),
            }
        )
    if scale is None:
        issues.append(
            {
                "code": "SCALE_UNCONFIRMED",
                "message": "图纸没有可确认的测量单位，管线只能显示图面长度，不能形成米制报价。",
                "severity": "blocks_pricing",
                "sources": [],
            }
        )
    for group in equipment_groups.values():
        if group.get("detectedBy") == "annotation":
            issues.append(
                {
                    "code": "EQUIPMENT_MODEL_FROM_ANNOTATION",
                    "message": (
                        f"{group.get('floorLabel') or group.get('view')} "
                        f"图块“{group.get('blockName')}”本身没有设备关键字，"
                        f"因邻近型号“{group.get('modelFromText')}”列为候选；请确认它确实是设备。"
                    ),
                    "severity": "review",
                    "sources": [
                        {"entityHandle": handle}
                        for handle in group.get("handles", [])[:3]
                    ],
                }
            )
        if group.get("modelConflict"):
            issues.append(
                {
                    "code": "MODEL_CONFLICT",
                    "message": (
                        f"{group.get('floorLabel') or group.get('view')} "
                        f"{group.get('layer')}/{group.get('blockName')}："
                        f"图面标注“{group.get('modelFromText')}”与图块属性"
                        f"“{group.get('modelFromXdata')}”不一致，当前按图面标注，请确认。"
                    ),
                    "severity": "review",
                    "sources": [
                        {"entityHandle": handle}
                        for handle in group.get("handles", [])[:3]
                    ],
                }
            )
    for group in route_groups.values():
        if "PIPE_SPEC_AMBIGUOUS" in group.get("issues", []):
            issues.append(
                {
                    "code": "PIPE_SPEC_AMBIGUOUS",
                    "message": (
                        f"{group.get('floorLabel') or group.get('view')} "
                        f"{group.get('layer')} 附近有多个管径标注，"
                        "无法确定该管段规格；请人工确认。"
                    ),
                    "severity": "review",
                    "sources": [
                        {"entityHandle": handle}
                        for handle in group.get("handles", [])[:3]
                    ],
                }
            )
        elif "PIPE_SPEC_UNRESOLVED" in group.get("issues", []):
            issues.append(
                {
                    "code": "PIPE_SPEC_UNRESOLVED",
                    "message": (
                        f"{group.get('floorLabel') or group.get('view')} "
                        f"{group.get('layer')} 没有读到管径标注；"
                        "规格未确定，不能自动套用铜管价格。"
                    ),
                    "severity": "review",
                    "sources": [
                        {"entityHandle": handle}
                        for handle in group.get("handles", [])[:3]
                    ],
                }
            )
        if "PIPE_SEGMENT_BOUNDARY_INFERRED" in group.get("issues", []):
            issues.append(
                {
                    "code": "PIPE_SEGMENT_BOUNDARY_INFERRED",
                    "message": (
                        f"{group.get('floorLabel') or group.get('view')} "
                        f"{group.get('layer')} 的管段跨越两种管径标注，"
                        + (
                            "包含按原生分歧符号尖端投影的分段提案；计量口径待确认，请在原图核对。"
                            if any(segment.get("boundaryEvidence") for segment in group.get("segments", []))
                            else "已按图面几何位置推断分段；分界位置待确认，请在原图核对。"
                        )
                    ),
                    "severity": "review",
                    "sources": [
                        {"entityHandle": handle}
                        for handle in group.get("handles", [])[:3]
                    ],
                }
            )
        if "PIPE_LAYER_UNCERTAIN" in group.get("issues", []):
            issues.append(
                {
                    "code": "PIPE_LAYER_UNCERTAIN",
                    "message": (
                        f"{group.get('floorLabel') or group.get('view')} "
                        f"图层“{group.get('layer')}”不是明确的管线图层，"
                        "但图形位于管径标注附近，已列为候选；请确认它确实是管线。"
                    ),
                    "severity": "review",
                    "sources": [
                        {"entityHandle": handle}
                        for handle in group.get("handles", [])[:3]
                    ],
                }
            )
    if not equipment_groups:
        issues.append(
            {
                "code": "NO_EQUIPMENT_CANDIDATES",
                "message": "没有识别到候选设备图块；请确认图块名称/图层，或由人工补录。",
                "severity": "blocks_pricing",
                "sources": [],
            }
        )
    if not route_groups:
        issues.append(
            {
                "code": "NO_PIPE_CANDIDATES",
                "message": "没有识别到候选管线；如果图上未画管线，请在报价中列明未计入。",
                "severity": "review",
                "sources": [],
            }
        )
    for item in unmeasured_routes[:20]:
        issues.append(
            {
                "code": "ROUTE_LENGTH_UNMEASURED",
                "message": f"{item['layer']} 的 {item['type']} 无法可靠量取长度，未计入。",
                "severity": "review",
                "sources": [{"entityHandle": item["handle"]}],
            }
        )
    issues.extend(floor_issues)

    type_counts = Counter(
        entity.get("type", "") for entity in report.get("entities", [])
    )
    unsupported_layers = Counter(
        _text(entity.get("layer"))
        for entity in report.get("entities", [])
        if not entity.get("supported", True)
    )
    layer_counts = Counter(
        _text(entity.get("layer")) for entity in report.get("entities", [])
    )
    view_names = sorted(
        {
            *[str(view.get("view")) for view in report.get("views", [])],
            *{group["view"] for group in equipment_groups.values()},
            *{group["view"] for group in route_groups.values()},
        }
    )
    view_summary = []
    for view in view_names:
        view_summary.append(
            {
                "view": view,
                "floorLabel": _floor_label(view, view_texts.get(view, [])),
                "equipmentGroups": sum(
                    1 for group in equipment_groups.values() if group["view"] == view
                ),
                "routeGroups": sum(
                    1 for group in route_groups.values() if group["view"] == view
                ),
            }
        )
    floor_labels = sorted(
        {
            str(group.get("floorLabel") or "")
            for group in equipment_groups.values()
        }
        | {
            str(group.get("floorLabel") or "")
            for group in route_groups.values()
        }
        | {
            str(region.get("label") or "")
            for region in floor_regions
        }
    )
    floor_summary = []
    for label in floor_labels if floor_regions else []:
        if not label:
            continue
        region = next(
            (
                item
                for item in floor_regions
                if item.get("label") == label
            ),
            {},
        )
        floor_summary.append(
            {
                "view": next(
                    (
                        group["view"]
                        for group in [
                            *equipment_groups.values(),
                            *route_groups.values(),
                        ]
                        if group.get("floorLabel") == label
                    ),
                    "model",
                ),
                "floorLabel": label,
                "equipmentGroups": sum(
                    1
                    for group in equipment_groups.values()
                    if group.get("floorLabel") == label
                ),
                "routeGroups": sum(
                    1
                    for group in route_groups.values()
                    if group.get("floorLabel") == label
                ),
                "bbox": region.get("bbox"),
                "entityCount": region.get("entityCount", 0),
                "confidence": region.get("confidence", ""),
            }
        )
    return {
        "schemaVersion": "generic-drawing-inspection-2",
        "engineVersion": INSPECTION_ENGINE_VERSION,
        "inspectionScope": (
            report.get("diagnostics", {}).get("scope") or {}
        ),
        "fileId": file_id,
        "sourceHash": report.get("sourceHash"),
        "units": units,
        "views": report.get("views", []),
        "floorCandidates": report.get("floorCandidates", []),
        "counts": {
            "entities": len(report.get("entities", [])),
            "byType": dict(type_counts.most_common(30)),
            "equipment": sum(g["quantity"] for g in equipment_groups.values()),
            "equipmentGroups": len(equipment_groups),
            "routes": sum(g["count"] for g in route_groups.values()),
            "routeGroups": len(route_groups),
            "views": len(view_names),
            "floors": len(
                [
                    item
                    for item in floor_summary
                    if item["floorLabel"] not in {"未识别", "model"}
                ]
            ),
        },
        "viewSummary": view_summary,
        "floorSummary": floor_summary,
        "floorRegions": floor_regions,
        "equipmentGroups": sorted(
            equipment_groups.values(), key=lambda item: (-item["quantity"], item["key"])
        ),
        "routeGroups": sorted(
            route_groups.values(),
            key=lambda item: (
                item["lengthRaw"] is None,
                -(item["lengthRaw"] or 0.0),
                item["key"],
            ),
        ),
        "topLayers": [
            {"layer": layer, "count": count}
            for layer, count in layer_counts.most_common(30)
        ],
        "excludedRouteCandidates": excluded_routes,
        "annotationRadiusAudit": annotation_radius_audit,
        "unsupportedByLayer": [
            {"layer": layer, "count": count}
            for layer, count in unsupported_layers.most_common(30)
        ],
        "issues": issues,
        "diagnostics": report.get("diagnostics", {}),
    }


__all__ = ["INSPECTION_ENGINE_VERSION", "inspect_drawing", "CadReadError"]
