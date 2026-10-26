from hvac_quote.drawing_adapter import _assign_model_floors


def _line(handle: str, x0: float, y0: float, x1: float, y1: float) -> dict:
    return {
        "type": "LINE",
        "layer": "0",
        "source": {
            "bbox": [x0, y0, x1, y1],
            "entityHandle": handle,
            "view": "model",
        },
        "geometry": {"start": [x0, y0, 0], "end": [x1, y1, 0]},
    }


def _text(handle: str, text: str, x: float, y: float) -> dict:
    return {
        "text": text,
        "layer": "FLOOR-TEXT",
        "view": "model",
        "handle": handle,
        "bbox": [x - 100, y - 20, x + 100, y + 20],
    }


def test_same_model_space_floor_regions_are_assigned():
    entities = []
    for index in range(10):
        entities.append(
            _line(f"L{index}", index * 90, 100, index * 90 + 80, 900)
        )
    for index in range(10):
        entities.append(
            _line(
                f"R{index}",
                3000 + index * 90,
                100,
                3080 + index * 90,
                900,
            )
        )
    texts = [
        _text("T1", "一层平面图", 450, 950),
        _text("T2", "二层平面图", 3450, 950),
    ]
    floor_map, regions, issues = _assign_model_floors(entities, texts)
    assert {floor_map[f"L{i}"] for i in range(10)} == {"一层"}
    assert {floor_map[f"R{i}"] for i in range(10)} == {"二层"}
    assert len(regions) == 2
    assert {region["label"] for region in regions} == {"一层", "二层"}
    assert any(issue["code"] == "MODEL_FLOOR_REGIONS_DETECTED" for issue in issues)


def test_single_floor_model_remains_one_region():
    entities = [
        _line(f"L{index}", index * 40, 0, index * 40 + 30, 500)
        for index in range(10)
    ]
    texts = [_text("T1", "三层平面图", 200, 550)]
    floor_map, regions, _ = _assign_model_floors(entities, texts)
    assert set(floor_map.values()) == {"三层"}
    assert len(regions) == 1
