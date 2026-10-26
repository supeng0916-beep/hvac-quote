"""草稿批量补充表：一次导入设备、管线、安装项。

原则：模板行和人工价二选一；缺名称、数量/长度、价格依据的行整批拒绝，
不把空白或猜测值写进报价。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from io import BytesIO
from uuid import uuid4

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .generic_quote import link_route_material, route_row_error

EQUIPMENT_SHEET = "设备"
PIPE_SHEET = "管线"
INSTALL_SHEET = "安装项"
REFERENCE_SHEET = "模板行参考"
GUIDE_SHEET = "填写说明"

EQUIPMENT_HEADERS = (
    "名称",
    "规格",
    "单位",
    "数量",
    "模板行",
    "材料单价",
    "安装单价",
    "楼层",
)
PIPE_HEADERS = (
    "名称",
    "类别",
    "规格",
    "单位",
    "长度(米)",
    "模板行",
    "材料单价",
    "安装单价",
    "楼层",
)
INSTALL_HEADERS = (
    "名称",
    "规格",
    "单位",
    "分类",
    "数量",
    "材料单价",
    "安装单价",
    "楼层",
)


def _style_header(sheet) -> None:
    fill = PatternFill("solid", fgColor="D9E2F3")
    for cell in sheet[1]:
        cell.font = Font(name="宋体", size=10, bold=True)
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center")


def _write_headers(sheet, headers: tuple[str, ...], widths: tuple[int, ...]) -> None:
    for index, header in enumerate(headers, 1):
        sheet.cell(1, index, header)
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    _style_header(sheet)
    sheet.freeze_panes = "A2"


def build_supplement_workbook(price_book: dict) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)

    equipment = workbook.create_sheet(EQUIPMENT_SHEET)
    _write_headers(equipment, EQUIPMENT_HEADERS, (28, 24, 8, 10, 10, 12, 12, 14))
    pipes = workbook.create_sheet(PIPE_SHEET)
    _write_headers(pipes, PIPE_HEADERS, (28, 10, 20, 8, 10, 10, 12, 12, 14))
    install = workbook.create_sheet(INSTALL_SHEET)
    _write_headers(install, INSTALL_HEADERS, (28, 24, 8, 10, 10, 12, 12, 14))

    reference = workbook.create_sheet(REFERENCE_SHEET)
    ref_headers = ("模板行", "名称", "规格", "单位", "材料价", "安装价", "分类", "系统")
    _write_headers(reference, ref_headers, (10, 30, 24, 8, 12, 12, 10, 10))
    rows = [
        row
        for row in (price_book.get("book") or {}).get("rows", [])
        if row.get("priceState") == "priced"
    ]
    for index, row in enumerate(rows, 2):
        values = (
            row.get("rowNumber"),
            row.get("name"),
            row.get("spec"),
            row.get("unit"),
            row.get("material"),
            row.get("installation"),
            row.get("category"),
            row.get("system"),
        )
        for column, value in enumerate(values, 1):
            reference.cell(index, column, value)

    guide = workbook.create_sheet(GUIDE_SHEET)
    guide.column_dimensions["A"].width = 100
    guide["A1"] = "批量补充表填写说明"
    guide["A1"].font = Font(name="宋体", size=14, bold=True)
    lines = (
        "1. 设备、管线、安装项分别填在对应工作表；不需要的行留空。",
        "2. 模板行和人工价二选一：填了模板行就用模板价；没有模板行才填材料单价/安装单价。",
        "3. 模板行参考看“模板行参考”工作表；不要把行号填错。",
        "4. 数量、长度必须大于 0；管线长度单位是米。",
        "5. 没有价格依据的设备/管线不要导入；系统会整批拒绝有错误的表。",
        "6. 安装项会进入本次报价的“补充项目”，不写回模板。",
    )
    for index, line in enumerate(lines, 3):
        guide.cell(index, 1, line).alignment = Alignment(wrap_text=True)

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _decimal(value, *, field: str, sheet: str, row: int) -> tuple[Decimal | None, str | None]:
    text = _text(value)
    if not text:
        return None, None
    if text.startswith("="):
        return None, f"{sheet} 第 {row} 行 {field} 是公式，请填写数值"
    try:
        return Decimal(text), None
    except InvalidOperation:
        return None, f"{sheet} 第 {row} 行 {field} 不是有效数字"


def _header_map(sheet, headers: tuple[str, ...]) -> dict[str, int]:
    found = {}
    for cell in sheet[1]:
        label = _text(cell.value)
        if label in headers:
            found[label] = cell.column
    missing = [header for header in headers if header not in found]
    if missing:
        raise ValueError(
            f"工作表“{sheet.title}”缺少列：{'、'.join(missing)}；"
            "请使用系统下载的批量补充表。"
        )
    return found


def _row_values(sheet, row: int, mapping: dict[str, int]) -> dict:
    return {header: sheet.cell(row, column).value for header, column in mapping.items()}


def _parse_equipment(rows: list[dict], priced: dict[int, dict]) -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    errors: list[str] = []
    for row_number, values in rows:
        name = _text(values.get("名称"))
        if not name:
            continue
        quantity, error = _decimal(
            values.get("数量"), field="数量", sheet=EQUIPMENT_SHEET, row=row_number
        )
        if error:
            errors.append(error)
            continue
        if quantity is None or quantity <= 0:
            errors.append(f"{EQUIPMENT_SHEET} 第 {row_number} 行数量必须大于 0")
            continue
        template_row, error = _decimal(
            values.get("模板行"), field="模板行", sheet=EQUIPMENT_SHEET, row=row_number
        )
        if error:
            errors.append(error)
            continue
        material, error = _decimal(
            values.get("材料单价"), field="材料单价", sheet=EQUIPMENT_SHEET, row=row_number
        )
        if error:
            errors.append(error)
            continue
        installation, error = _decimal(
            values.get("安装单价"), field="安装单价", sheet=EQUIPMENT_SHEET, row=row_number
        )
        if error:
            errors.append(error)
            continue
        row_key = int(template_row) if template_row is not None else None
        if row_key is not None and row_key not in priced:
            errors.append(
                f"{EQUIPMENT_SHEET} 第 {row_number} 行模板行 {row_key} 不存在或不可计价"
            )
            continue
        if row_key is not None and (material is not None or installation is not None):
            errors.append(
                f"{EQUIPMENT_SHEET} 第 {row_number} 行模板行和人工价只能二选一"
            )
            continue
        if row_key is None and material is None and installation is None:
            errors.append(
                f"{EQUIPMENT_SHEET} 第 {row_number} 行需要模板行，或材料单价/安装单价"
            )
            continue
        items.append(
            {
                "name": name,
                "spec": _text(values.get("规格")),
                "unit": _text(values.get("单位")) or "台",
                "quantity": str(quantity),
                "price_row": row_key,
                "material": str(material if material is not None else Decimal("0")),
                "installation": str(
                    installation if installation is not None else Decimal("0")
                ),
                "floor": _text(values.get("楼层")) or "批量补充",
            }
        )
    return items, errors


def _parse_pipes(rows: list[dict], priced: dict[int, dict]) -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    errors: list[str] = []
    for row_number, values in rows:
        name = _text(values.get("名称"))
        if not name:
            continue
        length, error = _decimal(
            values.get("长度(米)"), field="长度(米)", sheet=PIPE_SHEET, row=row_number
        )
        if error:
            errors.append(error)
            continue
        if length is None or length <= 0:
            errors.append(f"{PIPE_SHEET} 第 {row_number} 行长度必须大于 0")
            continue
        template_row, error = _decimal(
            values.get("模板行"), field="模板行", sheet=PIPE_SHEET, row=row_number
        )
        if error:
            errors.append(error)
            continue
        material, error = _decimal(
            values.get("材料单价"), field="材料单价", sheet=PIPE_SHEET, row=row_number
        )
        if error:
            errors.append(error)
            continue
        installation, error = _decimal(
            values.get("安装单价"), field="安装单价", sheet=PIPE_SHEET, row=row_number
        )
        if error:
            errors.append(error)
            continue
        row_key = int(template_row) if template_row is not None else None
        if row_key is not None and row_key not in priced:
            errors.append(
                f"{PIPE_SHEET} 第 {row_number} 行模板行 {row_key} 不存在或不可计价"
            )
            continue
        if row_key is not None and (material is not None or installation is not None):
            errors.append(f"{PIPE_SHEET} 第 {row_number} 行模板行和人工价只能二选一")
            continue
        if row_key is None and material is None and installation is None:
            errors.append(
                f"{PIPE_SHEET} 第 {row_number} 行需要模板行，或材料单价/安装单价"
            )
            continue
        category_value = _text(values.get("类别")) or "管线"
        spec_value = _text(values.get("规格"))
        if row_key is not None:
            incompatibility = route_row_error(
                {
                    "category": category_value,
                    "spec": spec_value,
                    "medium": "",
                },
                priced.get(row_key),
            )
            if incompatibility:
                errors.append(
                    f"{PIPE_SHEET} 第 {row_number} 行：{incompatibility}"
                )
                continue
        items.append(
            {
                "name": name,
                "category": category_value,
                "spec": spec_value,
                "unit": _text(values.get("单位")) or "米",
                "length": str(length),
                "price_row": row_key,
                "material": str(material if material is not None else Decimal("0")),
                "installation": str(
                    installation if installation is not None else Decimal("0")
                ),
                "floor": _text(values.get("楼层")) or "批量补充",
            }
        )
    return items, errors


def _parse_install(rows: list[dict]) -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    errors: list[str] = []
    for row_number, values in rows:
        name = _text(values.get("名称"))
        if not name:
            continue
        quantity, error = _decimal(
            values.get("数量"), field="数量", sheet=INSTALL_SHEET, row=row_number
        )
        if error:
            errors.append(error)
            continue
        if quantity is None or quantity <= 0:
            errors.append(f"{INSTALL_SHEET} 第 {row_number} 行数量必须大于 0")
            continue
        material, error = _decimal(
            values.get("材料单价"), field="材料单价", sheet=INSTALL_SHEET, row=row_number
        )
        if error:
            errors.append(error)
            continue
        installation, error = _decimal(
            values.get("安装单价"), field="安装单价", sheet=INSTALL_SHEET, row=row_number
        )
        if error:
            errors.append(error)
            continue
        if material is None and installation is None:
            errors.append(
                f"{INSTALL_SHEET} 第 {row_number} 行需要材料单价或安装单价"
            )
            continue
        category = _text(values.get("分类"))
        category = "equipment" if category in {"设备", "equipment"} else "pipe"
        items.append(
            {
                "name": name,
                "spec": _text(values.get("规格")),
                "unit": _text(values.get("单位")) or ("台" if category == "equipment" else "项"),
                "category": category,
                "quantity": str(quantity),
                "material": str(material if material is not None else Decimal("0")),
                "installation": str(
                    installation if installation is not None else Decimal("0")
                ),
                "floor": _text(values.get("楼层")) or "批量补充",
            }
        )
    return items, errors


def parse_supplement(content: bytes, price_book: dict) -> tuple[dict, list[str]]:
    try:
        workbook = load_workbook(BytesIO(content), data_only=False)
    except Exception as exc:  # noqa: BLE001
        return {}, [f"无法读取 XLSX：{exc}"]
    priced = {
        int(row["rowNumber"]): row
        for row in (price_book.get("book") or {}).get("rows", [])
        if row.get("priceState") == "priced"
    }
    errors: list[str] = []
    parsed: dict[str, list[dict]] = {
        "equipment": [],
        "routes": [],
        "custom_items": [],
    }

    def rows_for(sheet_name: str, headers: tuple[str, ...]):
        sheet = workbook[sheet_name]
        mapping = _header_map(sheet, headers)
        rows = []
        for row_number in range(2, sheet.max_row + 1):
            values = _row_values(sheet, row_number, mapping)
            if any(_text(value) for value in values.values()):
                rows.append((row_number, values))
        return rows

    try:
        equipment, sheet_errors = _parse_equipment(
            rows_for(EQUIPMENT_SHEET, EQUIPMENT_HEADERS), priced
        )
        parsed["equipment"] = equipment
        errors.extend(sheet_errors)
        routes, sheet_errors = _parse_pipes(
            rows_for(PIPE_SHEET, PIPE_HEADERS), priced
        )
        parsed["routes"] = routes
        errors.extend(sheet_errors)
        custom_items, sheet_errors = _parse_install(
            rows_for(INSTALL_SHEET, INSTALL_HEADERS)
        )
        parsed["custom_items"] = custom_items
        errors.extend(sheet_errors)
    except KeyError as exc:
        errors.append(f"缺少工作表：{exc}; 请使用系统下载的批量补充表")
    except ValueError as exc:
        errors.append(str(exc))
    return parsed, errors


def apply_supplement(
    data: dict, parsed: dict, *, rows: list[dict] | None = None
) -> dict:
    equipment = list(data.get("equipment") or [])
    routes = list(data.get("routes") or [])
    custom_items = list(data.get("customItems") or [])
    for item in parsed.get("equipment", []):
        key = f"bulk-equipment-{uuid4().hex[:8]}"
        equipment.append(
            {
                "key": key,
                "ruleKey": key,
                "label": item["name"],
                "model": item["spec"],
                "quantity": item["quantity"],
                "unit": item["unit"],
                "priceRow": item["price_row"],
                "manualMaterial": item["material"] if item["price_row"] is None else None,
                "manualInstallation": item["installation"] if item["price_row"] is None else None,
                "manualUnit": item["unit"],
                "include": True,
                "confidence": "manual",
                "floorLabel": item["floor"],
                "layer": "批量补充",
                "issues": [],
            }
        )
    for item in parsed.get("routes", []):
        key = f"bulk-route-{uuid4().hex[:8]}"
        group = {
            "key": key,
            "ruleKey": key,
            "origin": "customer_input",
            "manualRecordId": key,
            "label": item["name"],
            "category": item["category"],
            "spec": item["spec"],
            "unit": item["unit"],
            "lengthRaw": None,
            "lengthM": item["length"],
            "priceRow": item["price_row"],
            "manualMaterial": item["material"] if item["price_row"] is None else None,
            "manualInstallation": item["installation"] if item["price_row"] is None else None,
            "manualUnit": item["unit"],
            "include": True,
            "confidence": "manual",
            "floorLabel": item["floor"],
            "layer": "批量补充",
            "issues": [],
        }
        routes.append(link_route_material(group, rows or []))
    for item in parsed.get("custom_items", []):
        custom_items.append(
            {
                "name": item["name"],
                "spec": item["spec"],
                "unit": item["unit"],
                "category": item["category"],
                "floor": item["floor"],
                "quantity": item["quantity"],
                "material": item["material"],
                "installation": item["installation"],
            }
        )
    data["equipment"] = equipment
    data["routes"] = routes
    data["customItems"] = custom_items
    return {
        "equipment": len(parsed.get("equipment", [])),
        "routes": len(parsed.get("routes", [])),
        "customItems": len(parsed.get("custom_items", [])),
    }


MISSING_HEADERS = (
    "名称/型号",
    "规格",
    "单位",
    "数量/长度",
    "楼层",
    "系统",
    "建议模板行",
    "说明",
)


def build_missing_price_workbook(data: dict, price_book: dict) -> bytes:
    """列出草稿里没有可用价格的候选，供客户逐项补价（不改母模板）。"""
    rows_by_number = {
        int(row["rowNumber"]): row
        for row in (price_book.get("book") or {}).get("rows") or []
    }

    def priced(group: dict) -> bool:
        if group.get("manualMaterial") is not None:
            return True
        row = rows_by_number.get(int(group.get("priceRow") or 0))
        return bool(row and row.get("priceState") == "priced")

    equipment_rows = []
    for group in data.get("equipment") or []:
        if group.get("isComponent") or group.get("isComponentManual"):
            continue
        if priced(group):
            continue
        equipment_rows.append(
            (
                str(group.get("label") or group.get("model") or ""),
                str(group.get("model") or ""),
                str(group.get("unit") or "台"),
                str(group.get("quantity") or ""),
                str(group.get("floorLabel") or ""),
                _system_label(group.get("systemHint")),
                "",
                "模板没有可用价格行；请确认系统、型号和单价依据。",
            )
        )

    route_rows = []
    for group in data.get("routes") or []:
        if priced(group):
            continue
        route_rows.append(
            (
                str(group.get("label") or group.get("category") or ""),
                str(group.get("spec") or ""),
                str(group.get("unit") or "米"),
                str(group.get("lengthM") or ""),
                str(group.get("floorLabel") or ""),
                _medium_label(group.get("medium"), group.get("category")),
                "",
                "规格或单价未确定；请补管径/材质和单价依据。",
            )
        )

    workbook = Workbook()
    first = workbook.active
    first.title = "设备缺价"
    _write_missing_sheet(first, equipment_rows)
    _write_missing_sheet(workbook.create_sheet("管线缺价"), route_rows)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _write_missing_sheet(sheet, rows) -> None:
    for column, header in enumerate(MISSING_HEADERS, 1):
        cell = sheet.cell(1, column, header)
        cell.font = Font(name="宋体", size=10, bold=True)
        cell.fill = PatternFill("solid", fgColor="FCE4D6")
    for row_index, values in enumerate(rows, start=2):
        for column, value in enumerate(values, 1):
            sheet.cell(row_index, column, value).font = Font(name="宋体", size=10)
    widths = (26, 26, 8, 14, 14, 12, 14, 46)
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width


def _system_label(value) -> str:
    return {"water": "水系统", "refrigerant": "冷媒系统"}.get(
        str(value or ""), "系统未知"
    )


def _medium_label(medium, category) -> str:
    labels = {
        "supply": "供水",
        "return": "回水",
        "condensate": "冷凝水",
        "drain": "排水",
        "cooling": "冷却水",
        "makeup": "补水",
        "refrigerant": "冷媒",
        "duct": "风管",
    }
    return labels.get(str(medium or ""), str(category or "未确定"))
