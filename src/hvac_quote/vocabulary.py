"""图纸检查词表。

内置词表放在 ``data/vocabulary.json``；不同客户/图层的命名习惯可通过
``HVAC_VOCABULARY`` 指向另一份 JSON 覆盖，不必改核心代码。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

VOCABULARY_PATH = Path(__file__).with_name("data") / "vocabulary.json"

DEFAULT_VOCABULARY: dict[str, tuple[str, ...]] = {
    "equipment_layer_markers": (
        "AC", "HVAC", "空调", "设备", "INAC", "OUTAC", "VRV", "FQCA",
    ),
    "equipment_text_markers": (
        "MDV", "KFR", "FP-", "室内机", "室外机", "风机盘管", "多联机",
        "新风机", "水泵", "阀门", "冷却塔", "水处理", "膨胀水箱",
        "分水器", "集水器", "冷水机组", "天花", "嵌入式",
    ),
    "pipe_layer_markers": (
        "冷媒", "铜管", "气管", "液管", "排水", "冷凝", "水管", "保温",
        "PIPE", "REF", "DRAIN", "WATER", "CMDLJ", "BZ", "QG", "YLG",
        "WL", "JL", "CHW", "CWS", "CDS", "冷冻水", "冷却水", "补水",
    ),
    "water_system_hints": (
        "水泵", "阀门", "冷却塔", "水处理", "冷冻水", "冷却水", "补水",
        "水管", "水系统",
    ),
    "water_route_markers": (
        "水管", "供水", "回水", "空冷", "冷冻水", "冷却水", "补水",
        "给水", "water", "CHW", "CWS", "CDS",
    ),
    "refrigerant_layer_aliases": ("ACS_DLJ_QYG",),
    "condensate_layer_aliases": ("ACS_KT_LN",),
    "floor_title_markers": ("平面图", "层平面", "首层", "底层", "屋顶层"),
    "frame_layer_markers": ("图框", "边框", "会签", "TITLE", "FRAME", "BORDER"),
    "pipe_text_markers": (
        "铜管", "冷媒", "液管", "气管", "排水", "冷凝", "水管", "冷冻水",
        "冷却水", "补水", "保温", "DN", "Φ", "φ",
    ),
    "model_text_markers": ("MDV", "KFR", "FP-", "室内机", "室外机", "多联机"),
    "model_code_markers": ("MDV", "KFR", "FP-", "RMX", "MMD", "FXD"),
}


def load_vocabulary(path: Path | None = None) -> dict[str, tuple[str, ...]]:
    """加载内置词表；显式配置的文件可覆盖其中任意字段。"""
    configured = os.environ.get("HVAC_VOCABULARY", "").strip()
    target = Path(configured) if configured else (path or VOCABULARY_PATH)
    if not target.is_file():
        if configured:
            raise RuntimeError(f"HVAC_VOCABULARY 指向的文件不存在：{target}")
        return dict(DEFAULT_VOCABULARY)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取图纸词表：{target}：{exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"图纸词表必须是 JSON 对象：{target}")
    merged = dict(DEFAULT_VOCABULARY)
    for key, value in payload.items():
        if key not in merged:
            raise RuntimeError(f"图纸词表包含未知字段：{key}")
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise RuntimeError(f"图纸词表字段 {key} 必须是字符串数组")
        merged[key] = tuple(value)
    return merged


VOCABULARY = load_vocabulary()
