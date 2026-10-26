"""V048-04 / VIS-COLOR: content-addressed crops and stable ACI colors."""
import hashlib
import io
from pathlib import Path

import ezdxf
from PIL import Image

from hvac_quote.vision import canonical_json
from hvac_quote.vision_crop import UNKNOWN_COLOR, _color, render_crop


def _drawing(path: Path, source_lines: int = 40) -> Path:
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    block = doc.blocks.new("CONTEXT")
    for index in range(source_lines):
        block.add_line(
            (index * 10, 0),
            (200, 400),
            dxfattribs={"color": 150},
        )
    doc.modelspace().add_blockref(
        "CONTEXT", (0, 0), dxfattribs={"layer": "B-墙体"}
    )
    doc.saveas(path)
    return path


def _crop(path: Path, tmp_path: Path, **overrides):
    options = {
        "bbox": [-100, -100, 500, 500],
        "max_size": 800,
        "expand_blocks": False,
    }
    options.update(overrides)
    return render_crop(path, "fold", tmp_path / "work", **options)


def test_crop_identity_binds_png_manifest_and_expansion(tmp_path):
    path = _drawing(tmp_path / "context.dxf")
    collapsed = _crop(path, tmp_path / "a")
    expanded = _crop(
        path, tmp_path / "b", expand_blocks=True
    )
    assert collapsed.png != expanded.png
    assert collapsed.mapping["cropId"] != expanded.mapping["cropId"]
    for crop in (collapsed, expanded):
        manifest = crop.mapping["manifest"]
        assert crop.mapping["pngSha256"] == hashlib.sha256(crop.png).hexdigest()
        assert len(crop.mapping["cropId"]) == 64
        assert crop.mapping["cropId"] == hashlib.sha256(
            canonical_json(manifest).encode("utf-8")
        ).hexdigest()
        assert manifest["pngSha256"] == crop.mapping["pngSha256"]


def test_same_inputs_are_stable_and_options_change_identity(tmp_path):
    path = _drawing(tmp_path / "context.dxf")
    first = _crop(path, tmp_path / "a")
    second = _crop(path, tmp_path / "b")
    assert first.mapping["cropId"] == second.mapping["cropId"]
    changed = _crop(path, tmp_path / "c", max_size=600)
    assert changed.mapping["cropId"] != first.mapping["cropId"]
    changed_padding = _crop(path, tmp_path / "d", padding=0.2)
    assert changed_padding.mapping["cropId"] != first.mapping["cropId"]


def test_aci150_is_stable_and_not_index_rotated():
    entity = {
        "geometry": {
            "renderColor": {
                "effectiveAciColor": 150,
                "aciColor": 150,
            }
        }
    }
    assert _color(entity, 0) == (0, 127, 255)
    assert _color(entity, 1) == (0, 127, 255)


def test_true_color_zero_wins_over_raw_red_and_unknown_is_neutral():
    entity = {
        "geometry": {
            "renderColor": {
                "effectiveTrueColor": 0,
                "trueColor": 0xFF0000,
                "aciColor": 150,
            }
        }
    }
    assert _color(entity, 0) == (0, 0, 0)
    assert _color(entity, 1) == (0, 0, 0)
    unknown = {
        "geometry": {"renderColor": {"aciColor": 0, "effectiveAciColor": None}}
    }
    assert _color(unknown, 0) == UNKNOWN_COLOR
    assert _color(unknown, 1) == UNKNOWN_COLOR


def test_rendered_aci150_lines_use_the_same_blue(tmp_path):
    path = _drawing(tmp_path / "context.dxf")
    crop = _crop(path, tmp_path / "a", expand_blocks=True)
    image = Image.open(io.BytesIO(crop.png)).convert("RGB")
    colors = {color for _count, color in image.getcolors(maxcolors=1_000_000)}
    assert (0, 127, 255) in colors
    # The INSERT placeholder frame is drawn separately; all CAD stroke lines
    # themselves must use the same ACI150 blue.
    assert len(
        colors - {(255, 255, 255), (0, 127, 255), (70, 70, 70)}
    ) == 0
