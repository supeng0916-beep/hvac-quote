"""把图纸检查候选 + 模板价格书变成可确认的报价草稿，并生成报价文件。"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import Evidence, OmittedItem, QuantityItem, TakeoffResult
from .money import format_quantity, upper_amount
from .presentation import build_groups
from .pdf import QuotePdfData, QuotePdfRow, QuotePdfSection, render_quote_pdf
from .pipeline import RunResult
from .pricing import coefficient_review_exception, price_items, round_money
from .template import export_workbook, recalc_workbook

TEXT_NORMALIZE_RE = re.compile(r"[^0-9A-Z\u4e00-\u9fff]+")
SET_NAME_RE = re.compile(r"(?:1|一)\s*拖\s*(\d+)")
SPEC_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")
WATER_ROUTE_CATEGORIES = {"水管", "冷冻水", "冷却水", "补水"}


def _normalize(value: str) -> str:
    return TEXT_NORMALIZE_RE.sub("", (value or "").upper())


def _model_key(value: str) -> str:
    # 图纸常见 MDV-D125 与模板 MDV-125 的后缀差异；仅用于成套归组，
    # 不改变原始型号和人工确认记录。
    return _normalize(value).replace("MDVD", "MDV")


def _spec_number(value: str | None) -> float | None:
    match = SPEC_NUMBER_RE.search(str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _row_is_insulation(row: dict) -> bool:
    haystack = f"{row.get('name', '')}{row.get('spec', '')}"
    return "保温" in haystack or "橡塑" in haystack


def _insulation_match(group: dict, rows: list[dict]) -> dict | None:
    if "保温" in f"{group.get('layer', '')}{group.get('category', '')}":
        return None
    category = str(group.get("category") or "")
    spec = _spec_number(group.get("spec"))
    if spec is None or not category:
        return None
    candidates = [
        row
        for row in rows
        if row.get("priceState") == "priced" and _row_is_insulation(row)
    ]
    if category in WATER_ROUTE_CATEGORIES:
        candidates = [row for row in candidates if row.get("system") == "water"]
        if not candidates:
            return None
    elif category == "排水":
        candidates = [
            row
            for row in candidates
            if row.get("system") in {"general", "water"}
            and (
                "冷凝水" in f"{row.get('name', '')}{row.get('spec', '')}"
                or "排水" in f"{row.get('name', '')}{row.get('spec', '')}"
                or "DN" in str(row.get("spec") or "").upper()
            )
        ]
    else:
        candidates = [
            row for row in candidates if row.get("system") != "water"
        ]
    best = None
    best_score = None
    for row in candidates:
        row_spec = _spec_number(row.get("spec"))
        if row_spec is None:
            continue
        diff = abs(row_spec - spec)
        if diff > 2.0:
            continue
        text = f"{row.get('name', '')}{row.get('spec', '')}"
        score = diff
        if category == "排水" and "冷凝水" in text:
            score -= 0.5
        if category == "排水" and "DN" in str(row.get("spec") or "").upper():
            score -= 0.2
        if category not in WATER_ROUTE_CATEGORIES and "保温" in text:
            score -= 0.1
        if best_score is None or score < best_score:
            best, best_score = row, score
    if best is None:
        return None
    return {
        "priceRow": int(best["rowNumber"]),
        "priceLabel": _row_label(best),
        "name": best.get("name", ""),
        "spec": best.get("spec", ""),
        "unit": best.get("unit") or "米",
        "rule": "equal_length",
        "confidence": "linked",
        "specDiff": round(best_score or 0.0, 2),
    }



def link_route_material(group: dict, rows: list[dict]) -> dict:
    """给任意入口创建的管线组补上保温依赖，规则与自动候选一致。

    所有入口（候选、单行补录、批量补充）都走这里：源管材只负责提供数量，
    对应保温由模板行匹配派生；匹配不到时明确标记缺项，而不是静默漏掉。
    """
    if "保温" in f"{group.get('layer', '')}{group.get('category', '')}":
        group.pop("linkedMaterial", None)
        group.pop("linkedMaterialMissing", None)
        return group
    if not str(group.get("spec") or "").strip():
        label = str(group.get("label") or group.get("manualLabel") or "")
        match = SPEC_NUMBER_RE.search(label)
        if match:
            group["spec"] = match.group(1)
    linked = _insulation_match(group, rows)
    if linked is not None:
        group["linkedMaterial"] = linked
        group.pop("linkedMaterialMissing", None)
        return group
    group.pop("linkedMaterial", None)
    group["linkedMaterialMissing"] = True
    message = (
        "这条管线没有匹配到对应的保温模板行；如果保温另计，"
        "请在“补录未识别内容”或批量补充表里单独添加保温项。"
    )
    issues = list(group.get("issues") or [])
    if message not in issues:
        issues.append(message)
    group["issues"] = issues
    return group


def _row_label(row: dict) -> str:
    return " ".join(
        part for part in (row.get("name"), row.get("spec")) if part
    ).strip()


def _equipment_match(
    model: str, rows: list[dict], system_hint: str = "refrigerant"
) -> tuple[dict | None, str]:
    candidates = [
        row
        for row in rows
        if row.get("category") == "equipment" and row.get("priceState") == "priced"
    ]
    if system_hint == "water":
        candidates = [row for row in candidates if row.get("system") == "water"]
    elif system_hint == "unknown":
        # 没有证据时不自动套价，避免把水系统设备按冷媒价成交。
        return None, "none"
    else:
        candidates = [row for row in candidates if row.get("system") != "water"]
    target = _model_key(model)
    if not target:
        return None, "none"
    for row in candidates:
        for alias in row.get("aliases") or []:
            alias_key = _model_key(str(alias))
            if alias_key and (alias_key in target or target in alias_key):
                return row, "exact"
    best = None
    best_score = 0.0
    for row in candidates:
        haystack = _normalize(f"{row.get('name', '')}{row.get('spec', '')}")
        if not haystack:
            continue
        if target == haystack or target in haystack or haystack in target:
            return row, "exact"
        score = difflib.SequenceMatcher(None, target, haystack).ratio()
        if score > best_score:
            best, best_score = row, score
    if best is not None and best_score >= 0.55:
        return best, "fuzzy"
    return None, "none"


COPPER_MARKERS = ("铜管", "冷媒")
DRAIN_MARKERS = ("PVC", "UPVC", "排水", "冷凝水", "冷凝")
WATER_MARKERS = ("水管", "无缝钢管", "镀锌钢管", "冷冻水", "冷却水", "补水")


def _row_material(row: dict) -> str:
    text = f"{row.get('name', '')}{row.get('spec', '')}".upper()
    if any(marker.upper() in text for marker in DRAIN_MARKERS):
        return "drain"
    if any(marker.upper() in text for marker in COPPER_MARKERS):
        return "copper"
    if any(marker.upper() in text for marker in WATER_MARKERS):
        return "water"
    return "other"


def _group_material(group: dict) -> str:
    medium = str(group.get("medium") or "")
    if medium in {"drain", "condensate"}:
        return "drain"
    if medium == "refrigerant":
        return "copper"
    if medium in {"supply", "return", "cooling", "makeup"}:
        return "water"
    category = str(group.get("category") or "")
    if category == "排水":
        return "drain"
    if category in {"冷媒管", "液管", "气管"}:
        return "copper"
    if category in {"水管", "冷冻水", "冷却水", "补水"}:
        return "water"
    return "unknown"


def _spec_kind(value, material: str) -> str:
    text = str(value or "").upper()
    if "DN" in text:
        return "dn"
    if "Φ" in text or "φ" in text:
        return "od"
    if material in {"drain", "water"}:
        return "dn"
    if material == "copper":
        return "od"
    return "unknown"


def _spec_value(value) -> float | None:
    match = SPEC_NUMBER_RE.search(str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def route_row_error(group: dict, row: dict | None) -> str | None:
    """检查管线组与模板行是否同一系统/材质/计量单位/规格类型。"""
    if row is None:
        return "模板行不存在"
    if str(row.get("unit") or "") not in {"米", "m", "M"}:
        return f"模板行 {row.get('rowNumber')} 的计量单位是“{row.get('unit')}”，不能按米计价"
    group_material = _group_material(group)
    row_material = _row_material(row)
    labels = {
        "copper": "冷媒铜管",
        "drain": "排水管",
        "water": "水管",
        "other": "其他材料",
        "unknown": "系统未知",
    }
    if group_material == "unknown":
        return "管线系统/介质未知，不能自动套用模板行"
    if group_material != row_material:
        return (
            f"{labels.get(group_material, group_material)}不能套用"
            f"{labels.get(row_material, row_material)}价格行"
        )
    group_spec = str(group.get("spec") or "")
    row_spec = str(row.get("spec") or "")
    group_kind = _spec_kind(group_spec, group_material)
    row_kind = _spec_kind(row_spec, row_material)
    if group_kind != row_kind:
        return (
            f"规格类型不兼容：图纸“{group_spec}”（{group_kind}）"
            f"不能套模板“{row_spec}”（{row_kind}）"
        )
    group_value = _spec_value(group_spec)
    row_value = _spec_value(row_spec)
    if group_value is None or row_value is None:
        return "缺少可比较的规格数值"
    tolerance = 0.01 if group_kind == "dn" else 0.1
    if abs(group_value - row_value) > tolerance:
        return (
            f"规格不匹配：图纸 {group_spec} 与模板 {row_spec} 相差超过 "
            f"{tolerance:g}"
        )
    return None


def _route_match(group: dict, rows: list[dict]) -> tuple[dict | None, str]:
    group_material = _group_material(group)
    if group_material == "unknown":
        return None, "none"
    candidates = [
        row
        for row in rows
        if row.get("category") == "pipe"
        and row.get("priceState") == "priced"
        and not _row_is_insulation(row)
        and str(row.get("unit") or "") in {"米", "m", "M"}
        and _row_material(row) == group_material
    ]
    spec = str(group.get("spec") or "")
    if not spec:
        return None, "none"
    group_kind = _spec_kind(spec, group_material)
    group_value = _spec_value(spec)
    if group_value is None or group_kind == "unknown":
        return None, "none"
    tolerance = 0.01 if group_kind == "dn" else 0.1
    matched = []
    for row in candidates:
        row_spec = str(row.get("spec") or "")
        if _spec_kind(row_spec, group_material) != group_kind:
            continue
        row_value = _spec_value(row_spec)
        if row_value is None:
            continue
        if abs(row_value - group_value) <= tolerance:
            matched.append(row)
    if len(matched) == 1:
        confidence = (
            "inferred"
            if group.get("specSource") == "layer_dimension"
            else "spec"
        )
        return matched[0], confidence
    if len(matched) > 1:
        return None, "ambiguous"
    # 同类同规格没有价格时明确缺价，禁止跨材料/跨规格回退。
    return None, "none"


def _model_tokens(spec: str) -> list[str]:
    tokens: list[str] = []
    for line in re.split(r"[\n;；]+", spec or ""):
        for part in re.split(r"[+＋]", line):
            value = part.strip()
            if value:
                tokens.append(value)
    return tokens


def _component_role(group: dict, tokens: list[str]) -> str:
    stored = str(group.get("role") or "")
    if stored in {"indoor", "outdoor"}:
        return stored
    model = str(group.get("model") or group.get("label") or "")
    model_key = _model_key(model)
    haystack = f"{model}{group.get('layer', '')}{group.get('blockName', '')}".upper()
    for index, token in enumerate(tokens):
        token_key = _model_key(token)
        if token_key and (token_key in model_key or model_key in token_key):
            if index == 0 and len(tokens) > 1:
                return "outdoor"
            if index > 0:
                return "indoor"
    if any(
        marker in haystack
        for marker in ("室外", "OUT", "OUTDOOR", "RMX")
    ) or "W/" in model:
        return "outdoor"
    if any(marker in haystack for marker in ("室内", "INAC", "INDOOR")) or "Q4" in model:
        return "indoor"
    return "unknown"


def _set_row_meta(row: dict | None) -> tuple[int, list[str]] | None:
    """套价行返回（每套内机数, 型号 tokens）；不是“1拖N”套价行返回 None。"""
    if not row or row.get("unit") != "套":
        return None
    match = SET_NAME_RE.search(str(row.get("name") or ""))
    if not match:
        return None
    indoor_per_set = int(match.group(1))
    if indoor_per_set <= 0:
        return None
    return indoor_per_set, _model_tokens(str(row.get("spec") or ""))


def _is_packaged_set_row(row: dict | None) -> bool:
    """套价行但没有“1拖N”说明：按整机包装套（如挂机/柜机）处理。"""
    return bool(row and row.get("unit") == "套" and _set_row_meta(row) is None)


def _same_model_family(first: str, second: str) -> bool:
    left = _model_key(first)
    right = _model_key(second)
    if not left or not right:
        return False
    length = 0
    for one, two in zip(left, right):
        if one != two:
            break
        length += 1
    return length >= 5


def _set_totals(groups: list[dict], tokens: list[str]) -> tuple[int, int]:
    indoor = 0
    outdoor = 0
    for group in groups:
        quantity = int(float(group.get("quantity") or 0))
        role = _component_role(group, tokens)
        if role == "indoor":
            indoor += quantity
        elif role == "outdoor":
            outdoor += quantity
    return indoor, outdoor


def _set_issue_message(row_number: int, indoor: int, outdoor: int, per_set: int) -> str:
    return (
        f"模板第 {row_number} 行按“1拖{per_set}”整套计价："
        f"当前识别到室内机 {indoor} 台、室外机 {outdoor} 台，"
        "与成套数量不一致；请核对数量或改选单机价格行。"
    )


def reconcile_equipment_sets(
    data: dict, rows_by_number: dict[int, dict]
) -> dict:
    """按当前数量重算成套关系，保存和生成前都应调用。

    状态完全由当前数据推导，不沿用历史布尔值；重复调用结果一致。
    """
    equipment = [dict(group) for group in data.get("equipment") or []]
    by_key = {str(group.get("key")): group for group in equipment}

    def row_number_of(group: dict) -> int:
        try:
            return int(group.get("priceRow") or 0)
        except (TypeError, ValueError):
            return 0

    # 1) 已存在的成套组：按当前成员数量复核；价格行改成单机行则解散。
    for set_group in [group for group in equipment if group.get("isSet")]:
        member_keys = [
            str(component.get("key"))
            for component in set_group.get("setComponents") or []
        ]
        members = [by_key[key] for key in member_keys if key in by_key]
        row_number = row_number_of(set_group)
        row = rows_by_number.get(row_number)
        meta = _set_row_meta(row)
        packaged = meta is None and _is_packaged_set_row(row)
        if (meta is None and not packaged) or len(members) < 2:
            for member in members:
                member["isComponent"] = False
                member.pop("componentOfSet", None)
                member["include"] = True
            equipment.remove(set_group)
            by_key.pop(str(set_group.get("key")), None)
            continue
        if packaged:
            tokens = _model_tokens(str(row.get("spec") or ""))
            indoor, outdoor = _set_totals(members, tokens)
            valid = indoor > 0 and indoor == outdoor
            per_set = 1
        else:
            per_set, tokens = meta
            indoor, outdoor = _set_totals(members, tokens)
            valid = (
                indoor > 0
                and indoor % per_set == 0
                and outdoor == indoor // per_set
            )
        if valid:
            set_group["quantity"] = str(outdoor)
            set_group["include"] = True
            set_group["setIncomplete"] = False
            set_group.pop("setIssueRow", None)
            set_group["issues"] = [
                issue
                for issue in set_group.get("issues") or []
                if "整套计价" not in str(issue)
            ]
            for member in members:
                member["isComponent"] = True
                member["componentOfSet"] = set_group.get("key")
                member["include"] = False
                member["setIncomplete"] = False
                member["unit"] = "台"
            continue
        message = _set_issue_message(row_number, indoor, outdoor, per_set)
        set_group["include"] = True
        set_group["setIncomplete"] = True
        set_group["setIssueRow"] = row_number
        set_group["issues"] = [message]
        for member in members:
            member["include"] = False
            member["setIncomplete"] = True

    # 2) 仍按台计价、但价格行是套价行的组：数量匹配时自动成套，否则标阻塞。
    pending = [
        group
        for group in equipment
        if not group.get("isSet")
        and not group.get("isComponent")
        and not group.get("isComponentManual")
        and group.get("include")
    ]
    for row_number, row in rows_by_number.items():
        meta = _set_row_meta(row)
        if meta is None:
            continue
        per_set, tokens = meta
        candidates = [
            group for group in pending if row_number_of(group) == row_number
        ]
        if not candidates:
            continue
        scopes: dict[tuple[str, str], list[dict]] = {}
        for group in candidates:
            scopes.setdefault(
                (str(group.get("view") or ""), str(group.get("floorLabel") or "")),
                [],
            ).append(group)
        for scope_groups in scopes.values():
            indoor, outdoor = _set_totals(scope_groups, tokens)
            valid = (
                indoor > 0
                and indoor % per_set == 0
                and outdoor == indoor // per_set
            )
            if not valid:
                message = _set_issue_message(
                    row_number, indoor, outdoor, per_set
                )
                for group in scope_groups:
                    group["setIncomplete"] = True
                    group["setIssueRow"] = row_number
                    if message not in (group.get("issues") or []):
                        group.setdefault("issues", []).append(message)
                continue
            set_key = f"set|{row_number}|" + "|".join(
                sorted(str(group.get("key")) for group in scope_groups)
            )
            set_group = {
                "key": set_key,
                "ruleKey": f"set|{row_number}",
                "label": f"{row.get('name') or row.get('spec') or '成套设备'}"
                f"（1套={per_set}台内机+1台外机）",
                "model": str(row.get("spec") or ""),
                "layer": "成套合并",
                "blockName": "",
                "view": scope_groups[0].get("view") or "",
                "floorLabel": scope_groups[0].get("floorLabel") or "",
                "sourceInspectionId": scope_groups[0].get("sourceInspectionId"),
                "sourceFileName": scope_groups[0].get("sourceFileName"),
                "quantity": str(outdoor),
                "unit": "套",
                "priceRow": row_number,
                "priceLabel": _row_label(row),
                "include": True,
                "confidence": "set",
                "isSet": True,
                "setIncomplete": False,
                "setComponents": [
                    {
                        "key": group.get("key"),
                        "label": group.get("label"),
                        "quantity": group.get("quantity"),
                    }
                    for group in scope_groups
                ],
                "modelSource": "set_rule",
                "modelConflict": False,
                "issues": [],
                "sourceHandles": sum(
                    (list(group.get("sourceHandles") or []) for group in scope_groups),
                    [],
                )[:20],
                "textHints": [],
            }
            message = "已并入成套计价，不再单独按台计费。"
            for group in scope_groups:
                group["isComponent"] = True
                group["componentOfSet"] = set_key
                group["include"] = False
                group["setIncomplete"] = False
                group["unit"] = "台"
                if message not in (group.get("issues") or []):
                    group.setdefault("issues", []).append(message)
            equipment.insert(0, set_group)
            by_key[set_key] = set_group


    # 2b) 包装套机（挂机/柜机等 unit=套、没有“1拖N”说明）：
    # 套价行型号与室内机同系列，且存在同系列、同数量的室外机时组成 1 套。
    # 室外机即使暂未匹配价格行也可以作为套内成员（由套装统一计价）。
    pool = [
        group
        for group in equipment
        if not group.get("isSet")
        and not group.get("isComponent")
        and not group.get("isComponentManual")
    ]
    for row_number, row in rows_by_number.items():
        if not _is_packaged_set_row(row):
            continue
        tokens = _model_tokens(str(row.get("spec") or ""))
        row_spec = str(row.get("spec") or "")
        indoor_groups = [
            group
            for group in pool
            if row_number_of(group) == row_number
            and _component_role(group, tokens) == "indoor"
            and _same_model_family(row_spec, str(group.get("model") or ""))
        ]
        for indoor in indoor_groups:
            quantity = int(float(indoor.get("quantity") or 0))
            if quantity <= 0:
                continue
            outdoor_matches = [
                group
                for group in pool
                if group is not indoor
                and _component_role(group, tokens) == "outdoor"
                and int(float(group.get("quantity") or 0)) == quantity
                and str(group.get("view") or "") == str(indoor.get("view") or "")
                and str(group.get("floorLabel") or "")
                == str(indoor.get("floorLabel") or "")
                and _same_model_family(
                    str(indoor.get("model") or ""),
                    str(group.get("model") or ""),
                )
            ]
            if len(outdoor_matches) != 1:
                indoor["setIncomplete"] = True
                indoor["setIssueRow"] = row_number
                message = (
                    f"模板第 {row_number} 行是整机套装，但当前没有唯一的"
                    "同系列室外机与室内机对应；请人工确认配套关系，不能重复计价。"
                )
                if message not in (indoor.get("issues") or []):
                    indoor.setdefault("issues", []).append(message)
                continue
            outdoor = outdoor_matches[0]
            set_key = f"set|{row_number}|packaged|{indoor.get('key')}"
            set_group = {
                "key": set_key,
                "ruleKey": f"set|{row_number}",
                "label": f"{row.get('name') or row.get('spec') or '整机套装'}"
                "（1套=室内机+室外机）",
                "model": row_spec,
                "layer": "成套合并",
                "blockName": "",
                "view": indoor.get("view") or "",
                "floorLabel": indoor.get("floorLabel") or "",
                "sourceInspectionId": indoor.get("sourceInspectionId"),
                "sourceFileName": indoor.get("sourceFileName"),
                "quantity": str(quantity),
                "unit": "套",
                "priceRow": row_number,
                "priceLabel": _row_label(row),
                "include": True,
                "confidence": "set",
                "isSet": True,
                "setIncomplete": False,
                "setComponents": [
                    {
                        "key": group.get("key"),
                        "label": group.get("label"),
                        "quantity": group.get("quantity"),
                    }
                    for group in (indoor, outdoor)
                ],
                "modelSource": "packaged_set_rule",
                "modelConflict": False,
                "issues": [],
                "sourceHandles": sum(
                    (
                        list(group.get("sourceHandles") or [])
                        for group in (indoor, outdoor)
                    ),
                    [],
                )[:20],
                "textHints": [],
            }
            message = "已并入整机套装计价，不再单独按台计费。"
            for group in (indoor, outdoor):
                group["isComponent"] = True
                group["componentOfSet"] = set_key
                group["include"] = False
                group["setIncomplete"] = False
                group["unit"] = "台"
                group["issues"] = [
                    issue
                    for issue in group.get("issues") or []
                    if "整套计价" not in str(issue)
                    and "整机套装" not in str(issue)
                ]
                if message not in group["issues"]:
                    group["issues"].append(message)
            equipment.insert(0, set_group)
            by_key[set_key] = set_group
            if set_group in pool:
                pool.remove(set_group)
            for group in (indoor, outdoor):
                if group in pool:
                    pool.remove(group)

    # 3) 价格行已改回单机行的组：清掉遗留的成套阻塞状态。
    for group in equipment:
        if group.get("isSet") or group.get("isComponent"):
            continue
        if group.get("setIssueRow") and row_number_of(group) != group.get(
            "setIssueRow"
        ):
            group["setIncomplete"] = False
            group["issues"] = [
                issue
                for issue in group.get("issues") or []
                if "整套计价" not in str(issue)
            ]

    data["equipment"] = equipment
    return data


def _collapse_equipment_sets(
    equipment: list[dict], rows_by_number: dict[int, dict]
) -> list[dict]:
    """同一“套”价行出现室内/外机多组时，合并为按套计价，避免重复计套。"""
    by_row: dict[int, list[dict]] = {}
    for group in equipment:
        row_number = group.get("priceRow")
        if row_number:
            by_row.setdefault(int(row_number), []).append(group)
    # 套价行同时列出室内/外机型号时，把已被单机行吸走的套内机改挂到套价行，
    # 再由下面的数量核对决定能否自动合并；数量不符仍会阻止直接计套。
    for row_number, row in rows_by_number.items():
        if row.get("unit") != "套":
            continue
        if not SET_NAME_RE.search(str(row.get("name") or "")):
            continue
        tokens = _model_tokens(str(row.get("spec") or ""))
        indoor_tokens = tokens[1:] if len(tokens) > 1 else tokens
        if not indoor_tokens:
            continue
        for group in equipment:
            if group.get("isSet") or group.get("isComponent"):
                continue
            if group.get("priceRow") == row_number:
                continue
            model_normalized = _model_key(str(group.get("model") or ""))
            if not model_normalized:
                continue
            if any(
                _model_key(token)
                and _model_key(token) in model_normalized
                for token in indoor_tokens
            ):
                old_row = group.get("priceRow")
                if old_row and int(old_row) in by_row:
                    by_row[int(old_row)] = [
                        item
                        for item in by_row[int(old_row)]
                        if item is not group
                    ]
                group["priceRow"] = row_number
                group["priceLabel"] = _row_label(row)
                group["confidence"] = "set_candidate"
                by_row.setdefault(row_number, []).append(group)
    set_groups: list[dict] = []
    for row_number, groups in by_row.items():
        row = rows_by_number.get(row_number, {})
        if row.get("unit") != "套":
            continue
        set_match = SET_NAME_RE.search(str(row.get("name") or ""))
        if not set_match:
            continue
        indoor_per_set = int(set_match.group(1))
        if indoor_per_set <= 0:
            continue
        tokens = _model_tokens(str(row.get("spec") or ""))
        indoor_groups = []
        outdoor_groups = []
        for group in groups:
            role = _component_role(group, tokens)
            if role == "indoor":
                indoor_groups.append(group)
            elif role == "outdoor":
                outdoor_groups.append(group)
        if len(groups) < 2 or not indoor_groups or not outdoor_groups:
            for group in groups:
                group["setIncomplete"] = True
                group["setIssueRow"] = row_number
                group["componentKind"] = _component_role(group, tokens)
                group["issues"].append(
                    f"模板第 {row_number} 行按“1拖{indoor_per_set}”整套计价，"
                    "但没有同时识别到室内机和外机；请补录缺失设备或改选单机价格行。"
                )
            continue
        indoor_total = sum(int(float(group.get("quantity") or 0)) for group in indoor_groups)
        outdoor_total = sum(int(float(group.get("quantity") or 0)) for group in outdoor_groups)
        if indoor_total % indoor_per_set != 0 or outdoor_total != indoor_total // indoor_per_set:
            message = (
                f"模板第 {row_number} 行按“1拖{indoor_per_set}”整套计价："
                f"当前识别到室内机 {indoor_total} 台、室外机 {outdoor_total} 台，"
                "与成套数量不一致；请核对数量或改选单机价格行。"
            )
            for group in groups:
                group["setIncomplete"] = True
                group["setIssueRow"] = row_number
                group["componentKind"] = _component_role(group, tokens)
                group["issues"].append(message)
            continue
        sets = outdoor_total
        set_key = f"set|{row_number}|" + "|".join(
            sorted(str(group.get("key")) for group in groups)
        )
        set_group = {
            "key": set_key,
            "ruleKey": f"set|{row_number}",
            "label": f"{row.get('name') or row.get('spec') or '成套设备'}"
            f"（1套={indoor_per_set}台内机+1台外机）",
            "model": str(row.get("spec") or ""),
            "layer": "成套合并",
            "blockName": "",
            "view": (
                groups[0].get("view")
                if all(group.get("view") == groups[0].get("view") for group in groups)
                else "多个视图"
            ),
            "floorLabel": "、".join(
                sorted({str(group.get("floorLabel")) for group in groups if group.get("floorLabel")})
            )
            or "多个楼层",
            "sourceInspectionId": groups[0].get("sourceInspectionId"),
            "sourceFileName": groups[0].get("sourceFileName"),
            "quantity": sets,
            "unit": "套",
            "priceRow": row_number,
            "priceLabel": _row_label(row),
            "include": True,
            "confidence": "set",
            "isSet": True,
            "setComponents": [
                {
                    "key": group.get("key"),
                    "label": group.get("label"),
                    "quantity": group.get("quantity"),
                }
                for group in groups
            ],
            "modelSource": "set_rule",
            "modelConflict": False,
            "issues": [],
            "sourceHandles": sum(
                (list(group.get("sourceHandles") or []) for group in groups), []
            )[:20],
            "textHints": [],
        }
        for group in groups:
            group["isComponent"] = True
            group["componentOfSet"] = set_key
            group["include"] = False
            group["confidence"] = "set_component"
            group["issues"].append("已并入成套计价，不再单独按台计费。")
        set_groups.append(set_group)
    if not set_groups:
        return equipment
    components = [group for group in equipment if group.get("isComponent")]
    others = [group for group in equipment if not group.get("isComponent")]
    return set_groups + components + others


def build_draft_data(
    inspection: dict,
    price_book: dict,
    *,
    title: str = "",
    view: str = "",
    rules: dict[tuple[str, str], int] | None = None,
) -> dict:
    report = inspection.get("report") or {}
    rows = list((price_book.get("book") or {}).get("rows") or [])
    rules = rules or {}
    rows_by_number = {int(row["rowNumber"]): row for row in rows}
    views = report.get("views") or []
    selected_value = str(view or "").strip()
    selected_floor = ""
    if "::" in selected_value:
        selected_view, selected_floor = selected_value.split("::", 1)
    else:
        selected_view = selected_value
    if selected_view in {"", "__all__", "全部", "全部视图", "全部楼层"}:
        selected_view = ""
    view_summary = list(report.get("viewSummary") or [])
    floor_summary = list(report.get("floorSummary") or [])
    scope_summary = floor_summary if floor_summary else view_summary
    if selected_floor:
        view_label = selected_floor
    elif not selected_view and len(scope_summary) > 1:
        view_label = (
            f"全部楼层（{len(scope_summary)} 个）"
            if floor_summary
            else "全部视图"
        )
    elif selected_view:
        view_label = selected_view
    else:
        view_label = (
            str(scope_summary[0].get("floorLabel") or scope_summary[0].get("view"))
            if scope_summary
            else "model"
        )
    units = report.get("units") or {}
    scale = units.get("metresPerDrawingUnit")
    source_file = str(inspection.get("fileName") or "")

    def in_scope(group: dict) -> bool:
        if selected_view and group.get("view") != selected_view:
            return False
        if selected_floor and group.get("floorLabel") != selected_floor:
            return False
        return True
    # 图纸单位/楼层范围由草稿页人工确认；原始 reader 的通用阻断项
    # 不能直接沿用，否则每个新图纸都会被永久阻塞。
    resolved_by_draft = {"SCALE_NOT_VERIFIED", "FLOOR_SCOPE_UNRESOLVED"}
    review_only = {
        "IMAGE_NOT_LOADED",
        "PROXY_OBJECT",
        "CAD_AUDIT_CHANGED",
        "DWG_CONVERSION_UNVERIFIED",
        "NO_PIPE_CANDIDATES",
        "NO_EQUIPMENT_CANDIDATES",
    }
    issues = [
        {
            "code": item.get("code", "ISSUE"),
            "message": item.get("message", ""),
            "severity": (
                "review"
                if item.get("code") in review_only
                else item.get("severity", "review")
            ),
            "detail": {"sources": item.get("sources", [])},
        }
        for item in report.get("issues", [])
        if item.get("code") not in resolved_by_draft
    ]
    equipment = []
    for group in report.get("equipmentGroups", []):
        if not in_scope(group):
            continue
        model = str(group.get("modelHint") or group.get("blockName") or "")
        saved_row = rows_by_number.get(
            rules.get(
                ("equipment", str(group.get("ruleKey") or group.get("key")))
            )
        )
        if saved_row is not None and saved_row.get("priceState") == "priced":
            price_row, confidence = saved_row, "rule"
        else:
            price_row, confidence = _equipment_match(
                model, rows, str(group.get("systemHint") or "refrigerant")
            )
        equipment.append(
            {
                "key": group.get("key"),
                "label": model or group.get("key"),
                "model": model,
                "layer": group.get("layer", ""),
                "blockName": group.get("blockName", ""),
                "view": group.get("view", ""),
                "floorLabel": group.get("floorLabel", ""),
                "sourceInspectionId": inspection.get("id"),
                "sourceFileName": source_file,
                "quantity": int(group.get("quantity") or 0),
                "unit": (price_row or {}).get("unit") or group.get("unit") or "台",
                "priceRow": (price_row or {}).get("rowNumber"),
                "priceLabel": _row_label(price_row) if price_row else "",
                "isNonCentral": bool(
                    price_row
                    and (
                        "KFR" in model.upper()
                        or any(
                            marker in f"{price_row.get('name', '')}{price_row.get('spec', '')}"
                            for marker in ("挂机", "柜机")
                        )
                    )
                ),
                "include": price_row is not None
                and confidence in {"exact", "rule"},
                "confidence": confidence,
                "modelSource": group.get("modelSource", ""),
                "procurementModelPending": group.get("modelSource") == "block",
                "modelConflict": bool(group.get("modelConflict")),
                "systemHint": group.get("systemHint", ""),
                "equipmentKind": group.get("equipmentKind", ""),
                "role": group.get("role", ""),
                "isWater": bool(
                    group.get("systemHint") == "water"
                    or any(
                        marker in f"{group.get('layer', '')}{group.get('category', '')}{model}"
                        for marker in ("水泵", "阀门", "冷却塔", "水管", "冷冻水", "冷却水", "补水")
                    )
                ),
                "manualMaterial": None,
                "manualInstallation": None,
                "manualUnit": (price_row or {}).get("unit") or group.get("unit") or "台",
                "issues": list(group.get("issues") or []),
                "sourceHandles": group.get("handles", []),
                "textHints": group.get("textHints", []),
            }
        )
    equipment = _collapse_equipment_sets(equipment, rows_by_number)
    equipment = reconcile_equipment_sets(
        {"equipment": equipment}, rows_by_number
    )["equipment"]
    routes = []
    for group in report.get("routeGroups", []):
        if not in_scope(group):
            continue
        length_m = group.get("lengthM")
        saved_row = rows_by_number.get(
            rules.get(
                ("route", str(group.get("ruleKey") or group.get("key")))
            )
        )
        if saved_row is not None and saved_row.get("priceState") == "priced":
            price_row, confidence = saved_row, "rule"
        else:
            price_row, confidence = _route_match(group, rows)
        linked = _insulation_match(group, rows)
        routes.append(
            {
                "key": group.get("key"),
                "label": group.get("layer") or group.get("key"),
                "layer": group.get("layer", ""),
                "category": group.get("category", ""),
                "medium": group.get("medium", ""),
                "role": group.get("role", ""),
                "spec": group.get("spec", ""),
                "pairSpec": group.get("pairSpec", ""),
                "specSource": group.get("specSource", ""),
                "detectedBy": group.get("detectedBy", ""),
                "layerCertain": bool(group.get("layerCertain", True)),
                "isWater": bool(
                    group.get("category") in WATER_ROUTE_CATEGORIES
                    or any(
                        marker in f"{group.get('layer', '')}{group.get('category', '')}"
                        for marker in ("水管", "冷冻水", "冷却水", "补水")
                    )
                ),
                "manualMaterial": None,
                "manualInstallation": None,
                "manualUnit": (price_row or {}).get("unit") or "米",
                "view": group.get("view", ""),
                "floorLabel": group.get("floorLabel", ""),
                "sourceRegionKey": group.get("sourceRegionKey", ""),
                "sourceInspectionId": inspection.get("id"),
                "sourceFileName": source_file,
                "lengthRaw": group.get("lengthRaw"),
                "lengthM": length_m,
                "lengthKnownRaw": group.get("lengthKnownRaw"),
                "lengthKnownM": group.get("lengthKnownM"),
                "unmeasuredSegmentCount": int(
                    group.get("unmeasuredSegmentCount") or 0
                ),
                "lengthStatus": group.get("lengthStatus") or "complete",
                "unit": (price_row or {}).get("unit") or "米",
                "priceRow": (price_row or {}).get("rowNumber"),
                "priceLabel": _row_label(price_row) if price_row else "",
                "include": price_row is not None
                and length_m is not None
                and confidence in {"exact", "rule", "spec"}
                and group.get("autoInclude") is True
                and group.get("eligibility") == "eligible"
                and bool(group.get("routeEvidenceKey")),
                "autoInclude": group.get("autoInclude") is True,
                "eligibility": group.get("eligibility") or "review",
                "reviewReasons": list(group.get("reviewReasons") or []),
                "routeEvidenceKey": group.get("routeEvidenceKey") or "",
                "evidence": group.get("evidence") or {},
                "weakEvidence": bool(group.get("weakEvidence")),
                "confidence": confidence,
                "linkedMaterial": linked,
                "linkedMaterialMissing": linked is None
                and "保温" not in str(group.get("category") or ""),
                "sourceHandles": group.get("handles", []),
                "segments": list(group.get("segments") or []),
                "annotation": group.get("annotation", ""),
                "textHints": group.get("textHints", []),
                "issues": list(group.get("issues") or []),
            }
        )
    for route in routes:
        route["routeProposalKey"] = route_proposal_key(route)
    if any(
        group.get("systemHint") == "water" for group in equipment
    ) or any(
        group.get("category") in WATER_ROUTE_CATEGORIES for group in routes
    ):
        issues.append(
            {
                "code": "WATER_PRICE_BASIS_MISSING",
                "message": (
                    "检测到水系统候选。当前模板没有经确认的水系统专用价格依据，"
                    "水泵、阀门、水管和保温需要人工选择模板行或补价，"
                    "不能按冷媒管价格自动套用。"
                ),
                "severity": "review",
                "sources": [],
            }
        )
    return {
        "schemaVersion": "generic-draft-2",
        "title": title or f"新图纸报价 · {inspection.get('fileName', '')}",
        "drawingName": inspection.get("fileName", ""),
        "inspectionId": inspection.get("id"),
        "inspectionIds": [inspection.get("id")],
        "sourceInspectionId": inspection.get("id"),
        "sourceFileName": source_file,
        "sourceFiles": [
            {
                "inspectionId": inspection.get("id"),
                "fileName": source_file,
                "view": view_label,
            }
        ],
        "priceBookId": price_book.get("id"),
        "priceBookName": price_book.get("name", ""),
        "view": view_label,
        "viewOptions": scope_summary,
        "floorSummary": floor_summary,
        "views": scope_summary
        or [{"view": view_label, "floorLabel": view_label}],
        "scale": scale,
        "scaleEditable": True,
        "equipment": equipment,
        "routes": routes,
        "issues": issues,
        "note": "新图纸候选经人工确认后计价；未画出的竖向段、接管、预留和损耗不计入。",
    }


def build_multi_draft_data(
    inspections: list[dict],
    price_book: dict,
    *,
    title: str = "",
    rules: dict[tuple[str, str], int] | None = None,
) -> dict:
    if not inspections:
        raise ValueError("至少需要一份图纸检查")
    if len(inspections) == 1:
        return build_draft_data(
            inspections[0], price_book, title=title, rules=rules
        )
    equipment: list[dict] = []
    routes: list[dict] = []
    issues: list[dict] = []
    view_options: list[dict] = []
    views: list[dict] = []
    source_files: list[dict] = []
    source_scales: dict[str, str] = {}
    scales: list[str | None] = []
    for inspection in inspections:
        one = build_draft_data(
            inspection, price_book, title=title, rules=rules
        )
        prefix = str(inspection.get("id") or "inspection")
        file_name = str(inspection.get("fileName") or "图纸")
        if one.get("scale") not in (None, ""):
            source_scales[prefix] = str(one.get("scale"))
        source_files.append(
            {
                "inspectionId": inspection.get("id"),
                "fileName": file_name,
                "view": one.get("view"),
            }
        )
        for item in one.get("viewOptions") or []:
            view_options.append(
                {
                    **item,
                    "fileName": file_name,
                    "inspectionId": inspection.get("id"),
                }
            )
        for item in one.get("views") or []:
            views.append(
                {
                    **item,
                    "fileName": file_name,
                    "inspectionId": inspection.get("id"),
                }
            )
        for item in one.get("equipment") or []:
            group = dict(item)
            old_key = str(group.get("key") or "")
            group["key"] = f"{prefix}|{old_key}"
            group["sourceInspectionId"] = inspection.get("id")
            group["sourceFileName"] = file_name
            group["sourceHandles"] = list(group.get("sourceHandles") or [])
            equipment.append(group)
        for item in one.get("equipment") or []:
            if not item.get("isSet"):
                continue
            mapped = next(
                (
                    group
                    for group in equipment
                    if group.get("sourceInspectionId") == inspection.get("id")
                    and str(group.get("key")) == f"{prefix}|{item.get('key')}"
                ),
                None,
            )
            if mapped is None:
                continue
            mapped["setComponents"] = [
                {
                    **component,
                    "key": f"{prefix}|{component.get('key')}",
                }
                for component in item.get("setComponents") or []
            ]
            for group in equipment:
                if group.get("sourceInspectionId") != inspection.get("id"):
                    continue
                if group.get("componentOfSet") == item.get("key"):
                    group["componentOfSet"] = mapped["key"]
        for item in one.get("routes") or []:
            group = dict(item)
            group["key"] = f"{prefix}|{item.get('key')}"
            group["sourceInspectionId"] = inspection.get("id")
            group["sourceFileName"] = file_name
            group["sourceHandles"] = list(group.get("sourceHandles") or [])
            routes.append(group)
        for item in one.get("issues") or []:
            issues.append(
                {
                    **item,
                    "message": f"{file_name}：{item.get('message')}",
                    "sourceFileName": file_name,
                    "sourceInspectionId": inspection.get("id"),
                }
            )
        scales.append(one.get("scale"))
    known_scales = [str(scale) for scale in scales if scale not in (None, "")]
    common_scale = (
        known_scales[0]
        if known_scales and len(set(known_scales)) == 1
        else None
    )
    return {
        "schemaVersion": "generic-draft-2",
        "title": title
        or f"多图纸合并报价 · {'、'.join(item['fileName'] for item in source_files)}",
        "drawingName": "、".join(item["fileName"] for item in source_files),
        "inspectionId": inspections[0].get("id"),
        "inspectionIds": [item.get("id") for item in inspections],
        "sourceInspectionId": inspections[0].get("id"),
        "sourceFileName": source_files[0]["fileName"],
        "sourceFiles": source_files,
        "priceBookId": price_book.get("id"),
        "priceBookName": price_book.get("name", ""),
        "view": "全部图纸与视图",
        "viewOptions": view_options,
        "views": views,
        "scale": common_scale,
        "sourceScales": source_scales,
        "scaleEditable": False,
        "equipment": equipment,
        "routes": routes,
        "issues": issues,
        "note": (
            "多张图纸合并为一份报价；各图纸的候选数量和长度分别保留来源，"
            "未画出的竖向段、接管、预留和损耗不计入。"
        ),
    }


def _manual_price(group: dict) -> tuple[Decimal, Decimal] | None:
    material = group.get("manualMaterial")
    installation = group.get("manualInstallation")
    if material in (None, "") and installation in (None, ""):
        return None
    try:
        material_value = Decimal(str(material if material not in (None, "") else "0"))
        installation_value = Decimal(
            str(installation if installation not in (None, "") else "0")
        )
    except (InvalidOperation, TypeError, ValueError):
        return None
    if material_value < 0 or installation_value < 0:
        return None
    return material_value, installation_value


def _excluded_group_omission(
    group: dict,
    kind: str,
    *,
    price_rows: dict[int, dict],
) -> OmittedItem:
    """Build one business-readable omission; never turns a candidate into quantity."""
    row_number = int(group.get("priceRow") or 0)
    row = price_rows.get(row_number)
    manual = _manual_price(group) is not None
    priced = manual or bool(row and row.get("priceState") == "priced")
    if kind == "equipment":
        model = str(group.get("model") or "").strip()
        block = str(group.get("blockName") or "").strip()
        quantity = group.get("quantity")
        model_source = str(group.get("modelSource") or "")
        role = str(group.get("role") or "")
        kind_label = str(group.get("equipmentKind") or "") or (
            "室外机"
            if role == "outdoor"
            else "室内机"
            if role == "indoor"
            else "设备"
        )
        if (model_source == "block" and (block or model)) or (
            not model and block
        ):
            subject = (
                f"未计入：{kind_label}图块 {block or model}；"
                f"候选{quantity if quantity is not None else '若干'}台，数量待核准；"
                "采购型号及单价待确认。请补齐真实型号和对应价格。"
            )
            reason = "model_unconfirmed"
        elif not priced:
            subject = (
                f"未计入：{model or block or '设备'}；"
                f"候选{quantity if quantity is not None else '若干'}台，数量待核准；"
                "缺匹配设备单价。请确认型号/数量并选择或补充设备价格。"
            )
            reason = "missing_price"
        else:
            subject = (
                f"未计入：{model or block or '设备'}；"
                "用户未纳入本次报价。"
            )
            reason = "user_excluded"
        return OmittedItem(subject=subject, reason=reason, evidence=[])

    category = str(group.get("category") or "").strip()
    spec = str(group.get("spec") or "").strip()
    if category in {"", "unknown"}:
        label = "管线类别待确认"
        if spec:
            label += f"（规格线索 DN{spec}）"
    elif category == "排水" and spec:
        label = f"DN{spec}排水管"
    else:
        label = " ".join(part for part in (category, spec) if part) or "管线"
    length = group.get("lengthM")
    parts = [f"未计入：{label}"]
    if length in (None, ""):
        parts.append("长度待核（长度未确定；未测段不能用 0 代替）")
        known_length = group.get("lengthKnownM")
        unmeasured = int(group.get("unmeasuredSegmentCount") or 0)
        if known_length not in (None, "") and unmeasured:
            parts.append(
                f"已知小计{format_quantity(known_length)}米，"
                f"另有{unmeasured}段未测"
            )
    else:
        parts.append(
            f"候选{format_quantity(length)}米，非本次已计入工程量"
        )
    if not priced:
        parts.append("缺匹配管价")
    if group.get("linkedMaterialMissing"):
        parts.append("对应保温也缺匹配价格/行")
    if priced and not group.get("linkedMaterialMissing"):
        parts.append("用户未纳入本次报价")
    parts.append(
        "请确认管段范围并选择或补充同类同规格价格；不得代用铜管或其它管径。"
    )
    if (
        group.get("reviewReasons")
        or group.get("eligibility") == "review"
        or length in (None, "")
    ):
        reason = "route_review_required"
    elif not priced:
        reason = "missing_price"
    else:
        reason = "user_excluded"
    return OmittedItem(subject="；".join(parts), reason=reason, evidence=[])


def _canonical_decimal(value) -> str:
    if value in (None, ""):
        return ""
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


def route_proposal_key(group: dict) -> str:
    """Bind the exact current takeoff proposal (not the template price).

    The CAD evidence key is immutable. This second key changes when the
    effective quantity/spec/scope shown to the user changes, so an old
    checkbox cannot confirm a new value.
    """
    payload = {
        "routeEvidenceKey": str(group.get("routeEvidenceKey") or ""),
        "sourceInspectionId": str(group.get("sourceInspectionId") or ""),
        "sourceRegionKey": str(group.get("sourceRegionKey") or ""),
        "view": str(group.get("view") or ""),
        "floorLabel": str(group.get("floorLabel") or ""),
        "lengthRaw": _canonical_decimal(group.get("lengthRaw")),
        "lengthM": _canonical_decimal(group.get("lengthM")),
        "spec": str(
            group.get("manualSpec") or group.get("spec") or ""
        ).strip(),
        "physicalSegmentIds": sorted(
            str(item.get("physicalSegmentId"))
            for item in group.get("segments") or []
            if item.get("physicalSegmentId")
        ),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _current_proposal_error(data: dict) -> str | None:
    """Recompute the proposal at the generation boundary.

    A stale stored key, or a key computed from values the user changed after
    the page was rendered, must be rejected before the template is opened.
    """
    for group in data.get("routes") or []:
        if not group.get("include") or group.get("origin") == "customer_input":
            continue
        stored = str(group.get("routeProposalKey") or "")
        if not stored:
            return "管线组的当前计量提案尚未生成，请保存并重新核对。"
        if stored != route_proposal_key(group):
            return "管线组的当前计量提案已变化，请重新核对后再生成。"
    return None


def _manual_route_record_error(
    group: dict,
    records: dict[str, dict] | None,
    project_id: str,
) -> str | None:
    record_id = str(group.get("manualRecordId") or "").strip()
    key = str(group.get("key") or "")
    if not record_id:
        return "人工补录缺少服务器记录，请重新补录后再计入。"
    if not records:
        return "人工补录记录不在服务器草稿中，请重新补录后再计入。"
    record = records.get(record_id)
    if not isinstance(record, dict):
        return "人工补录记录不在服务器草稿中，请重新补录后再计入。"
    if str(record.get("manualRecordId") or "") != record_id:
        return "人工补录记录ID与服务器记录不一致，请重新补录。"
    if str(record.get("key") or "") != key:
        return "人工补录记录与管线组身份不一致，请重新补录。"
    if project_id and str(record.get("projectId") or "") != project_id:
        return "人工补录记录不属于当前项目，不能计入。"
    effective_spec = str(
        group.get("manualSpec") or group.get("spec") or ""
    )
    checks = (
        ("category", str(group.get("category") or "")),
        ("medium", str(group.get("medium") or "")),
        ("spec", effective_spec),
        ("manualSpec", str(group.get("manualSpec") or "")),
        (
            "manualUnit",
            str(group.get("manualUnit") or group.get("unit") or ""),
        ),
        ("manualMaterial", _canonical_decimal(group.get("manualMaterial"))),
        (
            "manualInstallation",
            _canonical_decimal(group.get("manualInstallation")),
        ),
    )
    for field, current in checks:
        recorded = record.get(field)
        if field in {"manualMaterial", "manualInstallation"}:
            recorded = _canonical_decimal(recorded)
        else:
            recorded = str(recorded or "")
        if recorded != current:
            return "人工补录的类别/介质/规格/单位或价格与服务器记录不一致，请重新核对。"
    if _canonical_decimal(record.get("lengthM")) != _canonical_decimal(
        group.get("lengthM")
    ):
        return "人工补录长度与服务器记录不一致，请重新核对。"
    if str(record.get("priceRow") or "") != str(
        group.get("priceRow") or ""
    ):
        return "人工补录模板行与服务器记录不一致，请重新核对。"
    return None


def _manual_route_error(
    data: dict,
    records: dict[str, dict] | None,
    project_id: str,
) -> str | None:
    for group in data.get("routes") or []:
        if not group.get("include"):
            continue
        if group.get("origin") != "customer_input":
            continue
        error = _manual_route_record_error(group, records, project_id)
        if error:
            return error
    return None


def route_adoption_error(group: dict) -> str | None:
    """Server-side route adoption gate; never trusts a template/price match."""
    if not group.get("include"):
        return None
    if group.get("origin") == "customer_input":
        # Created by the server's explicit manual-entry path, not a CAD group.
        if not group.get("manualRecordId"):
            return "人工补录缺少服务器记录，请重新补录后再计入。"
        return None
    if not group.get("routeEvidenceKey"):
        return "管线来自旧版检查，需重新检查图纸后核对。"
    if group.get("eligibility") == "excluded":
        return "该对象不是可计入的路线候选。"
    proposal_key = str(group.get("routeProposalKey") or "")
    if not proposal_key:
        return "当前管线计量提案尚未生成，请保存后在页面重新核对。"
    if group.get("eligibility") == "eligible":
        return None
    review = group.get("routeReview") or {}
    if (
        review.get("confirmed") is True
        and review.get("evidenceKey") == group["routeEvidenceKey"]
        and review.get("proposalKey") == proposal_key
        and group.get("medium") not in (None, "", "unknown")
        and str(group.get("spec") or group.get("manualSpec") or "").strip()
    ):
        return None
    return "该组管线的身份、规格或范围尚未核对，不能仅补比例或价格后计入。"


def draft_exceptions(data: dict, price_book: dict) -> list[dict]:
    rows = {
        int(row["rowNumber"]): row
        for row in (price_book.get("book") or {}).get("rows", [])
    }
    exceptions = list(data.get("issues") or [])
    if data.get("scaleEditable", True) and data.get("scale") in (None, "", "0"):
        if any(group.get("include") for group in data.get("routes", [])):
            exceptions.append(
                {
                    "code": "SCALE_UNCONFIRMED",
                    "message": "有管线勾选，但比例未确认；不能形成米制报价。",
                    "severity": "blocks_pricing",
                    "detail": {},
                }
            )
    for group in data.get("routes", []):
        if not group.get("include"):
            continue
        adoption_error = route_adoption_error(group)
        if adoption_error:
            exceptions.append(
                {
                    "code": "ROUTE_ADOPTION_UNCONFIRMED",
                    "message": (
                        f"管线组“{group.get('label') or group.get('key')}”："
                        f"{adoption_error}"
                    ),
                    "severity": "blocks_pricing",
                    "detail": {
                        "groupKey": group.get("key"),
                        "reviewReasons": list(group.get("reviewReasons") or []),
                    },
                }
            )
        if _manual_price(group) is not None:
            continue
        row_number = int(group.get("priceRow") or 0)
        row = rows.get(row_number)
        if row is None:
            continue
        incompatibility = route_row_error(group, row)
        if incompatibility:
            exceptions.append(
                {
                    "code": "ROUTE_PRICE_INCOMPATIBLE",
                    "message": (
                        f"管线组“{group.get('label') or group.get('key')}”："
                        f"{incompatibility}；请改选同系统同材质的模板行，"
                        "或填写客户人工价。"
                    ),
                    "severity": "blocks_pricing",
                    "detail": {
                        "groupKey": group.get("key"),
                        "priceRow": row_number,
                    },
                }
            )
    for group in data.get("equipment", []):
        if group.get("isComponent") or group.get("isComponentManual"):
            continue
        if (
            group.get("include")
            and group.get("setConfirmed")
            and group.get("setIncomplete")
        ):
            row_number = group.get("setIssueRow") or group.get("priceRow")
            exceptions.append(
                {
                    "code": "SET_CONFIRMED_BUT_INCOMPLETE",
                    "message": (
                        f"设备组“{group.get('label') or group.get('key')}”勾选了按套确认，"
                        f"但模板第 {row_number} 行按成套计价，识别到的室内/外机数量"
                        "与成套数量不一致；请改为正确套数，或把室内机单独选择"
                        "单机价格行，不能直接按套生成。"
                    ),
                    "severity": "blocks_pricing",
                    "detail": {"groupKey": group.get("key")},
                }
            )
            continue
        for issue in group.get("issues") or []:
            exceptions.append(
                {
                    "code": "SET_COMPONENTS_INCOMPLETE"
                    if group.get("setIncomplete") and group.get("include")
                    else "EQUIPMENT_REVIEW",
                    "message": str(issue),
                    "severity": "blocks_pricing"
                    if group.get("setIncomplete") and group.get("include")
                    else "review",
                    "detail": {"groupKey": group.get("key")},
                }
            )
        if not group.get("include"):
            continue
        row = rows.get(int(group.get("priceRow") or 0))
        manual_price = _manual_price(group)
        if (
            (group.get("procurementModelPending") or group.get("modelSource") == "block")
            and (row is None or row.get("priceState") != "priced")
            and not str(group.get("manualLabel") or "").strip()
        ):
            exceptions.append(
                {
                    "code": "PROCUREMENT_MODEL_REQUIRED",
                    "message": (
                        f"设备组“{group.get('label') or group.get('key')}”的采购型号待核："
                        "图纸仅提供图块名称。请选择已确认型号的模板价格行，"
                        "或填写人工确认名称/型号及对应材料、安装价。"
                    ),
                    "severity": "blocks_pricing",
                    "detail": {"groupKey": group.get("key")},
                }
            )
        if (row is None or row.get("priceState") != "priced") and manual_price is None:
            exceptions.append(
                {
                    "code": "EQUIPMENT_PRICE_REQUIRED",
                    "message": (
                        f"设备组“{group.get('key')}”缺少可用模板行；"
                        "水系统等模板未覆盖项请填写人工材料价和安装价。"
                    ),
                    "severity": "blocks_pricing",
                    "detail": {"groupKey": group.get("key")},
                }
            )
        try:
            if Decimal(str(group.get("quantity") or "0")) <= 0:
                raise ValueError
        except (InvalidOperation, ValueError):
            exceptions.append(
                {
                    "code": "EQUIPMENT_QUANTITY_INVALID",
                    "message": f"设备组“{group.get('key')}”数量必须大于 0。",
                    "severity": "blocks_pricing",
                    "detail": {"groupKey": group.get("key")},
                }
            )
    for group in data.get("routes", []):
        for issue in group.get("issues") or []:
            message = str(issue)
            if message in {"PIPE_SPEC_UNRESOLVED", "PIPE_SPEC_AMBIGUOUS"}:
                message = (
                    f"管线组“{group.get('key')}”的管径未确定或存在多个标注；"
                    "请确认规格后再套价。"
                )
            elif message == "PIPE_LAYER_UNCERTAIN":
                message = (
                    f"管线组“{group.get('key')}”来自通用图层，"
                    "仅因靠近管径标注而列为候选；请确认它确实是管线。"
                )
            elif message == "POLYLINE_SEGMENTS_ONLY":
                message = "管线长度按折线段累加，需核对弧段和竖向段是否另计。"
            elif message == "ARC_APPROXIMATED":
                message = "圆弧段按弧长近似，需核对是否应并入直管。"
            elif message == "PIPE_SPEC_INFERRED":
                message = (
                    f"管线组“{group.get('key')}”的管径是按同系统唯一 DN 标注推断的，"
                    "未逐段确认；请核对后再计入。"
                )
            elif message in {"SPLINE_LENGTH_UNVERIFIED", "ELLIPSE_LENGTH_UNVERIFIED"}:
                message = "曲线长度未可靠量取，未计入。"
            elif message in {"GEOMETRY_INVALID", "GEOMETRY_UNSUPPORTED"}:
                message = "图形无法可靠量取，未计入。"
            exceptions.append(
                {
                    "code": "PIPE_REVIEW",
                    "message": message,
                    "severity": "review",
                    "detail": {"groupKey": group.get("key")},
                }
            )
        if not group.get("include"):
            continue
        if group.get("linkedMaterialMissing"):
            exceptions.append(
                {
                    "code": "LINKED_INSULATION_PRICE_MISSING",
                    "message": (
                        f"管线组“{group.get('key')}”已计入，但模板里没有"
                        "可匹配的对应保温行；保温未计价，不能按 0 元处理。"
                    ),
                    "severity": "review",
                    "detail": {"groupKey": group.get("key")},
                }
            )
        row = rows.get(int(group.get("priceRow") or 0))
        manual_price = _manual_price(group)
        if (row is None or row.get("priceState") != "priced") and manual_price is None:
            exceptions.append(
                {
                    "code": "PIPE_PRICE_REQUIRED",
                    "message": (
                        f"管线组“{group.get('key')}”缺少可用模板行；"
                        "水系统等模板未覆盖项请填写人工材料价和安装价。"
                    ),
                    "severity": "blocks_pricing",
                    "detail": {"groupKey": group.get("key")},
                }
            )
        try:
            if Decimal(str(group.get("lengthM") or "0")) <= 0:
                raise ValueError
        except (InvalidOperation, ValueError):
            exceptions.append(
                {
                    "code": "PIPE_LENGTH_INVALID",
                    "message": f"管线组“{group.get('key')}”长度必须大于 0。",
                    "severity": "blocks_pricing",
                    "detail": {"groupKey": group.get("key")},
                }
            )
    for index, item in enumerate(data.get("customItems") or [], start=1):
        name = str(item.get("name") or "").strip()
        if not name:
            exceptions.append(
                {
                    "code": "CUSTOM_ITEM_NAME_REQUIRED",
                    "message": f"补充项目 {index} 缺少名称。",
                    "severity": "blocks_pricing",
                    "detail": {"index": index},
                }
            )
            continue
        try:
            quantity = Decimal(str(item.get("quantity") or "0"))
            material = Decimal(str(item.get("material") or "0"))
            installation = Decimal(str(item.get("installation") or "0"))
        except (InvalidOperation, TypeError, ValueError):
            quantity = material = installation = Decimal("-1")
        if quantity <= 0 or material < 0 or installation < 0:
            exceptions.append(
                {
                    "code": "CUSTOM_ITEM_INVALID",
                    "message": (
                        f"补充项目“{name}”的数量必须大于 0，"
                        "材料价和安装价不能为负数。"
                    ),
                    "severity": "blocks_pricing",
                    "detail": {"index": index},
                }
            )
    try:
        global_coefficient = Decimal(str(data.get("globalCoefficient") or "1"))
        equipment_coefficient = Decimal(
            str(
                data.get("equipmentCoefficient")
                if data.get("equipmentCoefficient") not in (None, "")
                else global_coefficient
            )
        )
        pipe_coefficient = Decimal(
            str(
                data.get("pipeCoefficient")
                if data.get("pipeCoefficient") not in (None, "")
                else global_coefficient
            )
        )
        tax_rate = Decimal(str(data.get("taxRate") or "0"))
    except (InvalidOperation, TypeError, ValueError):
        global_coefficient = equipment_coefficient = pipe_coefficient = Decimal("-1")
        tax_rate = Decimal("-1")
    if (
        global_coefficient <= 0
        or equipment_coefficient <= 0
        or pipe_coefficient <= 0
        or tax_rate < 0
        or tax_rate > 1
    ):
        exceptions.append(
            {
                "code": "COEFFICIENT_INVALID",
                "message": "系数必须大于 0，税率必须在 0 到 1 之间。",
                "severity": "blocks_pricing",
                "detail": {},
            }
        )
    else:
        review = coefficient_review_exception(
            global_coefficient=global_coefficient,
            equipment_coefficient=equipment_coefficient,
            pipe_coefficient=pipe_coefficient,
        )
        if review:
            exceptions.append(review)
    unique = {}
    for item in exceptions:
        key = (item.get("code"), item.get("message"))
        unique[key] = item
    return list(unique.values())


def _decimal_quantity(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"数量无效：{value!r}") from exc


def apply_source_multipliers(
    data: dict,
    multipliers: dict[str, Decimal | str | int],
    *,
    default_source_id: str = "",
) -> None:
    """按来源图纸的“代表层数 ×N”重算设备数量和管线长度。

    以首次建草稿时保存的 baseQuantity/baseLengthM 为基准，重复执行不会
    继续累乘；手工改过的数量会被重新覆盖，界面会提示。
    """
    normalized: dict[str, Decimal] = {}
    for key, value in multipliers.items():
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            number = Decimal("1")
        if number <= 0:
            number = Decimal("1")
        normalized[str(key)] = number
    for group in data.get("equipment", []):
        source = str(group.get("sourceInspectionId") or default_source_id or "")
        multiplier = normalized.get(source, Decimal("1"))
        base = group.get("baseQuantity")
        if base in (None, ""):
            base = group.get("quantity")
        try:
            base_value = Decimal(str(base))
        except (InvalidOperation, TypeError, ValueError):
            continue
        group["baseQuantity"] = str(base)
        group["quantity"] = str(base_value * multiplier)
        group["sourceMultiplier"] = str(multiplier)
    for group in data.get("routes", []):
        source = str(group.get("sourceInspectionId") or default_source_id or "")
        multiplier = normalized.get(source, Decimal("1"))
        base = group.get("baseLengthM")
        if base in (None, ""):
            base = group.get("lengthM")
        if base not in (None, ""):
            try:
                base_value = Decimal(str(base))
            except (InvalidOperation, TypeError, ValueError):
                base_value = None
            if base_value is not None:
                group["baseLengthM"] = str(base)
                group["lengthM"] = str(base_value * multiplier)
        raw = group.get("baseLengthRaw")
        if raw in (None, ""):
            raw = group.get("lengthRaw")
        if raw is not None:
            try:
                raw_value = Decimal(str(raw))
            except (InvalidOperation, TypeError, ValueError):
                raw_value = None
            if raw_value is not None:
                # CAD 原始长度保持不变；代表层数只派生有效量。
                group["baseLengthRaw"] = str(raw)
                group["lengthRaw"] = float(raw_value)
        group["sourceMultiplier"] = str(multiplier)
        known_base = group.get("baseLengthKnownM")
        if known_base in (None, ""):
            known_base = group.get("lengthKnownM")
        if known_base not in (None, ""):
            try:
                known_value = Decimal(str(known_base))
            except (InvalidOperation, TypeError, ValueError):
                known_value = None
            if known_value is not None:
                group["baseLengthKnownM"] = str(known_base)
                group["lengthKnownM"] = float(known_value * multiplier)
    data["sourceMultipliers"] = {
        key: str(value) for key, value in normalized.items()
    }


def blocking_exceptions(exceptions: list[dict] | tuple[dict, ...]) -> list[dict]:
    """返回仍需处理的阻塞项；客户已逐条核对确认的图纸疑点不再阻塞。"""
    return [
        item
        for item in exceptions
        if item.get("severity") == "blocks_pricing"
        and not item.get("acknowledged")
    ]


def apply_source_scales(
    data: dict,
    scales: dict[str, Decimal | str | int],
    *,
    default_source_id: str = "",
) -> None:
    """按客户确认的图面比例，从图面长度重算管线米数。

    lengthRaw 是 CAD 原始量，始终不变；代表层数只乘在有效量上。
    重算时同时更新 baseLengthM/baseLengthRaw/baseLengthKnownM，避免之后再
    应用代表层数或比例时重复放大。
    """
    applied: dict[str, str] = {}
    for group in data.get("routes", []):
        source = str(group.get("sourceInspectionId") or default_source_id or "")
        raw_scale = scales.get(source)
        if raw_scale in (None, ""):
            continue
        try:
            scale = Decimal(str(raw_scale))
            raw_text = group.get("baseLengthRaw")
            if raw_text in (None, ""):
                raw_text = group.get("lengthRaw")
            raw = Decimal(str(raw_text))
            multiplier = Decimal(str(group.get("sourceMultiplier") or "1"))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if not scale.is_finite() or scale <= 0 or raw <= 0:
            continue
        if multiplier <= 0:
            multiplier = Decimal("1")
        length_m = raw * scale * multiplier
        group["scale"] = str(scale)
        old_length = group.get("lengthM")
        try:
            old_value = Decimal(str(old_length))
            same_length = (
                old_length not in (None, "")
                and abs(old_value - length_m)
                <= max(Decimal("1e-9"), abs(old_value) * Decimal("1e-12"))
            )
        except (InvalidOperation, TypeError, ValueError):
            same_length = False
        if not same_length:
            group["lengthM"] = format(length_m.normalize(), "f")
        group["lengthRaw"] = float(raw)
        group["baseLengthM"] = format((raw * scale).normalize(), "f")
        group["baseLengthRaw"] = format(raw.normalize(), "f")
        known_raw = group.get("lengthKnownRaw")
        if known_raw is not None:
            try:
                known_value = Decimal(str(known_raw))
            except (InvalidOperation, TypeError, ValueError):
                known_value = None
            if known_value is not None:
                known_base = known_value * scale
                group["baseLengthKnownM"] = format(
                    known_base.normalize(), "f"
                )
                group["lengthKnownM"] = float(known_base * multiplier)
        applied[source] = str(scale)
    if applied:
        stored = dict(data.get("sourceScales") or {})
        stored.update(applied)
        data["sourceScales"] = stored


def _authoritative_route_error(
    data: dict,
    authoritative_routes: dict[str, dict] | None,
) -> str | None:
    for group in data.get("routes") or []:
        if not group.get("include"):
            continue
        if group.get("origin") == "customer_input":
            if not group.get("manualRecordId"):
                return "人工补录缺少服务器记录，不能直接生成。"
            continue
        if authoritative_routes is None:
            return "缺少服务端权威图纸检查快照，不能生成含 CAD 管线的报价。"
        authoritative = authoritative_routes.get(str(group.get("key") or ""))
        if authoritative is None:
            return "管线组不在当前服务端检查快照中，请重新检查图纸。"
        for field in (
            "routeEvidenceKey",
            "medium",
            "spec",
            "sourceRegionKey",
            "lengthRaw",
        ):
            if group.get(field) != authoritative.get(field):
                return "管线组的权威证据与当前草稿不一致，请重新检查并核对。"
        if (
            group.get("eligibility") == "eligible"
            and authoritative.get("eligibility") != "eligible"
        ) or (
            group.get("autoInclude") is True
            and authoritative.get("autoInclude") is not True
        ):
            return "管线组的采用状态不能高于权威检查快照，请重新核对。"
        if sorted(group.get("reviewReasons") or []) != sorted(
            authoritative.get("reviewReasons") or []
        ):
            return "管线组的审查原因与权威快照不一致，请重新检查并核对。"
        if sorted(
            str(item.get("physicalSegmentId"))
            for item in group.get("segments") or []
            if item.get("physicalSegmentId")
        ) != sorted(
            str(item.get("physicalSegmentId"))
            for item in authoritative.get("segments") or []
            if item.get("physicalSegmentId")
        ):
            return "管线组的物理段与权威快照不一致，请重新检查并核对。"
        if sorted(
            str(item) for item in group.get("sourceHandles") or []
        ) != sorted(
            str(item) for item in authoritative.get("handles") or []
        ):
            return "管线组的来源实体与权威快照不一致，请重新检查并核对。"
    return None


def generate_generic_quote(
    *,
    project_name: str,
    draft: dict,
    template_path: Path,
    out_dir: Path,
    price_book: dict,
    price_overrides: dict[int, tuple[Decimal, Decimal]] | None = None,
    authoritative_routes: dict[str, dict] | None = None,
    manual_route_records: dict[str, dict] | None = None,
    project_id: str = "",
) -> RunResult:
    rows = list((price_book.get("book") or {}).get("rows") or [])
    by_row = {int(row["rowNumber"]): row for row in rows}
    data = reconcile_equipment_sets(
        dict(draft.get("data") or {}), by_row
    )
    for group in data.get("routes") or []:
        if not group.get("include"):
            continue
        adoption_error = route_adoption_error(group)
        if adoption_error:
            raise ValueError(
                f"管线组 {group.get('key')}：{adoption_error}"
            )
    proposal_error = _current_proposal_error(data)
    if proposal_error:
        raise ValueError(proposal_error)
    manual_error = _manual_route_error(
        data, manual_route_records, project_id
    )
    if manual_error:
        raise ValueError(manual_error)
    authority_error = _authoritative_route_error(
        data, authoritative_routes
    )
    if authority_error:
        raise ValueError(authority_error)
    items: list[QuantityItem] = []
    quantities: dict[int, Decimal] = {}
    omitted: list[OmittedItem] = []
    custom_items_excel: list[dict] = []

    for index, group in enumerate(data.get("equipment", []), start=1):
        if group.get("isComponent") or group.get("isComponentManual"):
            continue
        if not group.get("include"):
            continue
        row = by_row.get(int(group.get("priceRow") or 0))
        manual_price = _manual_price(group)
        project_price = None
        if row is not None and row.get("projectPrice"):
            project_price = (
                Decimal(str(row.get("material") or "0")),
                Decimal(str(row.get("installation") or "0")),
            )
        quantity = _decimal_quantity(group.get("quantity"))
        if quantity <= 0:
            raise ValueError(f"设备组 {group.get('key')} 数量必须大于 0")
        if manual_price is None and project_price is None and (
            row is None or row.get("priceState") != "priced"
        ):
            raise ValueError(f"设备组 {group.get('key')} 的模板行没有可用价格")
        if (
            (group.get("procurementModelPending") or group.get("modelSource") == "block")
            and (row is None or row.get("priceState") != "priced")
            and not str(group.get("manualLabel") or "").strip()
        ):
            raise ValueError(
                f"设备组 {group.get('key')} 的采购型号待核；"
                "请填写人工确认名称/型号，或选择已确认型号的模板价格行。"
            )
        unit_prices = manual_price or project_price
        price_source = (
            "customer_manual"
            if manual_price is not None
            else "project_price"
            if project_price is not None
            else None
        )
        equipment_spec = str(
            (row or {}).get("spec")
            or group.get("manualLabel")
            or group.get("model")
            or ""
        )
        display_name = " ".join(
            part
            for part in (
                group.get("floorLabel")
                if group.get("floorLabel") not in (None, "", "model")
                else "",
                str(
                    group.get("manualLabel")
                    or (row or {}).get("name")
                    or group.get("label")
                    or group.get("model")
                    or ""
                ),
            )
            if part
        )
        items.append(
            QuantityItem(
                item_id=f"custom-equipment-{index}",
                category="equipment",
                spec=equipment_spec,
                unit=str(
                    group.get("manualUnit")
                    or (row or {}).get("unit")
                    or "台"
                ),
                quantity=quantity,
                template_row=int(row["rowNumber"]) if row and project_price is None else None,
                unit_prices=unit_prices,
                price_source=price_source,
                display_name=display_name,
                display_spec=equipment_spec,
                evidence=[
                    Evidence(
                        kind="customer_input",
                        handle=str(group.get("key")),
                        note=(
                            "水系统等模板未覆盖项，客户提供人工单价"
                            if manual_price is not None
                            else "项目价行，按本次项目确认价"
                            if project_price is not None
                            else "新图纸候选设备，经人工确认"
                        ),
                    )
                ],
            )
        )
        if unit_prices is not None:
            custom_items_excel.append(
                {
                    "name": display_name,
                    "spec": equipment_spec,
                    "unit": str(
                        group.get("manualUnit")
                        or (row or {}).get("unit")
                        or group.get("unit")
                        or "台"
                    ),
                    "quantity": quantity,
                    "material": unit_prices[0],
                    "installation": unit_prices[1],
                    "category": "equipment",
                    "floor": str(group.get("floorLabel") or "人工补价"),
                }
            )
        else:
            quantities[int(row["rowNumber"])] = (
                quantities.get(int(row["rowNumber"]), Decimal("0")) + quantity
            )

    included_route_rows = {
        int(group["priceRow"])
        for group in data.get("routes", [])
        if group.get("include") and group.get("priceRow")
    }
    linked_rows: set[int] = set()
    source_rows_by_key: dict[str, int] = {}
    for index, group in enumerate(data.get("routes", []), start=1):
        if not group.get("include"):
            continue
        adoption_error = route_adoption_error(group)
        if adoption_error:
            raise ValueError(
                f"管线组 {group.get('key')}：{adoption_error}"
            )
        row = by_row.get(int(group.get("priceRow") or 0))
        if row is not None:
            source_rows_by_key[str(group.get("key"))] = int(row["rowNumber"])
        manual_price = _manual_price(group)
        project_price = None
        if row is not None and row.get("projectPrice"):
            project_price = (
                Decimal(str(row.get("material") or "0")),
                Decimal(str(row.get("installation") or "0")),
            )
        quantity = _decimal_quantity(group.get("lengthM"))
        if quantity <= 0:
            raise ValueError(f"管线组 {group.get('key')} 长度必须大于 0")
        if manual_price is None and project_price is None and (
            row is None or row.get("priceState") != "priced"
        ):
            raise ValueError(f"管线组 {group.get('key')} 的模板行没有可用价格")
        if manual_price is None and row is not None:
            incompatibility = route_row_error(group, row)
            if incompatibility:
                raise ValueError(
                    f"管线组 {group.get('key')}：{incompatibility}；"
                    "请改选同系统同材质的模板行，或填写客户人工价"
                )
        unit_prices = manual_price or project_price
        item_id = f"custom-route-{index}"
        route_spec = str(
            group.get("manualSpec")
            or (row or {}).get("spec")
            or group.get("spec")
            or group.get("label")
            or ""
        )
        display_name = " ".join(
            part
            for part in (
                group.get("floorLabel")
                if group.get("floorLabel") not in (None, "", "model")
                else "",
                str(
                    (row or {}).get("name")
                    or group.get("label")
                    or group.get("category")
                    or ""
                ),
            )
            if part
        )
        items.append(
            QuantityItem(
                item_id=item_id,
                category="pipe",
                spec=route_spec,
                unit=str(
                    group.get("manualUnit")
                    or (row or {}).get("unit")
                    or "米"
                ),
                quantity=quantity,
                template_row=int(row["rowNumber"]) if row and project_price is None else None,
                unit_prices=unit_prices,
                price_source=(
                    "customer_manual"
                    if manual_price is not None
                    else "project_price"
                    if project_price is not None
                    else None
                ),
                display_name=display_name,
                display_spec=route_spec,
                evidence=[
                    Evidence(
                        kind="customer_input",
                        handle=str(group.get("key")),
                        note=(
                            "水系统等模板未覆盖项，客户提供人工单价"
                            if manual_price is not None
                            else "项目价行，按本次项目确认价"
                            if project_price is not None
                            else "新图纸候选管线，经人工确认"
                        ),
                    )
                ],
            )
        )
        if unit_prices is not None:
            custom_items_excel.append(
                {
                    "name": display_name,
                    "spec": route_spec,
                    "unit": str(group.get("manualUnit") or "米"),
                    "quantity": quantity,
                    "material": unit_prices[0],
                    "installation": unit_prices[1],
                    "category": "pipe",
                    "floor": str(group.get("floorLabel") or "人工补价"),
                }
            )
            omitted.append(
                OmittedItem(
                    subject=(
                        f"人工/项目价管线“{group.get('key')}”的对应保温未自动拆分；"
                        "如保温另计，请补充保温项目。"
                    ),
                    reason="manual_price_insulation_not_split",
                    evidence=[],
                )
            )
            continue
        row_number = int(row["rowNumber"])
        quantities[row_number] = (
            quantities.get(row_number, Decimal("0")) + quantity
        )
        linked = group.get("linkedMaterial") or {}
        linked_row_number = int(linked.get("priceRow") or 0)
        linked_unit = str(linked.get("unit") or "米")
        if linked_row_number and linked_unit in {"米", "m", "M"}:
            if linked_row_number in included_route_rows:
                omitted.append(
                    OmittedItem(
                        subject=(
                            "对应保温已由图中管线组计入："
                            f"{linked.get('priceLabel') or linked_row_number}"
                        ),
                        reason="linked_material_already_counted",
                        evidence=[],
                    )
                )
            else:
                linked_row = by_row.get(linked_row_number)
                if linked_row is not None and linked_row.get("priceState") == "priced":
                    existing_linked = next(
                        (
                            item
                            for item in items
                            if item.template_row == linked_row_number
                            and item.item_id.startswith("custom-linked-")
                        ),
                        None,
                    )
                    if existing_linked is not None:
                        existing_linked.quantity += quantity
                    else:
                        items.append(
                            QuantityItem(
                                item_id=f"custom-linked-{index}",
                                category="pipe",
                                spec=str(linked_row.get("spec") or linked.get("spec") or ""),
                                unit=str(linked_row.get("unit") or "米"),
                                quantity=quantity,
                                template_row=linked_row_number,
                                derived_from=str(group.get("key")),
                                display_name=" ".join(
                                    part
                                    for part in (
                                        group.get("floorLabel")
                                        if group.get("floorLabel") not in (None, "", "model")
                                        else "",
                                        str(
                                            linked_row.get("name")
                                            or linked.get("name")
                                            or "对应保温"
                                        ),
                                    )
                                    if part
                                ),
                                display_spec=str(
                                    linked_row.get("spec") or linked.get("spec") or ""
                                ),
                                evidence=[
                                    Evidence(
                                        kind="rule",
                                        handle=str(group.get("key")),
                                        note="按已确认规则与对应管材等长联动",
                                    )
                                ],
                            )
                        )
                    quantities[linked_row_number] = (
                        quantities.get(linked_row_number, Decimal("0")) + quantity
                    )
                    linked_rows.add(linked_row_number)
                else:
                    omitted.append(
                        OmittedItem(
                            subject=(
                                f"对应保温未计价（模板行 {linked_row_number} 不可用）："
                                f"{group.get('key')}"
                            ),
                            reason="linked_insulation_price_missing",
                            evidence=[],
                        )
                    )
        elif group.get("linkedMaterialMissing"):
            omitted.append(
                OmittedItem(
                    subject=f"对应保温未计价（模板缺少匹配行）：{group.get('key')}",
                    reason="linked_insulation_price_missing",
                    evidence=[],
                )
            )

    for index, item in enumerate(data.get("customItems") or [], start=1):
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError(f"补充项目 {index} 缺少名称")
        quantity = _decimal_quantity(item.get("quantity"))
        if quantity <= 0:
            raise ValueError(f"补充项目“{name}”数量必须大于 0")
        try:
            material = Decimal(str(item.get("material") or "0"))
            installation = Decimal(str(item.get("installation") or "0"))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"补充项目“{name}”价格无效") from exc
        if material < 0 or installation < 0:
            raise ValueError(f"补充项目“{name}”价格不能为负数")
        category = str(item.get("category") or "pipe")
        if category not in {"equipment", "pipe"}:
            category = "pipe"
        spec = str(item.get("spec") or "")
        unit = str(item.get("unit") or ("台" if category == "equipment" else "米"))
        floor = str(item.get("floor") or "补充项目")
        items.append(
            QuantityItem(
                item_id=f"custom-extra-{index}",
                category=category,
                spec=spec,
                unit=unit,
                quantity=quantity,
                unit_prices=(material, installation),
                price_source="customer_input",
                display_name=f"{floor} {name}".strip(),
                display_spec=spec,
                evidence=[
                    Evidence(
                        kind="customer_input",
                        handle=f"custom-extra-{index}",
                        note="模板缺项，客户确认补入本次报价",
                    )
                ],
            )
        )
        custom_items_excel.append(
            {
                "name": name,
                "spec": spec,
                "unit": unit,
                "quantity": quantity,
                "material": material,
                "installation": installation,
                "category": category,
                "floor": floor,
            }
        )

    if not items:
        raise ValueError("没有可计价的设备或管线；请至少勾选一项并确认模板行。")
    omitted.append(
        OmittedItem(
            subject="未画出的竖向段、设备接管、预留和损耗",
            reason="unmeasured_vertical",
            evidence=[],
        )
    )
    for group in data.get("equipment", []):
        # 成套成员已包含在套价内，不能重复列为“未计入”；
        # 只有成套自身被排除时才在下面按套列出。
        if group.get("isComponent") or group.get("componentOfSet"):
            continue
        if not group.get("include"):
            item = _excluded_group_omission(
                group, "equipment", price_rows=by_row
            )
            if group.get("isSet") and "整套未计入" not in item.subject:
                item = OmittedItem(
                    subject=item.subject + "（整套未计入）",
                    reason=item.reason,
                    evidence=[],
                )
            omitted.append(item)
    for group in data.get("routes", []):
        if not group.get("include"):
            omitted.append(
                _excluded_group_omission(group, "route", price_rows=by_row)
            )

    prices = {
        row_number: (
            Decimal(str(row["material"])),
            Decimal(str(row["installation"])),
        )
        for row_number, row in by_row.items()
        if row.get("priceState") == "priced"
        and row.get("material") is not None
        and row.get("installation") is not None
    }
    for row_number, override in (price_overrides or {}).items():
        if int(row_number) in prices:
            prices[int(row_number)] = override
    try:
        global_coefficient = Decimal(str(data.get("globalCoefficient") or "1"))
        equipment_coefficient = Decimal(
            str(
                data.get("equipmentCoefficient")
                if data.get("equipmentCoefficient") not in (None, "")
                else global_coefficient
            )
        )
        pipe_coefficient = Decimal(
            str(
                data.get("pipeCoefficient")
                if data.get("pipeCoefficient") not in (None, "")
                else global_coefficient
            )
        )
        tax_rate = Decimal(str(data.get("taxRate") or "0"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("系数或税率不是有效数字") from exc
    if global_coefficient <= 0 or equipment_coefficient <= 0 or pipe_coefficient <= 0:
        raise ValueError("系数必须大于 0")
    if tax_rate < 0 or tax_rate > 1:
        raise ValueError("税率必须在 0 到 1 之间")
    tax_basis = str(data.get("taxBasis") or "subtotal")
    if tax_basis not in {"subtotal", "installation_and_auxiliary"}:
        raise ValueError("计税基数口径无效")
    pricing = price_items(
        items,
        prices,
        tax_rate=tax_rate,
        global_coefficient=global_coefficient,
        category_coefficients={
            "equipment": equipment_coefficient,
            "pipe": pipe_coefficient,
        },
        tax_basis=tax_basis,
    )
    uppercase = upper_amount(pricing.total)
    row_labels = {int(row["rowNumber"]): row for row in rows}
    delivery_items = {
        item.item_id: {
            "category": item.category,
            "spec": item.spec,
            "unit": item.unit,
            "displayName": item.display_name,
            "displaySpec": item.display_spec,
        }
        for item in items
    }
    delivery_groups = build_groups(
        [
            {
                "itemId": line.item_id,
                "templateRow": line.template_row,
                "quantity": str(line.quantity),
                "materialUnitPrice": str(line.material_unit_price),
                "installationUnitPrice": str(line.installation_unit_price),
                "baseAmount": str(line.base_amount),
            }
            for line in pricing.lines
            if line.bucket != "equipment"
        ],
        delivery_items,
        row_labels,
    )
    acknowledged = [
        item
        for item in data.get("issues") or []
        if item.get("acknowledged")
    ]
    equipment_included = [
        group for group in data.get("equipment", []) if group.get("include")
    ]
    routes_included = [
        group for group in data.get("routes", []) if group.get("include")
    ]
    scope_summary = {
        "project": project_name,
        "title": str(data.get("title") or ""),
        "generatedAt": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "stage": (
            "阶段测算（已核对部分图纸疑点，未画部分不计入）"
            if acknowledged
            else "阶段测算（候选经人工确认，未画部分不计入）"
        ),
        "drawings": "、".join(
            sorted(
                {
                    str(group.get("sourceFileName") or "")
                    for group in [*data.get("equipment", []), *data.get("routes", [])]
                    if group.get("sourceFileName")
                }
            )
        ),
        "template": (
            f"{price_book.get('name') or ''}"
            f"{('（' + str(price_book.get('fileName')) + '）') if price_book.get('fileName') else ''}"
        ),
        "coefficients": (
            f"设备 {equipment_coefficient}、管线 {pipe_coefficient}、"
            f"总体 {global_coefficient}"
        ),
        "taxBasis": (
            "安装费与辅材（不含主设备材料）"
            if tax_basis == "installation_and_auxiliary"
            else "整单未税金额"
        ),
        "taxRate": format((tax_rate * 100).normalize(), "f") + "%",
        "equipment": (
            f"{len(equipment_included)} 组 / "
            f"{sum(int(float(group.get('quantity') or 0)) for group in equipment_included)} 台（套）"
            if equipment_included
            else ""
        ),
        "routes": (
            f"{len(routes_included)} 组 / "
            f"{sum(float(group.get('lengthM') or 0) for group in routes_included):.3f} 米"
            if routes_included
            else ""
        ),
        "acknowledged": (
            f"{len(acknowledged)} 项（按现有识别结果继续，缺失部分未计入）"
            if acknowledged
            else ""
        ),
        "omitted": [item.subject for item in omitted],
    }
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    working = export_workbook(
        out_dir / "quote-working.xlsx", project_name=project_name,
        pricing=pricing, items=items, labels=row_labels, tax_rate=tax_rate,
        scope=scope_summary,
    )
    recalc = recalc_workbook(working, out_dir)
    excel_subtotal_exact = recalc.values["subtotal"]
    excel_total = round_money(recalc.values["total"])
    excel_subtotal = round_money(excel_subtotal_exact)
    excel_tax = excel_total - excel_subtotal
    if excel_total != pricing.total:
        raise ValueError(
            "Excel 重算总额与页面预览不一致："
            f"Excel {excel_total}，预览 {pricing.total}；请检查模板公式。"
        )
    expected_upper = uppercase
    actual_upper = recalc.texts.get("uppercase")
    if actual_upper and actual_upper != expected_upper:
        raise ValueError(
            "Excel 重算大写金额与最终总额不一致："
            f"Excel“{actual_upper}”，应为“{expected_upper}”；"
            "请检查 _uppercase 工作表公式。"
        )
    if excel_subtotal != pricing.subtotal:
        raise ValueError(
            "Excel 重算未税合计与页面预览不一致："
            f"Excel {excel_subtotal}，预览 {pricing.subtotal}；请检查模板公式。"
        )
    labels = {
        int(row["rowNumber"]): row
        for row in rows
    }
    equipment_rows = []
    pipe_items = {
        item.item_id: {
            "category": item.category,
            "spec": item.spec,
            "unit": item.unit,
            "displayName": item.display_name,
            "displaySpec": item.display_spec,
        }
        for item in items
    }
    for line in pricing.lines:
        if line.bucket != "equipment":
            continue
        row = labels.get(int(line.template_row or 0), {})
        equipment_rows.append(
            QuotePdfRow(
                index="",
                name=str(row.get("name") or line.display_name or ""),
                spec=str(row.get("spec") or line.display_spec or ""),
                unit=str(row.get("unit") or ""),
                quantity=format_quantity(line.quantity),
                material_unit_price=f"{line.material_unit_price:,.2f}",
                installation_unit_price=f"{line.installation_unit_price:,.2f}",
                material_amount=f"{line.material_amount:,.2f}",
                installation_amount=f"{line.installation_amount:,.2f}",
                total_amount=f"{line.base_amount:,.2f}",
            )
        )
    pipe_groups = build_groups(
        [
            {
                "itemId": line.item_id,
                "templateRow": line.template_row,
                "quantity": str(line.quantity),
                "materialUnitPrice": str(line.material_unit_price),
                "installationUnitPrice": str(line.installation_unit_price),
                "baseAmount": str(line.base_amount),
            }
            for line in pricing.lines
            if line.bucket != "equipment"
        ],
        pipe_items,
        labels,
    )
    pipe_sections = []
    for group in pipe_groups:
        group_rows = []
        for row in group["rows"]:
            group_rows.append(
                QuotePdfRow(
                    index="",
                    name=str(row["name"]),
                    spec=str(row["spec"] or ""),
                    unit=str(row["unit"] or ""),
                    quantity=format_quantity(row["quantity"]),
                    material_unit_price=f"{Decimal(str(row['materialUnitPrice'] or 0)):,.2f}",
                    installation_unit_price=f"{Decimal(str(row['installationUnitPrice'] or 0)):,.2f}",
                    material_amount="",
                    installation_amount="",
                    total_amount=f"{Decimal(str(row['amount'])):,.2f}",
                )
            )
        if group_rows:
            pipe_sections.append((group["title"], group_rows))
    counter = 1
    for position, row in enumerate(equipment_rows):
        equipment_rows[position] = replace(row, index=str(counter))
        counter += 1
    numbered_sections = []
    for title, rows in pipe_sections:
        numbered = []
        for row in rows:
            numbered.append(replace(row, index=str(counter)))
            counter += 1
        numbered_sections.append((title, numbered))
    sections = []
    if equipment_rows:
        sections.append(QuotePdfSection("设备与安装", tuple(equipment_rows)))
    for title, rows in numbered_sections:
        sections.append(QuotePdfSection(title, tuple(rows)))
    caveats = [
        "新图纸的候选设备/管线来自图块、图层和可量取几何，未逐项人工重数。",
        "客户确认的模板行与数量是本次报价依据；母模板未被修改。",
    ]
    acknowledged_issues = [
        item
        for item in data.get("issues") or []
        if item.get("acknowledged")
    ]
    if acknowledged_issues:
        caveats.append(
            f"客户已逐条核对并确认 {len(acknowledged_issues)} 项图纸疑点；"
            "未识别、未支持或未补录的部分未自动计入本次报价。"
        )
    if custom_items_excel:
        caveats.append(
            "补充项目由客户确认名称、数量、单位和价格；未写回母模板。"
        )
    if any(group.get("linkedMaterial") for group in data.get("routes", [])):
        caveats.append(
            "铜管/排水对应的保温按已确认规则等长联动；"
            "缺少匹配保温行时明确列为未计价。"
        )
    template_notes = ()
    template_title = "暖通工程报价表"
    pdf_data = QuotePdfData(
        title=project_name,
        project=f"项目报价单 · {template_title}",
        stage_note=(
            "新图纸报价：候选设备/管线经人工确认；"
            "未画出的竖向段、接管、预留和损耗不计入。"
        ),
        sections=tuple(sections),
        subtotal=f"{excel_subtotal:,.2f}",
        tax=f"{excel_tax:,.2f}",
        total=f"{excel_total:,.2f}",
        omitted=tuple(item.subject for item in omitted),
        notes=template_notes,
        caveats=tuple(caveats),
        tax_label=f"税额（{format((tax_rate * 100).normalize(), 'f')}%）",
        uppercase=uppercase,
        tax_basis_note=(
            "安装费与辅材（不含主设备材料）"
            if tax_basis == "installation_and_auxiliary"
            else "整单未税金额"
        ),
    )
    pdf_path = render_quote_pdf(out_dir / "quote.pdf", pdf_data)
    excel_path = out_dir / "quote.xlsx"
    shutil.copyfile(recalc.workbook_path, excel_path)
    quote_json = out_dir / "quote.json"
    takeoff = TakeoffResult(
        source_sha256=str((price_book.get("book") or {}).get("sourceSha256") or ""),
        drawing_path=str(data.get("drawingName") or ""),
        items=items,
        omitted=omitted,
        issues=[],
        evidence={"draft": data, "priceBookId": price_book.get("id")},
    )
    payload = {
        "schemaVersion": "0.1",
        "scope": "custom-drawing",
        "status": "stage_review_ready_with_caveats",
        "statusNote": "新图纸候选经人工确认；未画出的部分未计入",
        "project": project_name,
        "source": {
            "drawing": data.get("drawingName"),
            "template": str(template_path),
            "priceBookId": price_book.get("id"),
        },
        "templateLabels": {
            str(number): {
                "index": row.get("index"),
                "name": row.get("name") or "",
                "spec": row.get("spec") or "",
                "unit": row.get("unit") or "",
            }
            for number, row in by_row.items()
            if any(item.template_row == number for item in items)
        },
        "items": [
            {
                "itemId": item.item_id,
                "category": item.category,
                "spec": item.spec,
                "unit": item.unit,
                "quantity": str(item.quantity),
                "templateRow": item.template_row,
                "displayName": item.display_name,
                "displaySpec": item.display_spec,
            }
            for item in items
        ],
        "scopeSummary": scope_summary,
        "omitted": [
            {"subject": item.subject, "reason": item.reason}
            for item in omitted
        ],
        "priceLines": [
            {
                "itemId": line.item_id,
                "templateRow": line.template_row,
                "quantity": str(line.quantity),
                "materialUnitPrice": str(line.material_unit_price),
                "installationUnitPrice": str(line.installation_unit_price),
                "materialAmount": str(line.material_amount),
                "installationAmount": str(line.installation_amount),
                "baseAmount": str(line.base_amount),
                "displayName": line.display_name,
                "displaySpec": line.display_spec,
            }
            for line in pricing.lines
        ],
        "totals": {
            "subtotal": str(excel_subtotal),
            "tax": str(excel_tax),
            "total": str(excel_total),
            "subtotalExact": str(excel_subtotal_exact),
            "taxExact": str(pricing.tax_exact),
            "uppercase": uppercase,
        },
        "pricingSettings": {
            "globalCoefficient": str(global_coefficient),
            "equipmentCoefficient": str(equipment_coefficient),
            "pipeCoefficient": str(pipe_coefficient),
            "taxRate": str(tax_rate),
            "taxBasis": tax_basis,
            "taxBaseAmount": str(pricing.tax_base_exact),
            "subtotalBeforeTax": str(pricing.subtotal_exact),
        },
        "customItems": [
            {
                **item,
                "quantity": str(item["quantity"]),
                "material": str(item["material"]),
                "installation": str(item["installation"]),
            }
            for item in custom_items_excel
        ],
        "draft": data,
        "excel": {cell: str(value) for cell, value in recalc.values.items()},
    }
    quote_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return RunResult(
        pricing=pricing,
        recalc=recalc,
        takeoff=takeoff,
        quote_json=quote_json,
        excel_path=excel_path,
        pdf_path=pdf_path,
    )


__all__ = ["build_draft_data", "build_multi_draft_data", "generate_generic_quote"]
