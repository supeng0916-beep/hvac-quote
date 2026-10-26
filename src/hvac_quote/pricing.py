"""模板价、分类系数、税与 B 舍入。

中间金额全程保留精度；只有最终展示金额按 half-up 保留两位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from .models import QuantityItem

CENT = Decimal("0.01")
ZERO = Decimal("0")


@dataclass(frozen=True)
class PriceLine:
    item_id: str
    template_row: int | None
    bucket: str
    quantity: Decimal
    material_unit_price: Decimal
    installation_unit_price: Decimal
    material_amount: Decimal
    installation_amount: Decimal
    base_amount: Decimal
    price_source: str | None = None
    display_name: str | None = None
    display_spec: str | None = None


@dataclass
class PricingResult:
    lines: list[PriceLine]
    category_bases: dict[str, Decimal]
    category_totals: dict[str, Decimal]
    coefficients: dict[str, Decimal]
    subtotal_exact: Decimal
    tax_exact: Decimal
    total_before_rounding: Decimal
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    rounding_basis: dict = field(default_factory=dict)
    tax_base_exact: Decimal = ZERO
    tax_basis: str = "subtotal"


def round_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def coefficient_review_exception(
    *,
    global_coefficient: Decimal,
    equipment_coefficient: Decimal,
    pipe_coefficient: Decimal,
) -> dict | None:
    """系数不大于 1 时的 review 级提醒；不阻断报价，只要求人工确认。"""
    values = {
        "total": ("总体系数", _decimal(global_coefficient)),
        "equipment": ("设备系数", _decimal(equipment_coefficient)),
        "pipe": ("管线系数", _decimal(pipe_coefficient)),
    }
    below = {
        key: (label, value)
        for key, (label, value) in values.items()
        if value < Decimal("1")
    }
    if below:
        detail = "、".join(
            f"{label} {value.normalize()}" for label, value in below.values()
        )
        return {
            "code": "COEFFICIENT_BELOW_ONE",
            "message": (
                f"系数小于 1（{detail}），请确认是否为让利或下浮；"
                "这会直接减少含税合计。"
            ),
            "severity": "review",
            "detail": {
                "coefficients": {
                    key: str(value) for key, (_, value) in values.items()
                }
            },
        }
    if all(value == Decimal("1") for _, value in values.values()):
        return {
            "code": "COEFFICIENT_AT_ONE",
            "message": (
                "总体系数、设备系数、管线系数均为 1（未加价），"
                "请确认报价是否已包含预期利润后再生成。"
            ),
            "severity": "review",
            "detail": {
                "coefficients": {
                    key: str(value) for key, (_, value) in values.items()
                }
            },
        }
    return None


def _decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _coefficient(key: str, bucket: str, global_coefficient: Decimal, overrides: dict) -> Decimal:
    if key in overrides:
        return _decimal(overrides[key])
    if bucket in overrides:
        return _decimal(overrides[bucket])
    return _decimal(global_coefficient)


TAX_BASIS_LABELS = {
    "subtotal": "整单未税金额",
    "installation_and_auxiliary": "安装费与辅材（不含主设备材料）",
}


def price_items(
    items: list[QuantityItem],
    prices: dict[int, tuple[Decimal, Decimal]],
    *,
    tax_rate: Decimal = Decimal("0"),
    global_coefficient: Decimal = Decimal("1"),
    category_coefficients: dict[str, Decimal] | None = None,
    tax_basis: str = "subtotal",
) -> PricingResult:
    overrides = category_coefficients or {}
    bases = {
        "equipment_material": ZERO,
        "equipment_installation": ZERO,
        "pipe_material": ZERO,
        "pipe_installation": ZERO,
    }
    lines: list[PriceLine] = []

    for item in items:
        if item.quantity is None:
            # 未知数量不进入计价，也不用 0 冒充。
            continue
        if item.unit_prices is not None:
            material_unit, installation_unit = (
                _decimal(item.unit_prices[0]),
                _decimal(item.unit_prices[1]),
            )
        else:
            if item.template_row is None:
                raise ValueError(f"{item.item_id}: 缺少模板行或项目价")
            if item.template_row not in prices:
                raise KeyError(f"{item.item_id}: 模板行 {item.template_row} 没有价格")
            material_unit, installation_unit = (
                _decimal(prices[item.template_row][0]),
                _decimal(prices[item.template_row][1]),
            )
        bucket = "equipment" if item.category == "equipment" else "pipe"
        material = item.quantity * material_unit
        installation = item.quantity * installation_unit
        bases[f"{bucket}_material"] += material
        bases[f"{bucket}_installation"] += installation
        lines.append(
            PriceLine(
                item_id=item.item_id,
                template_row=item.template_row,
                bucket=bucket,
                quantity=item.quantity,
                material_unit_price=material_unit,
                installation_unit_price=installation_unit,
                material_amount=material,
                installation_amount=installation,
                base_amount=material + installation,
                price_source=item.price_source,
                display_name=item.display_name,
                display_spec=item.display_spec,
            )
        )

    coefficients: dict[str, Decimal] = {}
    adjusted: dict[str, Decimal] = {}
    for key, base in bases.items():
        bucket = "equipment" if key.startswith("equipment") else "pipe"
        coefficient = _coefficient(key, bucket, global_coefficient, overrides)
        coefficients[key] = coefficient
        adjusted[key] = base * coefficient

    subtotal_exact = sum(adjusted.values(), ZERO)
    if tax_basis == "installation_and_auxiliary":
        # 示例口径：主设备材料不进税基，设备安装人工和辅材（材料+安装）进税基。
        tax_base_exact = (
            adjusted["equipment_installation"]
            + adjusted["pipe_material"]
            + adjusted["pipe_installation"]
        )
    else:
        tax_base_exact = subtotal_exact
    tax_exact = tax_base_exact * _decimal(tax_rate)
    total_before_rounding = subtotal_exact + tax_exact
    subtotal = round_money(subtotal_exact)
    tax = round_money(tax_exact)
    total = round_money(total_before_rounding)

    return PricingResult(
        lines=lines,
        category_bases=bases,
        category_totals=adjusted,
        coefficients=coefficients,
        subtotal_exact=subtotal_exact,
        tax_exact=tax_exact,
        total_before_rounding=total_before_rounding,
        subtotal=subtotal,
        tax=tax,
        total=total,
        rounding_basis={
            "profile": "exact-intermediate-final-half-up2-v2",
            "subtotalExact": str(subtotal_exact),
            "taxBase": tax_basis,
            "taxBaseExact": str(tax_base_exact),
            "taxableBases": {
                key: str(adjusted[key])
                for key in (
                    "equipment_installation",
                    "pipe_material",
                    "pipe_installation",
                )
            },
            "taxExact": str(tax_exact),
            "totalBeforeRounding": str(total_before_rounding),
            "finalTotal": str(total),
        },
        tax_base_exact=tax_base_exact,
        tax_basis=tax_basis,
    )
