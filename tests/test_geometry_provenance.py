"""G-AUDIT / D048-01: source provenance and complete collapse paths."""
import ezdxf
import pytest

from hvac_cad.normalize import normalize
from hvac_quote.drawing_adapter import (
    _annotation_radii,
    _inherited_context_route_rejection,
)


def test_two_array_occurrences_have_distinct_collapse_paths():
    doc = ezdxf.new()
    leaf = doc.blocks.new("OPAQUE_LEAF")
    for i in range(20):
        leaf.add_line((0, i), (10, i))
    outer = doc.blocks.new("OUTER_CONTAINER")
    outer.add_blockref("OPAQUE_LEAF", (0, 0), dxfattribs={"layer": "B-墙体"})
    outer.add_line((0, 0), (100, 0), dxfattribs={"layer": "冷媒管"})
    doc.modelspace().add_blockref(
        "OUTER_CONTAINER", (0, 0), dxfattribs={"row_count": 2, "row_spacing": 1000}
    )
    report = normalize(doc, "f", "a" * 64).report()
    events = report["diagnostics"]["opaqueBlocks"]
    assert len(events) == 2
    keys = {(event["view"], tuple(event["instancePath"])) for event in events}
    assert len(keys) == 2
    assert len({event["eventId"] for event in events}) == 2
    assert {
        part
        for event in events
        for part in event["instancePath"]
        if part.startswith("instance-")
    } == {"instance-0", "instance-1"}
    assert all(event["representedInsertCount"] == 1 for event in events)
    assert all(event["definitionHandle"] for event in events)
    assert report["diagnostics"]["issueCounts"]["CONTEXT_BLOCK_COLLAPSED"] == len(events)


def test_protected_symbol_children_carry_owner_and_context():
    doc = ezdxf.new()
    symbol = doc.blocks.new("dadadddfvv")
    for i in range(20):
        symbol.add_line((0, i), (10, i))
    root = doc.modelspace().add_blockref(
        "dadadddfvv", (0, 0), dxfattribs={"layer": "CMDLJ_BZ_QYG"}
    )
    doc.modelspace().add_line((0, 0), (500, 0), dxfattribs={"layer": "冷媒铜管"})
    report = normalize(doc, "f", "b" * 64).report()
    root_handle = root.dxf.handle
    leaves = [
        entity
        for entity in report["entities"]
        if entity.get("type") == "LINE"
        and entity.get("instancePath", [None])[0] == root_handle
    ]
    assert leaves
    assert all(leaf["source"]["originalLayer"] == "0" for leaf in leaves)
    assert all(leaf["source"]["layerInherited"] is True for leaf in leaves)
    assert all(
        leaf["source"]["layerOwnerPath"] == [root_handle] for leaf in leaves
    )
    assert all(
        leaf["source"]["expansionContexts"][0]["instancePath"] == [root_handle]
        for leaf in leaves
    )
    assert all(
        _inherited_context_route_rejection(leaf)
        == "INHERITED_LAYER_CONTEXT_GEOMETRY"
        for leaf in leaves
    )
    independent = next(
        entity
        for entity in report["entities"]
        if entity.get("type") == "LINE"
        and entity.get("instancePath") == [entity.get("definitionHandle")]
        and entity.get("layer") == "冷媒铜管"
    )
    assert independent["source"]["layerInherited"] is False
    assert _inherited_context_route_rejection(independent) is None


def test_inherited_outer_symbol_layer_is_not_a_route():
    context = {
        "instancePath": ["r"],
        "blockName": "arbitrary",
        "childCount": 3124,
        "protectedBy": ["layer"],
        "reason": "opaque_definition_protected_for_expansion",
    }
    leaf = {
        "type": "LINE",
        "instancePath": ["r", "instance-0", "leaf"],
        "source": {
            "originalLayer": "0",
            "layerInherited": True,
            "layerOwnerPath": ["r"],
            "expansionContexts": [context],
        },
    }
    assert _inherited_context_route_rejection(leaf) == (
        "INHERITED_LAYER_CONTEXT_GEOMETRY"
    )


def test_inner_explicit_route_layer_is_not_blanket_excluded():
    context = {"instancePath": ["r"]}
    leaf = {
        "type": "LINE",
        "instancePath": ["r", "instance-0", "pipeinsert", "leaf"],
        "source": {
            "originalLayer": "0",
            "layerInherited": True,
            "layerOwnerPath": ["r", "instance-0", "pipeinsert"],
            "expansionContexts": [context],
        },
    }
    assert _inherited_context_route_rejection(leaf) is None


def _line(identifier, view, span):
    return {
        "id": identifier,
        "type": "LINE",
        "supported": True,
        "source": {"view": view, "bbox": [0, 0, span, 1]},
        "geometry": {},
    }


def test_annotation_radius_has_no_cross_view_dependency():
    model = [_line("m", "model", 126150)]
    radii, _ = _annotation_radii(model)
    both, _ = _annotation_radii(model + [_line("l", "Layout1", 1e84)])
    assert radii["model"] == both["model"] == 4205
    _, audit = _annotation_radii(model + [_line("l", "Layout1", 1e84)])
    assert next(
        item for item in audit if item["view"] == "model"
    )["maxSpanEntityId"] == "m"


@pytest.mark.parametrize(
    "bbox",
    [[0, 0, float("inf"), 1], [10, 0, 1, 2], [0, 0, True, 2], [0, 0, "3", 1]],
)
def test_malformed_bbox_never_becomes_a_contributor(bbox):
    radii, audit = _annotation_radii(
        [
            {
                "id": "bad",
                "type": "LINE",
                "supported": True,
                "source": {"view": "model", "bbox": bbox},
                "geometry": {},
            }
        ]
    )
    assert audit[0]["contributorCount"] == 0
    assert audit[0]["invalidBoundsCount"] == 1
    assert radii["model"] == pytest.approx(1000 / 30, abs=1e-12)


def test_hatch_and_insert_bounds_do_not_set_annotation_radius():
    entities = [
        {
            "id": "hatch",
            "type": "HATCH",
            "supported": True,
            "source": {"view": "model", "bbox": [0, 0, 1e9, 1e9]},
            "geometry": {},
        },
        _line("line", "model", 3000),
    ]
    radii, audit = _annotation_radii(entities)
    assert radii["model"] == 100
    assert audit[0]["excludedBoundsCount"] == 1


def _nested_route(tmp_path, outer_layer, inner_native):
    from hvac_quote.drawing_adapter import inspect_drawing

    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    inner = doc.blocks.new("SERVICE_LINES")
    inner.add_line(
        (0, 0),
        (10000, 0),
        dxfattribs={"layer": "冷凝水管 DN25" if inner_native else "0"},
    )
    outer = doc.blocks.new("ASSEMBLY")
    outer.add_blockref(
        "SERVICE_LINES",
        (0, 0),
        dxfattribs={"layer": "0" if inner_native else "冷凝水管 DN25"},
    )
    doc.modelspace().add_blockref(
        "ASSEMBLY", (0, 0), dxfattribs={"layer": outer_layer}
    )
    path = tmp_path / f"{outer_layer}-{inner_native}.dxf"
    doc.saveas(path)
    return inspect_drawing(path, "nested", tmp_path / "work")


@pytest.mark.parametrize(
    "outer_layer,inner_native",
    [("0", True), ("ACS_KT_LN", True), ("ACS_KT_LN", False)],
)
def test_legitimate_nested_route_is_not_deleted_as_equipment_symbol(
    tmp_path, outer_layer, inner_native
):
    report = _nested_route(tmp_path, outer_layer, inner_native)
    assert report["counts"]["equipment"] == 0
    assert sum(group["count"] for group in report["routeGroups"]) == 1


def test_unrelated_layout_text_does_not_change_model_medium(tmp_path):
    from hvac_quote.drawing_adapter import inspect_drawing

    path = tmp_path / "cross-view-text-grid.dxf"
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    model = doc.modelspace()
    model.add_line((0, 0), (10000, 0))
    model.add_text("DN25", dxfattribs={"height": 20, "insert": (5000, 10)})
    model.add_text("冷凝水", dxfattribs={"height": 20, "insert": (50000, 0)})
    doc.layout("Layout1").add_text(
        "UNRELATED", dxfattribs={"height": 20, "insert": (1e7, 0)}
    )
    doc.saveas(path)
    model_only = inspect_drawing(
        path, "model", tmp_path / "m", scope={"views": ["model"]}
    )
    all_views = inspect_drawing(path, "all", tmp_path / "a")
    assert model_only["routeGroups"][0]["medium"] == all_views["routeGroups"][0][
        "medium"
    ]
