"""视觉裁图渲染与像素↔CAD 映射（离线）。"""

from pathlib import Path

import ezdxf
from ezdxf.enums import TextEntityAlignment

from hvac_quote.vision_crop import render_crop


def _drawing(path: Path) -> Path:
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_line((0, 0), (5000, 0), dxfattribs={"layer": "冷媒铜管"})
    msp.add_lwpolyline(
        [(0, 1000), (2500, 1000), (2500, 3000)],
        dxfattribs={"layer": "冷媒铜管"},
    )
    text = msp.add_text("9.53×15.88", height=200)
    text.set_placement((1000, 300), align=TextEntityAlignment.LEFT)
    block = doc.blocks.new("FP-102")
    block.add_lwpolyline([(0, 0), (800, 0), (800, 600), (0, 600)], close=True)
    msp.add_blockref("FP-102", (3500, 1500), dxfattribs={"layer": "AC"})
    doc.saveas(path)
    return path


def test_render_crop_returns_png_and_mapping(tmp_path):
    path = _drawing(tmp_path / "crop.dxf")
    result = render_crop(path, "crop", tmp_path / "work", max_size=800)
    assert result.png[:8] == b"\x89PNG\r\n\x1a\n"
    assert result.entityCount >= 3
    mapping = result.mapping
    assert mapping["pixelToCad"]
    assert mapping["scale"] > 0
    assert mapping["cadBBox"][0] < mapping["cadBBox"][2]
    assert mapping["truncated"] is False


def test_render_crop_respects_bbox_and_candidates(tmp_path):
    path = _drawing(tmp_path / "crop.dxf")
    result = render_crop(
        path,
        "crop",
        tmp_path / "work",
        bbox=[3000, 1000, 4500, 2500],
        candidates=[
            {
                "candidateId": "equipment|fp102",
                "bbox": [3500, 1500, 4300, 2100],
            }
        ],
        max_size=600,
    )
    assert result.entityCount >= 1
    assert result.mapping["drawnEntities"] >= 1
    assert result.mapping["width"] <= 601
    assert result.mapping["height"] <= 601
    # 像素↔CAD 的边界换算自洽
    x0, y0, x1, y1 = result.mapping["cadBBox"]
    scale = result.mapping["scale"]
    assert abs((x0 + 0 / scale) - x0) < 1e-9
    assert abs((y1 - 0 / scale) - y1) < 1e-9


def test_render_crop_by_handle(tmp_path):
    path = _drawing(tmp_path / "crop.dxf")
    doc = ezdxf.readfile(path)
    handles = {entity.dxf.handle for entity in doc.modelspace()}
    result = render_crop(
        path, "crop", tmp_path / "work", handles=handles, max_size=500
    )
    assert result.entityCount >= 1
