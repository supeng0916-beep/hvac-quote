import json
from decimal import Decimal
from pathlib import Path

import pytest

from hvac_quote.drawing_adapter import _route_category
from hvac_quote.vocabulary import DEFAULT_VOCABULARY, load_vocabulary






def test_vocabulary_builtin_file_has_all_sections():
    vocabulary = load_vocabulary()
    assert set(vocabulary) == set(DEFAULT_VOCABULARY)
    assert "空调" in vocabulary["equipment_layer_markers"]
    assert "水管" in vocabulary["water_system_hints"]
    assert "平面图" in vocabulary["floor_title_markers"]


def test_water_supply_and_return_layers_are_water_routes():
    assert _route_category("PIPE-空冷供水", []) == "水管"
    assert _route_category("PIPE-空冷回水", []) == "水管"
    assert _route_category("S-给排水(适用于设备图）", []) == "排水"
    assert _route_category("PIPE-冷媒管", []) == "冷媒管"


def test_vocabulary_override_merges_with_defaults(tmp_path):
    path = tmp_path / "vocabulary.json"
    path.write_text(
        json.dumps({"floor_title_markers": ["MYFLOOR"]}, ensure_ascii=False),
        encoding="utf-8",
    )

    vocabulary = load_vocabulary(path)

    assert vocabulary["floor_title_markers"] == ("MYFLOOR",)
    assert vocabulary["equipment_layer_markers"] == DEFAULT_VOCABULARY[
        "equipment_layer_markers"
    ]


def test_vocabulary_unknown_field_is_rejected(tmp_path):
    path = tmp_path / "vocabulary.json"
    path.write_text(json.dumps({"unknown": ["x"]}), encoding="utf-8")

    with pytest.raises(RuntimeError):
        load_vocabulary(path)
