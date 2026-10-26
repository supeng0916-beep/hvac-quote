from decimal import Decimal

from hvac_quote.money import format_quantity


def test_format_quantity_rounds_to_millimetres_for_display():
    assert format_quantity(Decimal("47.23456789")) == "47.235"
    assert format_quantity(Decimal("10.78012345")) == "10.78"
    assert format_quantity(Decimal("0.21512345")) == "0.215"
    assert format_quantity(2) == "2"
    assert format_quantity("3.0004") == "3"
