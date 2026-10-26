"""待实现的0.4.8路由验收合同；复制到tests/test_route_evidence_048.py后执行。

这不是本轮通过的测试。基线多项应失败；不得为迁就旧实现删断言。
真实原图另按routes.md第5节回放，合成测试不能代替原图验收。
"""
from copy import deepcopy
from pathlib import Path

import ezdxf
import pytest

from hvac_quote.drawing_adapter import inspect_drawing
from hvac_quote.generic_quote import (
    apply_source_scales,
    build_draft_data,
    draft_exceptions,
    generate_generic_quote,
)


def drawing():
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    return doc, doc.modelspace()


def note(msp, text, xy, height=100):
    return msp.add_text(text, dxfattribs={"height": height, "insert": xy})


def inspect(doc, tmp_path, name="case"):
    source = tmp_path / f"{name}.dxf"
    doc.saveas(source)
    return inspect_drawing(source, name, tmp_path / f"work-{name}")


def groups_for(report, handle):
    return [
        group for group in report["routeGroups"]
        if any(s["handle"] == handle for s in group.get("segments", []))
    ]


def assert_not_automatic(groups):
    assert all(group.get("autoInclude") is False for group in groups)


def test_open_layer_zero_wall_near_pair_does_not_become_automatic(tmp_path):
    doc, msp = drawing()
    wall = msp.add_lwpolyline([(0, 0), (20000, 0), (20000, 6000)])
    note(msp, "9.53×15.88", (8000, 100))
    report = inspect(doc, tmp_path)
    candidates = groups_for(report, wall.dxf.handle)
    assert_not_automatic(candidates)
    audited = [x for x in report["excludedRouteCandidates"] if x["handle"] == wall.dxf.handle]
    assert candidates or audited, "不能静默丢失已召回对象"
    assert any(
        x.get("reviewReasons") or x.get("code")
        for x in candidates + audited
    )


@pytest.mark.parametrize("native_color", [{"color": 252}, {"color": 0},
                                           {"color": 5, "true_color": 0xFF0000}])
def test_explicit_refrigerant_route_is_not_deleted_by_color(tmp_path, native_color):
    doc, msp = drawing()
    line = msp.add_line((0, 0), (20000, 0),
                        dxfattribs={"layer": "冷媒铜管9.53×15.88", **native_color})
    report = inspect(doc, tmp_path)
    groups = groups_for(report, line.dxf.handle)
    assert {g["spec"] for g in groups} == {"9.53", "15.88"}
    assert all(g["medium"] == "refrigerant" and g["autoInclude"] for g in groups)
    assert sorted(g["lengthRaw"] for g in groups) == [20000, 20000]
    ids = [s["physicalSegmentId"] for g in groups for s in g["segments"]]
    assert len(ids) == 2 and len(set(ids)) == 1, "气液共用物理段合法，不能去掉一条"


def test_layer_zero_short_connected_route_keeps_automatic_positive(tmp_path):
    doc, msp = drawing()
    msp.add_line((0, 0), (20000, 0), dxfattribs={"layer": "冷媒铜管9.53×15.88"})
    block = doc.blocks.new("MDV-D125Q4")
    block.add_lwpolyline([(-100, 0), (100, 0), (100, 200), (-100, 200)], close=True)
    msp.add_blockref("MDV-D125Q4", (10000, 300))
    branch = msp.add_line((10000, 0), (10000, 300), dxfattribs={"color": 252})
    note(msp, "9.53×15.88", (10010, 100), height=20)
    groups = groups_for(inspect(doc, tmp_path), branch.dxf.handle)
    assert {g["spec"] for g in groups} == {"9.53", "15.88"}
    assert all(g["autoInclude"] for g in groups), "不能把所有layer0或短管人工化"
    assert all(g["lengthRaw"] == 300 for g in groups)


def test_named_drain_is_not_changed_to_copper_by_closer_pair(tmp_path):
    doc, msp = drawing()
    line = msp.add_line((0, 0), (20000, 0), dxfattribs={"layer": "冷凝水 DN25"})
    note(msp, "9.53×15.88", (8000, 10))
    note(msp, "DN25", (1000, 200))
    groups = groups_for(inspect(doc, tmp_path), line.dxf.handle)
    assert groups
    assert all(g["medium"] in {"drain", "condensate"} for g in groups)
    assert all(g["spec"] == "25" and not g.get("pairSpec") for g in groups)


def test_untyped_conflicting_medium_cannot_choose_the_closest_text(tmp_path):
    doc, msp = drawing()
    line = msp.add_line((0, 0), (20000, 0))
    note(msp, "9.53×15.88", (9000, 20))
    note(msp, "DN25", (1000, 100))
    note(msp, "DN25", (18000, 100))
    groups = groups_for(inspect(doc, tmp_path), line.dxf.handle)
    assert groups
    assert_not_automatic(groups)
    alternatives = [
        alt for g in groups for s in g["segments"]
        for alt in (s.get("evidence") or {}).get("annotationAlternatives", [])
    ]
    assert alternatives, "冲突标注必须保留，不能取最近后丢掉其他证据"


def test_acs_exact_aliases_restore_lines_but_never_circle_length(tmp_path):
    doc, msp = drawing()
    copper = msp.add_line((0, 0), (20000, 0), dxfattribs={"layer": "ACS_DLJ_QYG"})
    drain = msp.add_line((0, 4000), (20000, 4000), dxfattribs={"layer": "ACS_KT_LN"})
    symbol = msp.add_circle((10000, 4000), 100, dxfattribs={"layer": "ACS_KT_LN"})
    wrong = msp.add_line((0, 8000), (20000, 8000), dxfattribs={"layer": "MY_ACS_KT_LN_TEXT"})
    report = inspect(doc, tmp_path)
    assert groups_for(report, copper.dxf.handle)
    assert groups_for(report, drain.dxf.handle)
    assert all(g["medium"] == "refrigerant" for g in groups_for(report, copper.dxf.handle))
    assert all(g["medium"] == "condensate" for g in groups_for(report, drain.dxf.handle))
    assert not groups_for(report, symbol.dxf.handle)
    assert not groups_for(report, wrong.dxf.handle)
    assert any(x["handle"] == symbol.dxf.handle for x in report["excludedRouteCandidates"])
    assert_not_automatic(groups_for(report, copper.dxf.handle))  # 管径未知


def test_dashed_route_positive_and_unanchored_diagonal_negative(tmp_path):
    doc, msp = drawing()
    doc.linetypes.new("01 虚线", dxfattribs={"pattern": [1.0, 0.5, -0.5]})
    attrs = {"linetype": "01 虚线", "ltscale": 200, "color": 136}
    good = msp.add_line((0, 0), (20000, 0),
                        dxfattribs={**attrs, "layer": "冷凝水 DN25"})
    weak = msp.add_line((0, 6000), (20000, 16000), dxfattribs=attrs)
    note(msp, "DN25", (9900, 11000))
    report = inspect(doc, tmp_path)
    assert groups_for(report, good.dxf.handle)
    assert all(g["autoInclude"] for g in groups_for(report, good.dxf.handle))
    assert_not_automatic(groups_for(report, weak.dxf.handle))


def test_inferred_bend_boundary_is_only_a_proposal(tmp_path):
    doc, msp = drawing()
    line = msp.add_lwpolyline([(0, 0), (10000, 0), (10000, 8000)],
                             dxfattribs={"layer": "冷媒铜管"})
    note(msp, "9.53×22.23", (2000, 300), 180)
    note(msp, "9.53×15.88", (10300, 5000), 180)
    groups = groups_for(inspect(doc, tmp_path), line.dxf.handle)
    assert groups
    assert_not_automatic(groups)
    assert any(s.get("boundaryInferred") for g in groups for s in g["segments"])


def test_two_instances_keep_different_ids_and_do_not_silently_deduplicate(tmp_path):
    doc, msp = drawing()
    block = doc.blocks.new("PLAN_COPY")
    block.add_line((0, 0), (20000, 0), dxfattribs={"layer": "冷媒铜管9.53×15.88"})
    refs = [msp.add_blockref("PLAN_COPY", xy) for xy in [(0, 0), (40000, 0)]]
    report = inspect(doc, tmp_path)
    segments = [s for g in report["routeGroups"] for s in g["segments"]]
    assert {s["handle"] for s in segments} == {r.dxf.handle for r in refs}
    assert len({s["entityId"] for s in segments}) == 2
    assert len({tuple(s["instancePath"]) for s in segments}) == 2
    assert len({s["definitionHandle"] for s in segments}) == 1
    assert len({s["sourceRegionKey"] for s in segments}) == 2
    assert len(segments) == 4, "两个实例各两角色，不能仅按definitionHandle或长度去重"


def book():
    return {"book": {"rows": [{"rowNumber": 105, "name": "冷媒铜管", "spec": "Φ9.53",
        "category": "pipe", "unit": "米", "material": "10", "installation": "2",
        "priceState": "priced"}]}}


def inspection_group():
    return {"key": "route|one", "ruleKey": "pipe-9.53", "layer": "0",
        "view": "model", "floorLabel": "model", "category": "冷媒管",
        "medium": "refrigerant", "spec": "9.53", "role": "液管",
        "lengthRaw": 1000, "lengthM": 1, "handles": ["ONE"], "segments": [],
        "issues": ["PIPE_IDENTITY_UNRESOLVED"], "reviewReasons": ["PIPE_IDENTITY_UNRESOLVED"],
        "eligibility": "review", "autoInclude": False, "routeEvidenceKey": "test-evidence-v1"}


def draft_from(group):
    return build_draft_data({"id": "inspection-one", "fileName": "test.dxf", "report": {
        "units": {"metresPerDrawingUnit": "0.001"}, "routeGroups": [group],
        "equipmentGroups": [], "issues": [], "views": ["model"]}}, book())


def test_price_rule_and_scale_cannot_promote_review_route():
    data = draft_from(inspection_group())
    group = data["routes"][0]
    assert group["include"] is False
    apply_source_scales(data, {"inspection-one": "0.002"})
    assert group["include"] is False
    group["include"] = True
    group["manualMaterial"] = "10"
    group["manualInstallation"] = "2"
    assert any(x["code"] == "ROUTE_ADOPTION_UNCONFIRMED" and x["severity"] == "blocks_pricing"
               for x in draft_exceptions(data, book()))


def test_legacy_missing_auto_include_and_evidence_fail_closed():
    legacy = inspection_group()
    for key in ("autoInclude", "routeEvidenceKey", "eligibility"):
        legacy.pop(key)
    data = draft_from(legacy)
    assert data["routes"][0]["include"] is False
    data["routes"][0]["include"] = True
    assert any(x["code"] == "ROUTE_ADOPTION_UNCONFIRMED" for x in draft_exceptions(data, book()))


def test_direct_generate_cannot_bypass_route_review(tmp_path):
    data = draft_from(inspection_group())
    data["routes"][0].update(include=True, manualMaterial="10", manualInstallation="2")
    with pytest.raises(ValueError, match="核对|管线|ROUTE_ADOPTION_UNCONFIRMED"):
        generate_generic_quote(project_name="门禁测试", draft={"data": data},
            template_path=tmp_path / "must-not-be-read.xlsx", out_dir=tmp_path / "out",
            price_book=book())
    assert not list((tmp_path / "out").glob("*.xlsx"))


def test_explicit_review_is_bound_to_unchanged_route_evidence():
    data = draft_from(inspection_group())
    group = data["routes"][0]
    group["include"] = True
    group["routeReview"] = {
        "confirmed": True,
        "evidenceKey": group["routeEvidenceKey"],
        "proposalKey": group["routeProposalKey"],
    }
    assert not any(x["code"] == "ROUTE_ADOPTION_UNCONFIRMED" for x in draft_exceptions(data, book()))
    changed = deepcopy(data)
    changed["routes"][0]["routeEvidenceKey"] = "test-evidence-v2"
    assert any(x["code"] == "ROUTE_ADOPTION_UNCONFIRMED" for x in draft_exceptions(changed, book()))


def _connection_case(tmp_path, strong=None, equipment=False, scope=None, name="case"):
    doc, msp = drawing()
    branch = msp.add_line((10000, 0), (10000, 300), dxfattribs={"color": 252})
    note(msp, "9.53×15.88", (10000.5, 100), height=0.1)
    anchor = None
    if strong:
        space = doc.layouts.get("Layout1") if strong == "other_view" else msp
        layer = (
            "冷凝水 DN25"
            if strong == "other_medium"
            else "冷媒铜管9.53×15.88"
        )
        anchor = space.add_line((0, 0), (20000, 0), dxfattribs={"layer": layer})
    if equipment:
        block = doc.blocks.new("MDV-D125Q4")
        block.add_lwpolyline(
            [(-100, 0), (100, 0), (100, 200), (-100, 200)], close=True
        )
        msp.add_blockref("MDV-D125Q4", (10000, 300))
    path = tmp_path / f"{name}.dxf"
    doc.saveas(path)
    report = inspect_drawing(path, name, tmp_path / f"work-{name}", scope=scope)
    groups = [
        group
        for group in report["routeGroups"]
        if any(
            segment["handle"] == branch.dxf.handle
            for segment in group["segments"]
        )
    ]
    return groups, anchor


def test_cross_view_contact_does_not_upgrade_model_branch(tmp_path):
    groups, _ = _connection_case(
        tmp_path,
        strong="other_view",
        scope={"views": ["model", "Layout1"]},
        name="cross-view",
    )
    assert len(groups) == 2
    assert all(
        group["eligibility"] == "review" and not group["autoInclude"]
        for group in groups
    )


def test_same_view_contact_keeps_short_branch_positive(tmp_path):
    groups, _ = _connection_case(tmp_path, strong="same_view", name="positive")
    assert len(groups) == 2
    assert all(
        group["eligibility"] == "eligible"
        and group["autoInclude"]
        and group["lengthRaw"] == 300
        for group in groups
    )


def test_equipment_bbox_contact_alone_is_not_automatic(tmp_path):
    groups, _ = _connection_case(
        tmp_path, equipment=True, name="bbox-only"
    )
    assert len(groups) == 2
    assert all(not group["autoInclude"] for group in groups)
    assert all(
        any(
            connection["kind"] == "equipment_bbox_contact"
            for segment in group["segments"]
            for connection in segment["evidence"]["connections"]
        )
        for group in groups
    )


def test_cross_medium_contact_is_not_automatic(tmp_path):
    groups, _ = _connection_case(
        tmp_path, strong="other_medium", name="cross-medium"
    )
    assert len(groups) == 2
    assert all(not group["autoInclude"] for group in groups)
    assert any(
        "PIPE_CROSS_MEDIUM_CONTACT" in group["reviewReasons"]
        for group in groups
    )
