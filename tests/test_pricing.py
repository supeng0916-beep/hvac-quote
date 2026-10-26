from decimal import Decimal

import pytest

from hvac_quote.models import QuantityItem
from hvac_quote.pricing import coefficient_review_exception, price_items

PRICES = {6: (Decimal("100"), Decimal("10")), 7: (Decimal("200"), Decimal("20")), 8: (Decimal("3"), Decimal("1"))}
SYNTHETIC = [("demo-unit-a", 6, "2"), ("demo-unit-b", 7, "1"), ("demo-pipe", 8, "4")]


def _items(rows):
    return [
        QuantityItem(
            item_id=item_id,
            category="equipment" if row in (6, 7) else "pipe",
            spec="x",
            unit="套" if row in (6, 7) else "米",
            quantity=Decimal(quantity),
            template_row=row,
        )
        for item_id, row, quantity in rows
    ]


def test_synthetic_stage_pricing():
    result = price_items(_items(SYNTHETIC), PRICES, tax_rate=Decimal("0.06"))
    assert result.subtotal_exact == Decimal("456")
    assert result.tax_exact == Decimal("27.36")
    assert result.total_before_rounding == Decimal("483.36")
    assert result.subtotal == Decimal("456.00")
    assert result.tax == Decimal("27.36")
    assert result.total == Decimal("483.36")
    assert len(result.lines) == 3


def test_b_rounding_does_not_round_intermediate_lines():
    items = _items(
        [
            ("a", 8, "2.125"),
            ("b", 9, "3.375"),
        ]
    )
    result = price_items(
        items,
        {8: (Decimal("1.235"), Decimal("0")), 9: (Decimal("1.235"), Decimal("0"))},
        tax_rate=Decimal("0"),
    )

    assert result.total_before_rounding == Decimal("6.7925")
    assert result.total == Decimal("6.79")


def test_category_coefficient_overrides_global_without_double_multiply():
    items = _items([("equipment", 6, "1"), ("pipe", 8, "1")])
    result = price_items(
        items,
        {6: (Decimal("10000"), Decimal("0")), 8: (Decimal("2000"), Decimal("0"))},
        tax_rate=Decimal("0"),
        global_coefficient=Decimal("1.2"),
        category_coefficients={"pipe": Decimal("1.3")},
    )

    assert result.lines[1].material_amount == Decimal("2000")
    assert result.category_totals["equipment_material"] == Decimal("12000")
    assert result.category_totals["pipe_material"] == Decimal("2600")
    assert result.subtotal_exact == Decimal("14600")


def test_unknown_quantity_is_not_priced_as_zero():
    items = _items([("unknown", 8, "0")])
    items[0].quantity = None

    result = price_items(items, PRICES)

    assert result.lines == []
    assert result.subtotal_exact == Decimal("0")


def test_coefficient_review_exception_is_review_only():
    at_one = coefficient_review_exception(
        global_coefficient=Decimal("1"),
        equipment_coefficient=Decimal("1"),
        pipe_coefficient=Decimal("1"),
    )
    assert at_one is not None
    assert at_one["code"] == "COEFFICIENT_AT_ONE"
    assert at_one["severity"] == "review"

    below = coefficient_review_exception(
        global_coefficient=Decimal("0.9"),
        equipment_coefficient=Decimal("0.9"),
        pipe_coefficient=Decimal("0.9"),
    )
    assert below is not None
    assert below["code"] == "COEFFICIENT_BELOW_ONE"
    assert below["severity"] == "review"

    assert coefficient_review_exception(
        global_coefficient=Decimal("1.2"),
        equipment_coefficient=Decimal("1.1"),
        pipe_coefficient=Decimal("1.3"),
    ) is None


def test_tax_basis_subtotal_and_installation_auxiliary():
    """合成金额验证两种计税基数。"""
    from hvac_quote.models import QuantityItem
    from hvac_quote.pricing import price_items

    def make_item(item_id, category, material, installation):
        return QuantityItem(
            item_id,
            category,
            "",
            "台" if category == "equipment" else "米",
            Decimal("1"),
            unit_prices=(Decimal(material), Decimal(installation)),
        )

    items = [
        make_item("e1", "equipment", "1000", "0"),
        make_item("e2", "equipment", "0", "100"),
        make_item("p1", "pipe", "200", "0"),
        make_item("p2", "pipe", "0", "50"),
    ]
    whole_order = price_items(items, {}, tax_rate=Decimal("0.05"))
    assert whole_order.tax == Decimal("67.50")
    assert whole_order.total == Decimal("1417.50")

    partial_base = price_items(
        items,
        {},
        tax_rate=Decimal("0.09"),
        tax_basis="installation_and_auxiliary",
    )
    assert partial_base.tax == Decimal("31.50")
    assert partial_base.total == Decimal("1381.50")
    assert partial_base.tax_base_exact == Decimal("350")
