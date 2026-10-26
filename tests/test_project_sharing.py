"""项目成员与共享：可编辑/只读角色、账号管理员全量访问。"""

import pytest
from fastapi.testclient import TestClient

from hvac_quote.app import create_app


def _client(tmp_path):
    app = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    client = TestClient(app)
    client.post(
        "/setup",
        data={
            "display_name": "负责人",
            "username": "boss",
            "password": "password123",
            "password_confirm": "password123",
        },
    )
    app.state.auth.create_user("editor", "password456", "张工")
    app.state.auth.create_user("viewer", "password789", "李老板")
    return app, client


def _login(client: TestClient, username: str, password: str):
    response = client.post(
        "/login",
        data={"username": username, "password": password, "next": "/projects"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        data={"name": "共享测试项目", "customer": "测试客户"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


def test_member_store_roles(tmp_path):
    app, client = _client(tmp_path)
    project_id = _project(client)
    store = app.state.projects

    store.add_member(project_id, "editor", "editor", "boss")
    assert store.role_for("boss", project_id) == "owner"
    assert store.role_for("editor", project_id) == "editor"
    assert store.role_for("viewer", project_id) is None
    members = store.list_members(project_id)
    assert [m["username"] for m in members] == ["editor"]

    with pytest.raises(ValueError):
        store.add_member(project_id, "editor", "editor", "boss")
    with pytest.raises(ValueError):
        store.add_member("prj-not-exist", "editor", "editor", "boss")

    assert store.remove_member(project_id, "editor") is True
    assert store.role_for("editor", project_id) is None
    assert store.remove_member(project_id, "editor") is False


def test_owner_adds_editor_and_viewer(tmp_path):
    app, client = _client(tmp_path)
    project_id = _project(client)

    page = client.get(f"/projects/{project_id}")
    assert "项目成员" in page.text

    added_editor = client.post(
        f"/projects/{project_id}/members",
        data={"username": "editor", "role": "editor"},
        follow_redirects=False,
    )
    assert added_editor.status_code == 303
    added_viewer = client.post(
        f"/projects/{project_id}/members",
        data={"username": "viewer", "role": "viewer"},
        follow_redirects=False,
    )
    assert added_viewer.status_code == 303

    unknown = client.post(
        f"/projects/{project_id}/members",
        data={"username": "nobody", "role": "viewer"},
    )
    assert unknown.status_code == 400

    _login(client, "editor", "password456")
    overview = client.get("/projects")
    assert overview.status_code == 200
    assert "共享测试项目" in overview.text
    assert "共享" in overview.text
    assert client.get(f"/projects/{project_id}").status_code == 200

    _login(client, "viewer", "password789")
    assert client.get(f"/projects/{project_id}").status_code == 200
    blocked = client.post(
        f"/projects/{project_id}/files",
        data={"kind": "drawing"},
        files={"upload": ("一层.dxf", b"0\nEOF\n", "application/dxf")},
    )
    assert blocked.status_code == 403
    assert "只读" in blocked.text


def test_non_member_gets_404(tmp_path):
    app, client = _client(tmp_path)
    project_id = _project(client)

    _login(client, "editor", "password456")
    assert client.get(f"/projects/{project_id}").status_code == 404


def test_admin_does_not_bypass_project_isolation(tmp_path):
    """账号间严格隔离是既有验收契约：管理员也不例外，需要显式加为成员。"""
    app, client = _client(tmp_path)
    _login(client, "editor", "password456")
    response = client.post(
        "/projects",
        data={"name": "编辑者的项目", "customer": "测试客户"},
        follow_redirects=False,
    )
    project_id = response.headers["location"].rsplit("/", 1)[-1]

    _login(client, "boss", "password123")
    assert client.get(f"/projects/{project_id}").status_code == 404
    assert "编辑者的项目" not in client.get("/projects").text

    # 所有者把管理员显式加为成员后才能访问。
    _login(client, "editor", "password456")
    client.post(
        f"/projects/{project_id}/members",
        data={"username": "boss", "role": "viewer"},
        follow_redirects=False,
    )
    _login(client, "boss", "password123")
    assert client.get(f"/projects/{project_id}").status_code == 200
    blocked = client.post(
        f"/projects/{project_id}/members",
        data={"username": "viewer", "role": "viewer"},
    )
    assert blocked.status_code == 403


def test_member_removed_loses_access(tmp_path):
    app, client = _client(tmp_path)
    project_id = _project(client)
    added = client.post(
        f"/projects/{project_id}/members",
        data={"username": "editor", "role": "editor"},
        follow_redirects=False,
    )
    assert added.status_code == 303

    removed = client.post(
        f"/projects/{project_id}/members/editor/delete",
        follow_redirects=False,
    )
    assert removed.status_code == 303

    _login(client, "editor", "password456")
    assert client.get(f"/projects/{project_id}").status_code == 404

    actions = {
        str(row["action"])
        for row in app.state.db.query("SELECT action FROM audit_logs")
    }
    assert "project_member_added" in actions
    assert "project_member_removed" in actions


def test_regular_member_cannot_manage_members(tmp_path):
    app, client = _client(tmp_path)
    project_id = _project(client)
    app.state.projects.add_member(project_id, "editor", "editor", "boss")

    _login(client, "editor", "password456")
    response = client.post(
        f"/projects/{project_id}/members",
        data={"username": "viewer", "role": "viewer"},
    )
    assert response.status_code == 403
