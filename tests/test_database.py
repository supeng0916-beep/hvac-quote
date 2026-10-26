import json

from fastapi.testclient import TestClient

from hvac_quote.app import create_app
from hvac_quote.auth import hash_password


def _setup(client: TestClient) -> None:
    client.post(
        "/setup",
        data={
            "display_name": "数据库管理员",
            "username": "admin",
            "password": "password123",
            "password_confirm": "password123",
        },
    )


def test_sqlite_persists_accounts_projects_and_audit(tmp_path):
    app1 = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    client1 = TestClient(app1)
    _setup(client1)
    project_id = client1.post(
        "/projects", data={"name": "持久化项目"}, follow_redirects=False
    ).headers["location"].rsplit("/", 1)[-1]
    assert (tmp_path / "data" / "hvac.db").is_file()

    app2 = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    client2 = TestClient(app2)
    login = client2.post(
        "/login",
        data={"username": "admin", "password": "password123"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    page = client2.get("/projects")
    assert "持久化项目" in page.text
    assert project_id in page.text

    actions = {
        row["action"]
        for row in app2.state.db.query("SELECT action FROM audit_logs")
    }
    assert {"setup", "login_success", "project_created"} <= actions


def test_legacy_json_is_migrated_once(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "users.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "users": [
                    {
                        "username": "legacy",
                        "displayName": "旧账号",
                        "passwordHash": hash_password("password123"),
                        "createdAt": "2026-10-01T10:00:00+08:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (data / "projects.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "projects": [
                    {
                        "id": "prj-20261001-100000-abcdef",
                        "owner": "legacy",
                        "name": "旧 JSON 项目",
                        "customer": "旧客户",
                        "note": "",
                        "createdAt": "2026-10-01T10:00:00+08:00",
                        "updatedAt": "2026-10-01T10:00:00+08:00",
                        "quotes": [],
                        "files": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    app = create_app(runs_dir=tmp_path / "runs", data_dir=data)
    client = TestClient(app)
    assert app.state.auth.get("legacy") is not None
    assert app.state.projects.get("legacy", "prj-20261001-100000-abcdef") is not None
    login = client.post(
        "/login",
        data={"username": "legacy", "password": "password123"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert "旧 JSON 项目" in client.get("/projects").text


def test_project_delete_cascades_quotes_and_files(tmp_path):
    app = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    client = TestClient(app)
    _setup(client)
    project_id = client.post(
        "/projects", data={"name": "级联删除项目"}, follow_redirects=False
    ).headers["location"].rsplit("/", 1)[-1]
    app.state.projects.add_quote(
        "admin",
        project_id,
        run_id="run-20261019-120000-abcdef",
        slice_key="custom",
        title="教学楼首层",
        status="ready",
        total="1.00",
        supplemented=False,
    )
    app.state.projects.add_file(
        "admin",
        project_id,
        original_name="plan.dwg",
        stored_name="file-test.dwg",
        kind="drawing",
        size=12,
        sha256="a" * 64,
    )
    response = client.post(
        f"/projects/{project_id}/delete",
        data={"confirm": "yes"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert app.state.db.scalar("SELECT COUNT(*) FROM projects") == 0
    assert app.state.db.scalar("SELECT COUNT(*) FROM quotes") == 0
    assert app.state.db.scalar("SELECT COUNT(*) FROM project_files") == 0
    actions = {
        row["action"]
        for row in app.state.db.query("SELECT action FROM audit_logs")
    }
    assert "project_deleted" in actions
