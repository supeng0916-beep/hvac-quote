import json

from fastapi.testclient import TestClient

from hvac_quote.app import create_app


def _login(client: TestClient) -> None:
    client.post(
        "/setup",
        data={
            "display_name": "项目经理",
            "username": "admin",
            "password": "password123",
            "password_confirm": "password123",
        },
    )


def _create_project(client: TestClient, name: str = "示例校园空调工程") -> str:
    response = client.post(
        "/projects",
        data={"name": name, "customer": "示例校园", "note": "长期项目"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


def _fake_quote(app, tmp_path, run_id: str) -> dict:
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    payload = {
        "items": [
            {
                "itemId": "demo-equipment",
                "category": "equipment",
                "spec": "DEMO-UNIT",
                "unit": "套",
            }
        ],
        "priceLines": [
            {
                "itemId": "demo-equipment",
                "templateRow": 6,
                "quantity": "4",
                "baseAmount": "1000.00",
            }
        ],
        "totals": {
            "subtotal": "1000.00",
            "tax": "100.00",
            "total": "1100.00",
        },
        "supplement": {"status": "not_supplied"},
    }
    (run_dir / "quote.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "quote.pdf").write_bytes(b"%PDF-1.4 test")
    (run_dir / "quote.xlsx").write_bytes(b"fake-xlsx")
    return payload


def test_project_create_list_and_delete(tmp_path):
    app = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    client = TestClient(app)
    _login(client)

    project_id = _create_project(client)
    page = client.get("/projects")
    assert page.status_code == 200
    assert "示例校园空调工程" in page.text
    assert "1 份报价" not in page.text
    assert "第一次使用？从这里开始" in page.text
    assert "上传资料" in page.text
    assert "还没有报价" in client.get(f"/projects/{project_id}").text

    run_id = "run-20261019-120000-abcdef"
    _fake_quote(app, tmp_path, run_id)
    app.state.projects.add_quote(
        "admin",
        project_id,
        run_id=run_id,
        slice_key="custom",
        title="演示范围",
        status="ready",
        total="1100.00",
        supplemented=False,
    )

    project_page = client.get(f"/projects/{project_id}")
    assert "演示范围" in project_page.text
    assert "1,100.00" in project_page.text
    assert "已生成" in project_page.text
    assert "项目汇总" in project_page.text
    assert "1 份报价计入" in project_page.text

    excluded = client.post(
        f"/projects/{project_id}/quotes/{run_id}/include",
        data={"included": "0"},
        follow_redirects=True,
    )
    assert excluded.status_code == 200
    assert "0 份报价计入" in excluded.text
    assert "计入汇总" in excluded.text

    result = client.get(f"/projects/{project_id}/quotes/{run_id}")
    assert result.status_code == 200
    assert "1,100.00" in result.text
    assert "本报价未包含" in result.text
    assert "模板行" not in result.text
    assert "row76" not in result.text
    assert "v1 设备" not in result.text

    pdf = client.get(
        f"/projects/{project_id}/quotes/{run_id}/download/quote.pdf"
    )
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")

    denied = client.post(
        f"/projects/{project_id}/delete", data={}, follow_redirects=False
    )
    assert denied.status_code == 400

    deleted = client.post(
        f"/projects/{project_id}/delete",
        data={"confirm": "yes"},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert not (tmp_path / "runs" / run_id).exists()
    assert "还没有项目" in client.get("/projects").text


def test_projects_are_isolated_by_account(tmp_path):
    app = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    alice = app.state.projects.create("alice", "Alice 项目")
    app.state.projects.create("bob", "Bob 项目")
    assert app.state.projects.get("alice", alice.id) is not None
    assert app.state.projects.get("bob", alice.id) is None

    client = TestClient(app)
    _login(client)
    assert "Alice 项目" not in client.get("/projects").text


def test_new_project_requires_name(tmp_path):
    app = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    client = TestClient(app)
    _login(client)

    response = client.post("/projects", data={"name": "  "})
    assert response.status_code == 400
    assert "请填写项目名称" in response.text


def test_project_file_upload_download_and_delete(tmp_path):
    app = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    client = TestClient(app)
    _login(client)
    project_id = _create_project(client)

    page = client.get(f"/projects/{project_id}/files")
    assert page.status_code == 200
    assert "项目资料" in page.text
    assert "还没有上传资料" in page.text
    assert "报价流程" in page.text
    assert "上传只是归档" in page.text

    uploaded = client.post(
        f"/projects/{project_id}/files",
        data={"kind": "drawing"},
        files={
            "upload": (
                "教学楼平面图.dxf",
                b"0\nSECTION\n0\nENDSEC\n0\nEOF\n",
                "application/octet-stream",
            )
        },
        follow_redirects=False,
    )
    assert uploaded.status_code == 303
    assert uploaded.headers["location"] == (
        f"/projects/{project_id}/files?notice=uploaded&uploaded=drawing"
    )

    project = app.state.projects.get("admin", project_id)
    assert project is not None
    assert len(project.files) == 1
    item = project.files[0]
    assert item.original_name == "教学楼平面图.dxf"
    assert item.kind == "drawing"
    assert item.size == len(b"0\nSECTION\n0\nENDSEC\n0\nEOF\n")
    assert (tmp_path / "data" / "projects" / project_id / "files" / item.stored_name).is_file()

    page = client.get(f"/projects/{project_id}/files")
    assert "教学楼平面图.dxf" in page.text
    assert "设计图纸" in page.text

    download = client.get(f"/projects/{project_id}/files/{item.id}")
    assert download.status_code == 200
    assert download.content == b"0\nSECTION\n0\nENDSEC\n0\nEOF\n"
    assert "filename*=utf-8" in download.headers["content-disposition"]

    deleted = client.post(
        f"/projects/{project_id}/files/{item.id}/delete",
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert not (tmp_path / "data" / "projects" / project_id / "files" / item.stored_name).exists()
    assert app.state.projects.get("admin", project_id).files == []


def test_project_file_rejects_wrong_type(tmp_path):
    app = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    client = TestClient(app)
    _login(client)
    project_id = _create_project(client)

    response = client.post(
        f"/projects/{project_id}/files",
        data={"kind": "drawing"},
        files={"upload": ("malware.exe", b"nope", "application/octet-stream")},
    )
    assert response.status_code == 400
    assert "设计图纸支持" in response.text
    assert app.state.projects.get("admin", project_id).files == []


def test_delete_project_removes_uploaded_files(tmp_path):
    app = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    client = TestClient(app)
    _login(client)
    project_id = _create_project(client)

    client.post(
        f"/projects/{project_id}/files",
        data={"kind": "reference"},
        files={"upload": ("说明.pdf", b"%PDF-1.4", "application/pdf")},
        follow_redirects=False,
    )
    project_data = tmp_path / "data" / "projects" / project_id
    assert project_data.is_dir()

    response = client.post(
        f"/projects/{project_id}/delete",
        data={"confirm": "yes"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert not project_data.exists()
