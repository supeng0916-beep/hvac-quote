"""报价交付展示：统一分组与规格排序，不改动计价和公式。

分组顺序固定为：冷媒铜管 → 铜管保温 → 排水管 → 排水保温 → 其他辅材。
分组只影响展示；数量、单价、金额和 Excel 模板行/公式保持原样。
"""

from __future__ import annotations

import re
from decimal import Decimal

GROUP_ORDER = (
    ("copper_pipe", "冷媒铜管"),
    ("copper_insulation", "铜管保温"),
    ("drain_pipe", "排水管"),
    ("drain_insulation", "排水保温"),
    ("other", "其他辅材"),
)
GROUP_LABELS = dict(GROUP_ORDER)
SPEC_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _spec_number(text: str) -> float:
    match = SPEC_NUMBER_RE.search(str(text or ""))
    if not match:
        return float("inf")
    try:
        return float(match.group(1))
    except ValueError:
        return float("inf")


def classify(name: str, spec: str) -> str:
    """按名称/规格归类；未知系统放入其他辅材，不混入铜管。"""
    text = f"{name} {spec}".upper()
    name_text = str(name or "")
    spec_text = str(spec or "")
    is_insulation = "保温" in name_text or "保温" in spec_text
    is_drain = any(
        marker in text
        for marker in ("排水", "PVC", "UPVC", "冷凝水", "冷凝")
    )
    is_copper = (
        any(marker in text for marker in ("铜管", "冷媒"))
        or "Φ" in spec_text
        or "φ" in spec_text
    )
    if is_insulation:
        if is_drain or "DN" in spec_text.upper():
            return "drain_insulation"
        if is_copper:
            return "copper_insulation"
        return "other"
    if is_drain:
        return "drain_pipe"
    if is_copper:
        return "copper_pipe"
    return "other"


def build_groups(
    price_lines: list[dict],
    items: dict[str, dict],
    labels: dict[int, dict],
) -> list[dict]:
    """把 priceLines 转成分组行；同名不同规格保持独立，不合并金额。"""
    grouped: dict[str, list[dict]] = {key: [] for key, _ in GROUP_ORDER}
    for line in price_lines or []:
        item = items.get(str(line.get("itemId")), {})
        template_row = line.get("templateRow")
        label = (
            labels.get(int(template_row))
            if template_row is not None
            else None
        ) or {}
        name = str(
            item.get("displayName")
            or item.get("display_name")
            or " ".join(
                part
                for part in (label.get("name"), label.get("spec"))
                if part
            )
            or item.get("spec")
            or "项目价补项"
        )
        spec = str(
            item.get("displaySpec")
            or item.get("display_spec")
            or label.get("spec")
            or (item.get("spec") if template_row is None else "")
            or ""
        )
        unit = str(item.get("unit") or label.get("unit") or "")
        quantity = str(line.get("quantity") or "")
        amount = str(line.get("baseAmount") or line.get("base_amount") or "0")
        category = str(item.get("category") or "")
        if category == "equipment":
            key = "equipment"
        else:
            key = classify(name, spec)
        grouped.setdefault(key, []).append(
            {
                "name": name,
                "spec": spec,
                "unit": unit,
                "quantity": quantity,
                "amount": amount,
                "materialUnitPrice": str(
                    line.get("materialUnitPrice")
                    or line.get("material_unit_price")
                    or ""
                ),
                "installationUnitPrice": str(
                    line.get("installationUnitPrice")
                    or line.get("installation_unit_price")
                    or ""
                ),
                "templateRow": template_row,
                "itemId": str(line.get("itemId") or ""),
                "group": key,
            }
        )
    groups = []
    for key, title in GROUP_ORDER:
        rows = grouped.get(key) or []
        if not rows:
            continue
        rows.sort(
            key=lambda row: (
                _spec_number(row["spec"]),
                str(row["name"]),
                str(row["itemId"]),
            )
        )
        total = sum(
            Decimal(str(row["amount"]).replace(",", "")) for row in rows
        )
        groups.append(
            {
                "key": key,
                "title": title,
                "rows": rows,
                "total": total,
            }
        )
    return groups


def equipment_rows(
    price_lines: list[dict],
    items: dict[str, dict],
    labels: dict[int, dict],
) -> list[dict]:
    rows = []
    for line in price_lines or []:
        item = items.get(str(line.get("itemId")), {})
        if str(item.get("category") or "") != "equipment":
            continue
        template_row = line.get("templateRow")
        label = (
            labels.get(int(template_row))
            if template_row is not None
            else None
        ) or {}
        rows.append(
            {
                "name": str(
                    item.get("displayName")
                    or label.get("name")
                    or "设备"
                ),
                "spec": str(
                    item.get("displaySpec")
                    or label.get("spec")
                    or item.get("spec")
                    or ""
                ),
                "unit": str(item.get("unit") or label.get("unit") or ""),
                "quantity": str(line.get("quantity") or ""),
                "amount": str(
                    line.get("baseAmount") or line.get("base_amount") or "0"
                ),
            }
        )
    return rows


__all__ = [
    "GROUP_LABELS",
    "GROUP_ORDER",
    "build_groups",
    "classify",
    "equipment_rows",
]
