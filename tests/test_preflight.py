"""R01：大图预检、范围选择、上下文块折叠与检查版本。"""

from pathlib import Path

import ezdxf
from ezdxf.enums import TextEntityAlignment
from fastapi.testclient import TestClient

from hvac_quote.app import create_app
from hvac_quote.drawing_adapter import INSPECTION_ENGINE_VERSION, inspect_drawing


def _text(msp, content, x, y):
    entity = msp.add_text(content, height=120)
    entity.set_placement((x, y), align=TextEntityAlignment.LEFT)


def _large_dxf(path: Path) -> Path:
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    wall = doc.blocks.new("WALL_CONTEXT")
    for index in range(40):
        wall.add_line((0, index * 10), (500, index * 10))
    equipment = doc.blocks.new("RMX112C")
    equipment.add_lwpolyline([(0, 0), (500, 0), (500, 400), (0, 400)], close=True)
    for index in range(5):
        msp.add_blockref(
            "WALL_CONTEXT", (index * 3000, 0), dxfattribs={"layer": "B-墙体"}
        )
    msp.add_blockref("RMX112C", (0, 3000), dxfattribs={"layer": "Daikin-AC"})
    _text(msp, "MDV-200W/DEMO", 700, 3200)
    doc.saveas(path)
    return path


def _client(tmp_path):
    app = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    client = TestClient(app)
    client.post(
        "/setup",
        data={
            "display_name": "预检",
            "username": "admin",
            "password": "password123",
            "password_confirm": "password123",
        },
    )
    return app, client


def _project_with_drawing(tmp_path):
    app, client = _client(tmp_path)
    project_id = client.post(
        "/projects",
        data={"name": "预检项目", "customer": "测试"},
        follow_redirects=False,
    ).headers["location"].rsplit("/", 1)[-1]
    path = _large_dxf(tmp_path / "large.dxf")
    client.post(
        f"/projects/{project_id}/files",
        data={"kind": "drawing"},
        files={"upload": ("large.dxf", path.read_bytes(), "application/octet-stream")},
        follow_redirects=False,
    )
    project = app.state.projects.get("admin", project_id)
    drawing = next(item for item in project.files if item.kind == "drawing")
    return app, client, project_id, drawing


def test_context_blocks_are_collapsed_and_equipment_survives(tmp_path):
    path = _large_dxf(tmp_path / "large.dxf")
    report = inspect_drawing(path, "large", tmp_path / "work")
    assert report["engineVersion"] == INSPECTION_ENGINE_VERSION
    assert report["counts"]["equipment"] == 1
    assert report["diagnostics"].get("opaqueBlocks")
    assert any(
        item["code"] == "CONTEXT_BLOCK_COLLAPSED" for item in report["issues"]
    )


def test_preflight_page_and_scoped_inspection(tmp_path):
    app, client, project_id, drawing = _project_with_drawing(tmp_path)
    preflight = client.post(
        f"/projects/{project_id}/files/{drawing.id}/preflight"
    )
    assert preflight.status_code == 200
    assert "图纸预检" in preflight.text
    assert "折叠后估算" in preflight.text
    assert "WALL_CONTEXT" in preflight.text

    inspected = client.post(
        f"/projects/{project_id}/files/{drawing.id}/inspect",
        data={"views": "model"},
        follow_redirects=False,
    )
    assert inspected.status_code == 303
    inspection_id = inspected.headers["location"].rsplit("/", 1)[-1]
    report = app.state.new_quotes.get_inspection(
        project_id, inspection_id
    )["report"]
    assert report["engineVersion"] == INSPECTION_ENGINE_VERSION

    # 同版本再次检查应复用；强制重检应建立新记录。
    again = client.post(
        f"/projects/{project_id}/files/{drawing.id}/inspect",
        follow_redirects=False,
    )
    assert again.status_code == 303
    assert "already-inspected" in again.headers["location"]

    forced = client.post(
        f"/projects/{project_id}/files/{drawing.id}/inspect",
        data={"force": "1"},
        follow_redirects=False,
    )
    assert forced.status_code == 303
    second_id = forced.headers["location"].rsplit("/", 1)[-1]
    assert second_id != inspection_id
    assert len(app.state.new_quotes.list_inspections(project_id)) == 2
