"""EXP-02: omissions must be readable and must never become zero quantities."""
from hvac_quote.generic_quote import _excluded_group_omission


def test_unknown_drain_length_is_not_zero_or_model_label():
    item = _excluded_group_omission(
        {
            "category": "排水",
            "spec": "40",
            "floorLabel": "model",
            "lengthM": None,
            "priceRow": None,
            "include": False,
            "linkedMaterialMissing": True,
        },
        "route",
        price_rows={},
    )
    assert item.quantity is None
    assert "DN40" in item.subject and "长度未确定" in item.subject
    assert "model" not in item.subject and "0米" not in item.subject
    assert "价格" in item.subject and "保温" in item.subject
    assert item.reason == "route_review_required"


def test_candidate_length_is_not_the_included_quantity():
    item = _excluded_group_omission(
        {
            "category": "排水",
            "spec": "40",
            "lengthM": "123.456789",
            "priceRow": None,
            "include": False,
        },
        "route",
        price_rows={},
    )
    assert item.quantity is None
    assert "123.457" in item.subject
    assert "非本次已计入工程量" in item.subject


def test_block_name_is_not_presented_as_a_purchase_model():
    item = _excluded_group_omission(
        {
            "model": "",
            "blockName": "RMX112C",
            "quantity": 8,
            "priceRow": None,
            "include": False,
        },
        "equipment",
        price_rows={},
    )
    assert item.quantity is None
    assert "RMX112C" in item.subject
    assert "图块" in item.subject
    assert "采购型号" in item.subject and "待确认" in item.subject
    assert item.reason == "model_unconfirmed"


def test_priced_but_excluded_is_not_reported_as_missing_price():
    item = _excluded_group_omission(
        {
            "category": "冷媒管",
            "spec": "9.53",
            "lengthM": "10",
            "priceRow": 102,
            "include": False,
        },
        "route",
        price_rows={102: {"rowNumber": 102, "priceState": "priced"}},
    )
    assert "缺匹配管价" not in item.subject
    assert "用户未纳入本次报价" in item.subject
    assert item.reason == "user_excluded"


def test_block_source_requires_purchase_model_confirmation():
    item = _excluded_group_omission(
        {
            "model": "RMX112C",
            "modelSource": "block",
            "blockName": "RMX112C",
            "equipmentKind": "室外机",
            "quantity": 8,
            "priceRow": None,
            "include": False,
        },
        "equipment",
        price_rows={},
    )
    assert "室外机图块 RMX112C" in item.subject
    assert "采购型号及单价待确认" in item.subject
    assert "缺匹配设备单价" not in item.subject
    assert item.reason == "model_unconfirmed"


def test_unknown_route_label_and_long_decimal_are_business_readable():
    item = _excluded_group_omission(
        {
            "category": "unknown",
            "spec": "40",
            "lengthM": "123.456789",
            "priceRow": None,
            "include": False,
            "eligibility": "review",
        },
        "route",
        price_rows={},
    )
    assert "管线类别待确认" in item.subject
    assert "DN40" in item.subject
    assert "unknown" not in item.subject
    assert "123.457" in item.subject
    assert "123.456789" not in item.subject
