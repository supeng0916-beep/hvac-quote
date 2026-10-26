"""客户报价模板导入：读取行、单位、材料价和安装价。

只提取事实，不修改母模板。公式价若没有缓存值，会标为待重算，不猜金额。
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter

EQUIPMENT_MARKERS = (
    "室内机",
    "室外机",
    "挂机",
    "柜机",
    "天花",
    "风机盘管",
    "多联机",
    "设备",
    "水泵",
    "阀门",
    "冷却塔",
    "水处理",
    "膨胀水箱",
    "分水器",
    "集水器",
    "冷水机组",
    "模块机",
    "主机",
    "新风",
    "MDV",
    "KFR",
    "FP-",
)
PIPE_MARKERS = (
    "铜管",
    "冷媒",
    "气管",
    "液管",
    "保温",
    "排水",
    "PVC",
    "水管",
    "冷凝",
    "冷冻水",
    "冷却水",
    "补水",
    "镀锌钢管",
    "钢管",
    "风管",
    "风口",
    "支吊架",
    "调试",
    "运输",
)
WATER_MARKERS = (
    "水系统",
    "水泵",
    "阀门",
    "冷却塔",
    "水处理",
    "膨胀水箱",
    "分水器",
    "集水器",
    "冷水机组",
    "冷冻水",
    "冷却水",
    "补水",
    "镀锌钢管",
    "无缝钢管",
    "焊接钢管",
    "空冷",
)
REFRIGERANT_MARKERS = (
    "冷媒",
    "铜管",
    "气管",
    "液管",
    "多联机",
    "室内机",
    "室外机",
    "挂机",
    "柜机",
)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _number(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return format(Decimal(str(value)), "f")
        except InvalidOperation:
            return None
    text = str(value).strip()
    if not text or text.startswith("="):
        return None
    try:
        return format(Decimal(text), "f")
    except InvalidOperation:
        return None


def _category(name: str, spec: str) -> str:
    haystack = f"{name} {spec}".upper()
    if any(marker.upper() in haystack for marker in EQUIPMENT_MARKERS):
        return "equipment"
    if any(marker.upper() in haystack for marker in PIPE_MARKERS):
        return "pipe"
    return "other"


def _system(name: str, spec: str) -> str:
    haystack = f"{name} {spec}".upper()
    if any(marker.upper() in haystack for marker in WATER_MARKERS):
        return "water"
    if any(marker.upper() in haystack for marker in REFRIGERANT_MARKERS):
        return "refrigerant"
    return "general"


def import_price_book(
    path: Path,
    *,
    file_id: str,
    source_sha256: str,
) -> dict:
    formula_book = load_workbook(path, data_only=False, read_only=True)
    value_book = load_workbook(path, data_only=True, read_only=True)
    try:
        sheet_name = formula_book.sheetnames[0]
        formula_sheet = formula_book[sheet_name]
        value_sheet = value_book[sheet_name]
        rows: list[dict] = []
        warnings: list[str] = []
        for row_number in range(2, formula_sheet.max_row + 1):
            index = _text(formula_sheet.cell(row_number, 1).value)
            name = _text(formula_sheet.cell(row_number, 2).value)
            spec = _text(formula_sheet.cell(row_number, 3).value)
            unit = _text(formula_sheet.cell(row_number, 4).value)
            material_formula = formula_sheet.cell(row_number, 7).value
            install_formula = formula_sheet.cell(row_number, 8).value
            material = _number(value_sheet.cell(row_number, 7).value)
            install = _number(value_sheet.cell(row_number, 8).value)
            if not (name or spec):
                continue
            if name in {"名称", "序号"} or spec in {"规格及型号", "规格型号"}:
                continue
            material_formula_flag = (
                isinstance(material_formula, str)
                and material_formula.startswith("=")
            )
            install_formula_flag = (
                isinstance(install_formula, str)
                and install_formula.startswith("=")
            )
            if (
                material is None
                and install is None
                and not material_formula_flag
                and not install_formula_flag
            ):
                continue
            price_state = (
                "priced"
                if material is not None and install is not None
                else "incomplete"
            )
            if material_formula_flag and material is None:
                price_state = "formula_unresolved"
            if install_formula_flag and install is None:
                price_state = "formula_unresolved"
            if price_state == "formula_unresolved":
                warnings.append(
                    f"第 {row_number} 行材料/安装价是公式且没有缓存值，需重算后再用于计价。"
                )
            elif price_state == "incomplete":
                warnings.append(
                    f"第 {row_number} 行缺少材料价或安装价，未进入可选模板行。"
                )
            rows.append(
                {
                    "rowNumber": row_number,
                    "index": index,
                    "name": name,
                    "spec": spec,
                    "unit": unit or "项",
                    "material": material,
                    "installation": install,
                    "category": _category(name, spec),
                    "system": _system(name, spec),
                    "priceState": price_state,
                }
            )
        if not rows:
            warnings.append("没有读到可用的报价行；请确认模板列：B 名称、C 型号、D 单位、G 材料价、H 安装价。")
        return {
            "schemaVersion": "price-book-1",
            "fileId": file_id,
            "sourceSha256": source_sha256,
            "sheetName": sheet_name,
            "columns": {
                "name": "B",
                "spec": "C",
                "unit": "D",
                "material": "G",
                "installation": "H",
            },
            "rows": rows,
            "warnings": warnings,
            "counts": {
                "rows": len(rows),
                "priced": sum(1 for row in rows if row["priceState"] == "priced"),
                "equipment": sum(1 for row in rows if row["category"] == "equipment"),
                "pipe": sum(1 for row in rows if row["category"] == "pipe"),
            },
        }
    finally:
        formula_book.close()
        value_book.close()


# ---- 自动识别真实报价表的工作表和列 -----------------------------------------

_HEADER_ALIASES = {
    "name": ("名称", "设备名称", "材料名称", "设备及材料名称"),
    "spec": ("规格及型号", "规格型号", "型号/规格", "规格", "型号"),
    "unit": ("单位",),
    "quantity": ("工程量", "数量"),
    "material": ("材料单价/元", "材料单价", "材料价"),
    "installation": ("安装费单价/元", "安装费单价", "安装单价", "安装费"),
}


def _normalize_header(value: Any) -> str:
    text = _text(value).replace(" ", "").replace("\n", "").replace("/", "")
    return text.replace("（", "(").replace("）", ")")


def _header_key(text: str) -> str | None:
    if "项目名称" in text:
        return None
    for key in ("installation", "material", "quantity", "spec", "unit", "name"):
        for alias in _HEADER_ALIASES[key]:
            normalized = _normalize_header(alias)
            if normalized == text:
                return key
    # “材料单价/元”“安装费单价/元”这类带单位的表头
    if "安装费单价" in text or "安装单价" in text:
        return "installation"
    if "材料单价" in text or "材料价" in text:
        return "material"
    if "工程量" in text or "数量" in text:
        return "quantity"
    if "规格" in text or "型号" in text:
        return "spec"
    if text == "单位":
        return "unit"
    if "名称" in text:
        return "name"
    return None


def _detect_table(sheet) -> tuple[int, dict[str, int]] | None:
    best: tuple[int, dict[str, int]] | None = None
    max_column = min(sheet.max_column, 40)
    for header_row in range(1, min(sheet.max_row, 15) + 1):
        mapping: dict[str, int] = {}
        for column in range(1, max_column + 1):
            key = _header_key(_normalize_header(sheet.cell(header_row, column).value))
            if key and key not in mapping:
                mapping[key] = column
        if {"name", "spec", "unit", "material"} <= set(mapping):
            if best is None or header_row > best[0]:
                best = (header_row, mapping)
    return best


def _mapped_rows(
    formula_sheet,
    value_sheet,
    mapping: dict[str, int],
    header_row: int,
) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    warnings: list[str] = []
    assumed_install_zero = 0
    assumed_material_zero = 0
    start_row = header_row + 1
    for row_number in range(start_row, formula_sheet.max_row + 1):
        name = _text(formula_sheet.cell(row_number, mapping["name"]).value)
        if not name or name in {"名称", "序号"}:
            continue
        if any(marker in name for marker in ("小计", "合计", "总计")):
            continue
        spec = _text(formula_sheet.cell(row_number, mapping["spec"]).value)
        unit = _text(formula_sheet.cell(row_number, mapping["unit"]).value)
        material_column = mapping["material"]
        install_column = mapping.get("installation")
        material_formula = value_sheet.cell(row_number, material_column).value
        formula_material = formula_sheet.cell(row_number, material_column).value
        material = _number(material_formula)
        install = None
        install_formula = None
        if install_column:
            install_formula = formula_sheet.cell(row_number, install_column).value
            install = _number(value_sheet.cell(row_number, install_column).value)
        material_is_formula = (
            isinstance(formula_material, str) and formula_material.startswith("=")
        )
        install_is_formula = (
            isinstance(install_formula, str) and install_formula.startswith("=")
        )
        if material is None and install is None and not (
            material_is_formula or install_is_formula
        ):
            continue
        price_state = "priced"
        if material_is_formula and material is None:
            price_state = "formula_unresolved"
        if install_is_formula and install is None:
            price_state = "formula_unresolved"
        if price_state == "formula_unresolved":
            warnings.append(
                f"{formula_sheet.title} 第 {row_number} 行价格是公式且没有缓存值，"
                "需重算后再用于计价。"
            )
        if material is None and price_state == "priced":
            material = "0"
            assumed_material_zero += 1
        if install is None and price_state == "priced":
            install = "0"
            assumed_install_zero += 1
        quantity = None
        if "quantity" in mapping:
            quantity = _number(
                value_sheet.cell(row_number, mapping["quantity"]).value
            )
        rows.append(
            {
                "rowNumber": row_number,
                "index": _text(formula_sheet.cell(row_number, 1).value),
                "name": name,
                "spec": spec,
                "unit": unit or "项",
                "quantity": quantity,
                "material": material,
                "installation": install,
                "category": _category(name, spec),
                "system": _system(name, spec),
                "priceState": price_state,
            }
        )
    if assumed_material_zero:
        warnings.append(
            f"有 {assumed_material_zero} 行材料价为空，已按 0 处理；请核对是否包含在安装价中。"
        )
    if assumed_install_zero:
        warnings.append(
            f"有 {assumed_install_zero} 行安装价为空，已按 0 处理；请核对是否已含在材料价中。"
        )
    return rows, warnings


def import_price_book_auto(
    path: Path,
    *,
    file_id: str,
    source_sha256: str,
) -> dict:
    """优先读标准第一 Sheet；找不到时扫描所有 Sheet 自动识别报价表。

    支持客户常见的 A-Q 列水冷报价表（名称/规格/单位/工程量/材料单价/安装单价），
    不再要求客户先手工改成 B/C/D/G/H。
    """
    formula_book = load_workbook(path, data_only=False, read_only=True)
    value_book = load_workbook(path, data_only=True, read_only=True)
    try:
        candidates: list[tuple[int, str, int, dict[str, int]]] = []
        for sheet_name in formula_book.sheetnames:
            sheet = formula_book[sheet_name]
            detected = _detect_table(sheet)
            if detected is None:
                continue
            header_row, mapping = detected
            value_sheet = value_book[sheet_name]
            rows, _ = _mapped_rows(sheet, value_sheet, mapping, header_row)
            if not rows:
                continue
            score = (100 if "installation" in mapping else 0) + len(rows)
            candidates.append((score, sheet_name, header_row, mapping))
        if not candidates:
            return import_price_book(
                path, file_id=file_id, source_sha256=source_sha256
            )
        candidates.sort(reverse=True)
        _, sheet_name, header_row, mapping = candidates[0]
        formula_sheet = formula_book[sheet_name]
        value_sheet = value_book[sheet_name]
        rows, warnings = _mapped_rows(
            formula_sheet, value_sheet, mapping, header_row
        )
        if not rows:
            return import_price_book(
                path, file_id=file_id, source_sha256=source_sha256
            )
        columns = {
            key: get_column_letter(column) for key, column in mapping.items()
        }
        return {
            "schemaVersion": "price-book-1",
            "fileId": file_id,
            "sourceSha256": source_sha256,
            "sheetName": sheet_name,
            "headerRow": header_row,
            "detectedBy": "auto",
            "columns": columns,
            "rows": rows,
            "warnings": warnings,
            "counts": {
                "rows": len(rows),
                "priced": sum(1 for row in rows if row["priceState"] == "priced"),
                "equipment": sum(1 for row in rows if row["category"] == "equipment"),
                "pipe": sum(1 for row in rows if row["category"] == "pipe"),
            },
        }
    finally:
        formula_book.close()
        value_book.close()


def inspect_workbook_tables(path: Path) -> list[dict]:
    """列出一个工作簿的 Sheet、前几行预览和自动识别的列映射。"""
    workbook = load_workbook(path, data_only=False, read_only=True)
    try:
        result: list[dict] = []
        for sheet_name in workbook.sheetnames:
            sheet = workbook[sheet_name]
            preview = []
            for row_number in range(1, min(sheet.max_row, 5) + 1):
                values = [
                    _text(sheet.cell(row_number, column).value)
                    for column in range(1, min(sheet.max_column, 16) + 1)
                ]
                if any(values):
                    preview.append(
                        {
                            "row": row_number,
                            "values": values,
                        }
                    )
            detected = _detect_table(sheet)
            columns = {}
            header_row = None
            if detected is not None:
                header_row, mapping = detected
                columns = {
                    key: get_column_letter(column)
                    for key, column in mapping.items()
                }
            result.append(
                {
                    "name": sheet_name,
                    "maxRow": sheet.max_row,
                    "maxColumn": sheet.max_column,
                    "preview": preview,
                    "headerRow": header_row,
                    "columns": columns,
                }
            )
        return result
    finally:
        workbook.close()


def import_price_book_mapped(
    path: Path,
    *,
    file_id: str,
    source_sha256: str,
    sheet_name: str,
    header_row: int,
    columns: dict[str, str],
) -> dict:
    """按客户手工指定的 Sheet/表头行/列导入价格书。"""
    required = {"name", "spec", "unit", "material"}
    missing = [key for key in required if not str(columns.get(key) or "").strip()]
    if missing:
        raise ValueError("请至少指定名称、规格、单位、材料价四列。")
    if header_row < 1:
        raise ValueError("表头行必须从第 1 行开始。")
    formula_book = load_workbook(path, data_only=False, read_only=True)
    value_book = load_workbook(path, data_only=True, read_only=True)
    try:
        if sheet_name not in formula_book.sheetnames:
            raise ValueError(f"工作表“{sheet_name}”不存在。")
        formula_sheet = formula_book[sheet_name]
        value_sheet = value_book[sheet_name]
        mapping: dict[str, int] = {}
        for key, value in columns.items():
            text = str(value or "").strip()
            if not text:
                continue
            try:
                if text.isdigit():
                    column = int(text)
                else:
                    column = column_index_from_string(text.upper())
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{key} 列“{text}”无效。") from exc
            if column < 1 or column > formula_sheet.max_column:
                raise ValueError(f"{key} 列“{text}”超出工作表范围。")
            mapping[key] = column
        rows, warnings = _mapped_rows(
            formula_sheet, value_sheet, mapping, header_row
        )
        if not rows:
            raise ValueError("按所选表头和列没有读到价格行；请检查表头行和列。")
        header_labels = {
            key: _normalize_header(
                formula_sheet.cell(header_row, column).value
            )
            for key, column in mapping.items()
        }
        return {
            "schemaVersion": "price-book-1",
            "fileId": file_id,
            "sourceSha256": source_sha256,
            "sheetName": sheet_name,
            "headerRow": header_row,
            "detectedBy": "manual",
            "columns": {
                key: get_column_letter(column)
                for key, column in mapping.items()
            },
            "headerLabels": header_labels,
            "mappingSignature": mapping_signature(header_labels),
            "rows": rows,
            "warnings": warnings,
            "counts": {
                "rows": len(rows),
                "priced": sum(1 for row in rows if row["priceState"] == "priced"),
                "equipment": sum(1 for row in rows if row["category"] == "equipment"),
                "pipe": sum(1 for row in rows if row["category"] == "pipe"),
            },
        }
    finally:
        formula_book.close()
        value_book.close()


def mapping_signature(header_labels: dict[str, str]) -> str:
    payload = json.dumps(
        {key: value for key, value in sorted(header_labels.items()) if value},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def find_matching_mapping(path: Path, profiles: list[dict]) -> dict | None:
    """在客户保存的映射配置中找与当前工作簿表头一致的一组列。"""
    if not profiles:
        return None
    workbook = load_workbook(path, data_only=False, read_only=True)
    try:
        matches: list[tuple[int, dict]] = []
        for sheet_name in workbook.sheetnames:
            sheet = workbook[sheet_name]
            max_column = min(sheet.max_column, 40)
            for header_row in range(1, min(sheet.max_row, 15) + 1):
                by_header: dict[str, int] = {}
                for column in range(1, max_column + 1):
                    label = _normalize_header(
                        sheet.cell(header_row, column).value
                    )
                    if label:
                        by_header.setdefault(label, column)
                for profile in profiles:
                    headers = profile.get("headers") or {}
                    if not {"name", "spec", "unit", "material"} <= set(headers):
                        continue
                    mapping: dict[str, int] = {}
                    for key, label in headers.items():
                        if not str(label or "").strip():
                            continue
                        column = by_header.get(_normalize_header(label))
                        if column is None:
                            break
                        mapping[key] = column
                    else:
                        if not {"name", "spec", "unit", "material"} <= set(mapping):
                            continue
                        matches.append(
                            (
                                0 if sheet_name == profile.get("sheetName") else 1,
                                {
                                    "sheetName": sheet_name,
                                    "headerRow": header_row,
                                    "columns": {
                                        key: get_column_letter(column)
                                        for key, column in mapping.items()
                                    },
                                    "profileId": profile.get("id"),
                                    "profileName": profile.get("fileName"),
                                    "profile": profile,
                                },
                            )
                        )
        if not matches:
            return None
        matches.sort(key=lambda item: item[0])
        return matches[0][1]
    finally:
        workbook.close()
