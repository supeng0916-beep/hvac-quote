from hvac_quote.drawing_adapter import (
    _apply_layer_spec_fallback,
    _parse_pipe_specs,
)
from hvac_quote.generic_quote import _route_match


def test_water_pipe_dn_single_specs_include_large_mains():
    for text, value in [
        ("DN25", "25"),
        ("DN125", "125"),
        ("DN200", "200"),
        ("DN250", "250"),
    ]:
        specs = _parse_pipe_specs(text)
        assert [item["spec"] for item in specs] == [value]


def test_refrigerant_pair_is_still_split_and_duct_size_is_ignored():
    pair = _parse_pipe_specs("9.53×15.88")
    assert {(item["spec"], item["role"]) for item in pair} == {
        ("9.53", "液管"),
        ("15.88", "气管"),
    }
    assert _parse_pipe_specs("600×320") == []


def test_layer_dimension_fallback_fills_unique_dn_for_same_system():
    groups = [
        {
            "key": "route|model|3层|PIPE-空冷供水|LWPOLYLINE|水管|nospec|水管",
            "ruleKey": "route|PIPE-空冷供水|LWPOLYLINE|水管|nospec|水管",
            "layer": "PIPE-空冷供水",
            "category": "水管",
            "spec": "",
            "role": "水管",
            "issues": ["PIPE_SPEC_UNRESOLVED"],
        }
    ]
    texts = [{"layer": "DIM-空冷供水", "text": "DN125"}]

    _apply_layer_spec_fallback(groups, texts)

    assert groups[0]["spec"] == "125"
    assert groups[0]["specSource"] == "layer_dimension"
    assert "PIPE_SPEC_UNRESOLVED" not in groups[0]["issues"]


def test_inferred_layer_dn_maps_to_steel_pipe_but_requires_review():
    rows = [
        {
            "rowNumber": 69,
            "name": "无缝钢管",
            "spec": "DN125*4mm",
            "category": "pipe",
            "system": "water",
            "unit": "米",
            "priceState": "priced",
            "material": "20",
            "installation": "4",
        }
    ]
    group = {
        "category": "水管",
        "spec": "125",
        "specSource": "layer_dimension",
        "layer": "PIPE-空冷供水",
        "textHints": [],
    }

    row, confidence = _route_match(group, rows)

    assert row["rowNumber"] == 69
    assert confidence == "inferred"
