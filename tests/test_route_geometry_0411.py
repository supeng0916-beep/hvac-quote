"""0.4.11 R1/R2 geometry regressions: real arcs, layer semantics, unknown lengths."""
from pathlib import Path

import ezdxf

from hvac_quote.drawing_adapter import (
    _accumulate_route_length,
    _entity_points,
    inspect_drawing,
)
from hvac_quote.generic_quote import build_draft_data


def _inspect(tmp_path: Path, doc, name: str) -> dict:
    path = tmp_path / f"{name}.dxf"
    doc.saveas(path)
    return inspect_drawing(path, name, tmp_path / f"{name}-work")


def _branch_groups(report: dict, handle: str) -> list[dict]:
    return [
        group
        for group in report["routeGroups"]
        if any(
            segment.get("definitionHandle") == handle
            for segment in group.get("segments") or []
        )
    ]


def test_arc_real_sweep_does_not_adopt_disconnected_branch(tmp_path):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    model = doc.modelspace()
    arc = model.add_arc(
        (0, 0), 10000, 0, 90, dxfattribs={"layer": "冷媒铜管9.53×15.88"}
    )
    branch = model.add_line((5000, 0), (5000, 300), dxfattribs={"color": 252})
    model.add_text(
        "9.53×15.88",
        dxfattribs={"height": 0.1, "insert": (5000.5, 310)},
    )
    report = _inspect(tmp_path, doc, "arc")
    arc_entity = next(
        entity
        for entity in report["routeGroups"]
        if any(
            segment.get("definitionHandle") == arc.dxf.handle
            for segment in entity.get("segments") or []
        )
    )
    assert "ARC_APPROXIMATED" in arc_entity["issues"]
    assert arc_entity["eligibility"] == "review"
    groups = _branch_groups(report, branch.dxf.handle)
    assert groups
    assert all(group["autoInclude"] is False for group in groups)
    assert all(group["eligibility"] == "review" for group in groups)

    entity = {
        "type": "ARC",
        "geometry": {
            "center": [0, 0],
            "radius": 10000,
            "startAngle": 0,
            "endAngle": 90,
        },
    }
    points = _entity_points(entity)
    assert len(points) > 4
    assert abs(points[0][0] - 10000) < 1e-9
    assert abs(points[0][1]) < 1e-9
    assert abs(points[-1][0]) < 1e-9
    assert abs(points[-1][1] - 10000) < 1e-9


def test_closed_outline_does_not_certify_branch(tmp_path):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    model = doc.modelspace()
    model.add_lwpolyline(
        [(0, 0), (20000, 0), (20000, 1000), (0, 1000)],
        close=True,
        dxfattribs={"layer": "冷媒铜管9.53×15.88"},
    )
    branch = model.add_line((10000, 0), (10000, 300), dxfattribs={"color": 252})
    model.add_text(
        "9.53×15.88",
        dxfattribs={"height": 0.1, "insert": (10000.5, 310)},
    )
    report = _inspect(tmp_path, doc, "closed")
    groups = _branch_groups(report, branch.dxf.handle)
    assert groups
    assert all(group["autoInclude"] is False for group in groups)


def test_same_view_line_branch_remains_eligible(tmp_path):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    model = doc.modelspace()
    model.add_line(
        (0, 0), (20000, 0), dxfattribs={"layer": "冷媒铜管9.53×15.88"}
    )
    branch = model.add_line((10000, 0), (10000, 300), dxfattribs={"color": 252})
    model.add_text(
        "9.53×15.88",
        dxfattribs={"height": 0.1, "insert": (10000.5, 310)},
    )
    report = _inspect(tmp_path, doc, "same-view")
    groups = _branch_groups(report, branch.dxf.handle)
    assert groups
    assert all(group["autoInclude"] is True for group in groups)


def test_explicit_equipment_layer_kept_and_exact_pipe_alias_excluded(tmp_path):
    def count(layer: str, name: str) -> int:
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        block = doc.blocks.new("OPAQUE_DEVICE")
        block.add_lwpolyline(
            [(0, 0), (100, 0), (100, 100), (0, 100)], close=True
        )
        doc.modelspace().add_blockref("OPAQUE_DEVICE", (0, 0), dxfattribs={"layer": layer})
        return _inspect(tmp_path, doc, name)["counts"]["equipment"]

    assert count("暖通-排风-阀门", "valve") == 1
    assert count("冷冻水泵", "pump") == 1
    assert count("ACS_KT_LN", "acs-alias") == 0


def test_unknown_curve_keeps_group_and_draft_length_null(tmp_path):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    block = doc.blocks.new("MDV-D125Q4")
    block.add_lwpolyline(
        [(-50, -50), (100, -50), (100, 50), (-50, 50)], close=True
    )
    block.add_spline(
        [(0, 1000), (5000, 2000), (10000, 1000)],
        dxfattribs={"layer": "冷凝水管 DN25"},
    )
    doc.modelspace().add_blockref(
        "MDV-D125Q4", (0, 0), dxfattribs={"layer": "暖通-空调-设备"}
    )
    report = _inspect(tmp_path, doc, "unknown")
    groups = [
        group
        for group in report["routeGroups"]
        if "冷凝水" in str(group.get("layer") or "")
    ]
    assert groups
    group = groups[0]
    assert group["lengthRaw"] is None
    assert group["lengthM"] is None
    assert group["lengthStatus"] == "unmeasured"
    assert group["lengthKnownRaw"] == 0
    assert group["unmeasuredSegmentCount"] == 1

    draft = build_draft_data(
        {"id": "unknown", "fileName": "unknown.dxf", "report": report},
        {"book": {"rows": []}},
    )
    route = next(
        item for item in draft["routes"] if "冷凝水" in item["label"]
    )
    assert route["lengthRaw"] is None
    assert route["lengthM"] is None
    assert route["lengthStatus"] == "unmeasured"
    assert route["unmeasuredSegmentCount"] == 1


def test_partial_group_keeps_known_subtotal_separate():
    group = {
        "lengthRaw": None,
        "lengthM": None,
        "lengthKnownRaw": 0.0,
        "lengthKnownM": 0.0,
        "unmeasuredSegmentCount": 0,
        "lengthStatus": "unmeasured",
    }
    from decimal import Decimal

    _accumulate_route_length(group, 10000.0, Decimal("0.001"))
    _accumulate_route_length(group, None, Decimal("0.001"))
    assert group["lengthRaw"] is None
    assert group["lengthM"] is None
    assert group["lengthStatus"] == "partial"
    assert group["lengthKnownRaw"] == 10000
    assert group["lengthKnownM"] == 10
    assert group["unmeasuredSegmentCount"] == 1
