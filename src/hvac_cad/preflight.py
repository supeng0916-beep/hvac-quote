"""大图轻量预检：在完整展开前统计布局、根对象与图块展开规模。

只遍历图块定义，不递归展开实例几何；用于选择检查范围、估算预算和
识别可以按上下文块折叠的大图块（不丢失暖通证据的判断见 normalize）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

HVAC_TEXT_MARKERS = (
    "MDV",
    "KFR",
    "FP-",
    "MMD",
    "FXD",
    "RMX",
    "多联机",
    "室内机",
    "室外机",
    "风机盘管",
    "新风机",
    "冷媒",
    "铜管",
    "液管",
    "气管",
    "水管",
    "冷冻水",
    "冷却水",
    "冷凝水",
    "排水",
    "保温",
    "风管",
    "水泵",
    "阀门",
    "冷却塔",
    "DN",
    "Φ",
    "φ",
)
# 只把明确的系统/设备图层作为强证据；“空调”“设备”这类泛化房间标注
# （例如建筑插件的 RC-空调、新风、排气）不能单独触发整块展开。
STRONG_LAYER_MARKERS = (
    "冷媒",
    "铜管",
    "气管",
    "液管",
    "排水",
    "冷凝",
    "水管",
    "保温",
    "PIPE",
    "REF",
    "DRAIN",
    "WATER",
    "CMDLJ",
    "CHW",
    "CWS",
    "CDS",
    "冷冻水",
    "冷却水",
    "补水",
    "风管",
    "风机",
    "INAC",
    "OUTAC",
    "VRV",
    "FQCA",
)
TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}
# Exact native layers observed in real drawings. Only complete layer names are
# matched; ACS/LN/QYG must not become broad substrings.
PROTECTED_EXACT_LAYERS = frozenset({"ACS_DLJ_QYG", "ACS_KT_LN"})
# 达到该子图元数量的块，在无暖通证据时按上下文块折叠为一个记录。
OPAQUE_MIN_CHILDREN = 20


@dataclass
class BlockStats:
    count: int = 0
    hvac: bool = False
    layers: set[str] = field(default_factory=set)
    texts: list[str] = field(default_factory=list)
    strongLayers: dict[str, int] = field(default_factory=dict)
    textHits: list[str] = field(default_factory=list)


def _layer_is_strong(layer: str) -> bool:
    if str(layer or "").strip().casefold() in {
        item.casefold() for item in PROTECTED_EXACT_LAYERS
    }:
        return True
    upper = (layer or "").upper()
    if any(marker.upper() in upper for marker in STRONG_LAYER_MARKERS):
        return True
    # 独立的 AC 图层（如 Daikin-AC），不匹配 RC-AC 之类复合词。
    return bool(re.search(r"(^|[^A-Z])AC([^A-Z]|$)", upper))


def _text_is_hvac(text: str) -> bool:
    upper = (text or "").upper()
    return any(marker.upper() in upper for marker in HVAC_TEXT_MARKERS)


def analyze_blocks(document, *, limit: int = 5_000_000) -> dict[str, BlockStats]:
    """按图块定义统计展开量与暖通证据；每个定义只算一次。"""
    cache: dict[str, BlockStats] = {}

    def visit(name: str, stack: frozenset[str]) -> BlockStats:
        if name in cache:
            return cache[name]
        if name in stack:
            return BlockStats(count=0)
        try:
            block = document.blocks.get(name)
        except Exception:  # noqa: BLE001 - 缺定义按空块处理，读取阶段另行报错
            block = None
        if block is None:
            stats = BlockStats()
            cache[name] = stats
            return stats
        stats = BlockStats()
        stack = stack | {name}
        for child in block:
            try:
                kind = child.dxftype()
                layer = str(child.dxf.get("layer", "0") or "0")
            except Exception:  # noqa: BLE001
                continue
            stats.layers.add(layer)
            if _layer_is_strong(layer):
                stats.strongLayers[layer] = stats.strongLayers.get(layer, 0) + 1
                stats.hvac = True
            if kind in TEXT_TYPES:
                try:
                    text = str(
                        child.dxf.get("text")
                        or child.dxf.get("characters")
                        or child.dxf.get("tag")
                        or ""
                    )
                except Exception:  # noqa: BLE001
                    text = ""
                if text and len(stats.texts) < 20:
                    stats.texts.append(text[:80])
                if _text_is_hvac(text):
                    stats.hvac = True
                    if len(stats.textHits) < 10:
                        stats.textHits.append(text[:80])
            if kind == "INSERT":
                try:
                    child_name = str(child.dxf.name)
                except Exception:  # noqa: BLE001
                    continue
                sub = visit(child_name, stack)
                stats.count = min(limit, stats.count + sub.count)
                stats.hvac = stats.hvac or sub.hvac
                stats.layers |= sub.layers
                for key, value in sub.strongLayers.items():
                    stats.strongLayers[key] = (
                        stats.strongLayers.get(key, 0) + value
                    )
                for item in sub.textHits:
                    if item not in stats.textHits and len(stats.textHits) < 10:
                        stats.textHits.append(item)
            elif kind != "ATTDEF":
                stats.count = min(limit, stats.count + 1)
        cache[name] = stats
        return stats

    for block in document.blocks:
        try:
            name = str(block.name)
        except Exception:  # noqa: BLE001
            continue
        if name.startswith("*"):
            continue
        visit(name, frozenset())
    return cache


def preflight_document(document, *, max_entities: int = 200_000) -> dict:
    """返回每个布局的根对象/估算展开量/大图块，不展开实例几何。"""
    analysis = analyze_blocks(document)
    views: list[dict] = []
    for space in document.layouts:
        try:
            if not len(space):
                continue
        except Exception:  # noqa: BLE001
            continue
        view = "model" if space.name == "Model" else str(space.name)
        root_count = 0
        estimate = 0
        effective = 0
        block_instances: dict[str, int] = {}
        block_estimate: dict[str, int] = {}
        layer_counts: dict[str, int] = {}
        for entity in space:
            root_count += 1
            try:
                kind = entity.dxftype()
                layer = str(entity.dxf.get("layer", "0") or "0")
            except Exception:  # noqa: BLE001
                continue
            layer_counts[layer] = layer_counts.get(layer, 0) + 1
            if kind == "INSERT":
                try:
                    name = str(entity.dxf.name)
                except Exception:  # noqa: BLE001
                    name = "?"
                stats = analysis.get(name, BlockStats())
                block_instances[name] = block_instances.get(name, 0) + 1
                block_estimate[name] = stats.count
                estimate += stats.count
                opaque = (
                    stats.count >= OPAQUE_MIN_CHILDREN and not stats.hvac
                )
                effective += 1 if opaque else stats.count
            else:
                estimate += 1
                effective += 1
        opaque_blocks = sorted(
            (
                {
                    "blockName": name,
                    "instances": count,
                    "childCount": block_estimate.get(name, 0),
                }
                for name, count in block_instances.items()
                if block_estimate.get(name, 0) >= OPAQUE_MIN_CHILDREN
                and not analysis.get(name, BlockStats()).hvac
            ),
            key=lambda item: -(item["instances"] * item["childCount"]),
        )[:20]
        views.append(
            {
                "view": view,
                "rootCount": root_count,
                "estimatedEntities": estimate,
                "effectiveEntities": effective,
                "withinBudget": effective <= max_entities,
                "opaqueBlocks": opaque_blocks,
                "topBlocks": sorted(
                    (
                        {
                            "blockName": name,
                            "instances": count,
                            "childCount": block_estimate.get(name, 0),
                            "hvacEvidence": analysis.get(
                                name, BlockStats()
                            ).hvac,
                        }
                        for name, count in block_instances.items()
                    ),
                    key=lambda item: -(item["instances"] * item["childCount"]),
                )[:20],
                "topLayers": sorted(
                    (
                        {"layer": layer, "count": count}
                        for layer, count in layer_counts.items()
                    ),
                    key=lambda item: -item["count"],
                )[:20],
            }
        )
    return {
        "schemaVersion": "cad-preflight-1",
        "dxfVersion": getattr(document, "dxfversion", ""),
        "maxEntities": max_entities,
        "opaqueThreshold": OPAQUE_MIN_CHILDREN,
        "views": views,
        "totalEstimated": sum(item["estimatedEntities"] for item in views),
        "totalEffective": sum(item["effectiveEntities"] for item in views),
        "blockDefinitionCount": len(analysis),
    }


__all__ = [
    "BlockStats",
    "HVAC_LAYER_MARKERS",
    "HVAC_TEXT_MARKERS",
    "OPAQUE_MIN_CHILDREN",
    "PROTECTED_EXACT_LAYERS",
    "analyze_blocks",
    "preflight_document",
]
