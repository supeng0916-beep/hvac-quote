"""裁图对折叠设备块的二次展开。"""

from pathlib import Path

import ezdxf

from hvac_quote.vision_crop import render_crop


def _drawing(path: Path) -> Path:
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    block = doc.blocks.new("INDOOR_UNIT")
    for index in range(30):
        block.add_line((0, index * 20), (600, index * 20))
    msp.add_blockref("INDOOR_UNIT", (0, 0), dxfattribs={"layer": "B-墙体"})
    msp.add_text("MDV-D125", height=120).set_placement((700, 0))
    doc.saveas(path)
    return path


def test_crop_can_expand_folded_blocks(tmp_path):
    path = _drawing(tmp_path / "folded.dxf")
    collapsed = render_crop(
        path, "fold", tmp_path / "w1", expand_blocks=False
    )
    expanded = render_crop(
        path, "fold", tmp_path / "w2", expand_blocks=True
    )
    assert collapsed.mapping["blockExpansion"] == "collapsed"
    assert expanded.mapping["blockExpansion"] == "full"
    assert expanded.entityCount > collapsed.entityCount
