"""Synthetic geometry regressions; customer originals are measured separately in runs/."""
from pathlib import Path

import ezdxf
import pytest

from hvac_quote.drawing_adapter import inspect_drawing


def drawing():
    doc = ezdxf.new()
    doc.header['$INSUNITS'] = 4
    return doc, doc.modelspace()


def note(msp, text, xy):
    return msp.add_text(text, dxfattribs={'height': 30, 'insert': xy})


def triangle(msp, x, y):
    hatch = msp.add_hatch(color=1)
    hatch.paths.add_polyline_path([(x, y), (x + 260, y - 150), (x + 260, y + 150)], is_closed=True)
    return hatch


def inspect(doc, tmp_path):
    path = tmp_path / 'drawing.dxf'
    doc.saveas(path)
    return inspect_drawing(path, 'synthetic', tmp_path / 'work')


def groups(report, handle):
    return [g for g in report['routeGroups'] if handle in g['handles']]


def pieces(report, handle, spec):
    return [s for g in groups(report, handle) if g['spec'] == spec
            for s in g['segments'] if s['handle'] == handle]


def test_native_branch_symbol_projects_inside_segment_instead_of_bend(tmp_path):
    doc, msp = drawing()
    route = msp.add_lwpolyline([(10000, 2000), (10000, 0), (0, 0)],
                             dxfattribs={'layer': '冷媒铜管'})
    symbol = triangle(msp, 9600, 8)
    note(msp, '9.53×22.23', (10010, 1000))
    note(msp, '9.53×15.88', (4000, 10))
    report = inspect(doc, tmp_path)
    gas = pieces(report, route.dxf.handle, '22.23')
    assert sum(s['lengthRaw'] for s in gas) == pytest.approx(2400)
    evidence = gas[0]['boundaryEvidence']
    assert evidence['symbolHandle'] == symbol.dxf.handle
    assert evidence['engineeringApproval'] is False
    assert evidence['apex']['point'] == pytest.approx([9600, 0, 0])
    assert evidence['base']['point'] == pytest.approx([9860, 0, 0])
    assert evidence['apex']['residualDU'] == pytest.approx(8)
    assert all(not g['autoInclude'] for g in groups(report, route.dxf.handle))
    assert sum(s['lengthRaw'] for s in pieces(report, route.dxf.handle, '9.53')) == pytest.approx(12000)
    assert sum(s['lengthRaw'] for spec in ['22.23', '15.88'] for s in pieces(report, route.dxf.handle, spec)) == pytest.approx(12000)
    assert evidence['baseVariant']['beforeLengthRaw'] == pytest.approx(2140)


def test_paired_note_and_native_branch_symbol_prevent_dn_vote_overriding_copper(tmp_path):
    doc, msp = drawing()
    route = msp.add_lwpolyline([(10000, 0), (0, 0), (0, 4000)], dxfattribs={'color': 1})
    triangle(msp, 7000, 4)
    note(msp, '9.53×15.88', (5000, 30))
    for x in [2000, 6000, 9000]:
        note(msp, 'DN25', (x, 100))
    report = inspect(doc, tmp_path)
    candidates = groups(report, route.dxf.handle)
    assert candidates
    assert {g['medium'] for g in candidates} == {'refrigerant'}
    assert {g['spec'] for g in candidates} == {'9.53', '15.88'}
    assert all(not g['autoInclude'] for g in candidates)
    assert any(g['evidence']['nativeBranchSymbols'] for g in candidates)


def test_unconnected_weak_architecture_keeps_unknown_identity_near_pair(tmp_path):
    doc, msp = drawing()
    wall = msp.add_lwpolyline([(0, 0), (15000, 0)], dxfattribs={'color': 252})
    note(msp, '9.53×22.23', (5000, 20))
    report = inspect(doc, tmp_path)
    assert all(g['medium'] == 'unknown' and not g['spec'] for g in groups(report, wall.dxf.handle))
    assert groups(report, wall.dxf.handle) or any(x['handle'] == wall.dxf.handle for x in report['excludedRouteCandidates'])


def test_explicit_drain_near_triangle_stays_drain(tmp_path):
    doc, msp = drawing()
    drain = msp.add_lwpolyline([(10000, 0), (0, 0), (0, 4000)],
                             dxfattribs={'layer': '冷凝水 DN25', 'color': 5})
    triangle(msp, 7000, 4)
    note(msp, '9.53×15.88', (5000, 30))
    assert {g['medium'] for g in groups(inspect(doc, tmp_path), drain.dxf.handle)} == {'condensate'}


def test_weak_wall_cannot_steal_note_from_branch_corroborated_route(tmp_path):
    doc, msp = drawing()
    route = msp.add_lwpolyline([(10000, 2000), (10000, 0), (0, 0)], dxfattribs={'color': 1})
    triangle(msp, 9600, 8)
    wall = msp.add_lwpolyline([(10100, 500), (10100, 1800)], dxfattribs={'color': 252})
    note(msp, '9.53×22.23', (10110, 1000))
    note(msp, '9.53×15.88', (4000, 10))
    report = inspect(doc, tmp_path)
    assert sum(s['lengthRaw'] for s in pieces(report, route.dxf.handle, '22.23')) == pytest.approx(2400)
    assert all(g['medium'] == 'unknown' for g in groups(report, wall.dxf.handle))


def test_multiple_possible_symbols_do_not_assign_one_gas_diameter_to_whole_route(tmp_path):
    doc, msp = drawing()
    route = msp.add_lwpolyline([(10000, 0), (0, 0), (0, 3000)], dxfattribs={'color': 1})
    triangle(msp, 8000, 4)
    triangle(msp, 4000, 4)
    note(msp, '9.53×15.88', (9000, 10))
    note(msp, '9.53×22.23', (1000, 10))
    report = inspect(doc, tmp_path)
    candidates = groups(report, route.dxf.handle)
    assert {g['medium'] for g in candidates} == {'refrigerant'}
    assert sum(s['lengthRaw'] for s in pieces(report, route.dxf.handle, '9.53')) == pytest.approx(13000)
    assert not pieces(report, route.dxf.handle, '15.88')
    assert not pieces(report, route.dxf.handle, '22.23')
    assert any(g['role'] == '气管' and not g['spec'] for g in candidates)
    assert any('PIPE_SEGMENT_BOUNDARY_AMBIGUOUS' in g['reviewReasons'] for g in candidates)


def test_all_along_route_notes_disambiguate_repeated_branch_symbols(tmp_path):
    doc, msp = drawing()
    route = msp.add_lwpolyline([(10000, 0), (0, 0), (0, 3000)], dxfattribs={'color': 1})
    triangle(msp, 8000, 4)
    second = triangle(msp, 4000, 4)
    note(msp, '9.53×15.88', (9000, 10))  # closer to route, but before an unrelated branch
    note(msp, '9.53×15.88', (5000, 50))  # still same specification after first branch
    note(msp, '9.53×22.23', (1000, 10))
    report = inspect(doc, tmp_path)
    gas = pieces(report, route.dxf.handle, '15.88')
    assert sum(s['lengthRaw'] for s in gas) == pytest.approx(6000)
    assert gas[0]['boundaryEvidence']['symbolHandle'] == second.dxf.handle
    assert sum(s['lengthRaw'] for s in pieces(report, route.dxf.handle, '22.23')) == pytest.approx(7000)


def test_native_symbol_port_and_own_pair_resolve_branch_without_counting_symbol_stub(tmp_path):
    doc, msp = drawing()
    trunk = msp.add_lwpolyline([(10000, 0), (0, 0), (0, 3000)], dxfattribs={'color': 1})
    triangle(msp, 7000, 0)
    msp.add_line((7260, 80), (7360, 80))
    branch = msp.add_lwpolyline([(7360, 80), (9000, 80), (9000, 2000)], dxfattribs={'color': 1})
    note(msp, '9.53×15.88', (1000, 10))
    msp.add_lwpolyline([(7400, 150), (8900, 150)], dxfattribs={'color': 5})
    block = doc.blocks.new('MDV-D125Q4')
    block.add_lwpolyline([(-20, -20), (20, -20), (20, 20), (-20, 20)], close=True)
    msp.add_blockref('MDV-D125Q4', (7400, 150))
    note(msp, '9.53×15.88', (8500, 120))
    note(msp, 'DN25', (8900, 180))
    report = inspect(doc, tmp_path)
    candidates = groups(report, branch.dxf.handle)
    assert {g['medium'] for g in candidates} == {'refrigerant'}
    assert {g['spec'] for g in candidates} == {'9.53', '15.88'}
    assert all(not g['autoInclude'] for g in candidates)
    assert all(g['lengthRaw'] == pytest.approx(3560) for g in candidates)
    assert candidates[0]['evidence']['nativeBranchConnections']


def test_short_exact_same_style_dn_branch_is_not_lost_to_minimum_length(tmp_path):
    doc, msp = drawing()
    main = msp.add_lwpolyline([(0, 0), (10000, 0)], dxfattribs={'color': 5})
    branch = msp.add_lwpolyline([(5000, 0), (5000, 100)], dxfattribs={'color': 5})
    block = doc.blocks.new('MDV-D125Q4')
    block.add_lwpolyline([(-50, -50), (50, -50), (50, 50), (-50, 50)], close=True)
    msp.add_blockref('MDV-D125Q4', (0, 0))
    note(msp, 'DN25', (2000, 20))
    note(msp, 'DN25', (3000, 20))
    report = inspect(doc, tmp_path)
    candidates = groups(report, branch.dxf.handle)
    assert candidates
    assert {g['medium'] for g in candidates} == {'drain'}
    assert {g['spec'] for g in candidates} == {'25'}
    assert all(g['lengthRaw'] == pytest.approx(100) and not g['autoInclude'] for g in candidates)
    assert candidates[0]['evidence']['topologyProposal']
