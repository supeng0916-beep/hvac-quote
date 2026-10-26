"""价格书行编辑/删除、价格书删除、草稿复制/删除、报价记录删除。"""

import pytest
from fastapi.testclient import TestClient

from hvac_quote.app import create_app


def _client(tmp_path) -> tuple:
    app = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    client = TestClient(app)
    client.post(
        "/setup",
        data={
            "display_name": "管理员",
            "username": "admin",
            "password": "password123",
            "password_confirm": "password123",
        },
    )
    return app, client


def _project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        data={"name": "管理测试项目", "customer": "测试客户"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


def _price_book(app, project_id: str) -> dict:
    db = app.state.db
    db.execute(
        "INSERT INTO project_files(id, project_id, original_name, stored_name, "
        "kind, size, sha256, uploaded_at) VALUES(?,?,?,?,?,?,?,?)",
        ("file-1", project_id, "模板.xlsx", "stored.xlsx", "template", 100, "abc", "2026-10-21T00:00:00"),
    )
    db.execute(
        "INSERT INTO drawing_inspections(id, project_id, file_id, status, "
        "report_json, created_at) VALUES(?,?,?,?,?,?)",
        ("insp-1", project_id, "file-1", "done", "{}", "2026-10-21T00:00:00"),
    )
    return app.state.new_quotes.save_price_book(
        project_id,
        "file-1",
        "测试价格书",
        {
            "rows": [
                {
                    "rowNumber": 1,
                    "index": "1",
                    "name": "室内机",
                    "spec": "DEMO-UNIT-01",
                    "unit": "台",
                    "material": "1200",
                    "installation": "100",
                    "category": "equipment",
                    "priceState": "priced",
                },
                {
                    "rowNumber": 2,
                    "index": "项目价",
                    "name": "补充泵",
                    "spec": "P-1",
                    "unit": "台",
                    "material": "1000",
                    "installation": "100",
                    "category": "equipment",
                    "priceState": "priced",
                    "projectPrice": True,
                },
            ],
            "columns": {
                "name": "B",
                "spec": "C",
                "unit": "D",
                "material": "G",
                "installation": "H",
            },
        },
    )


def _draft(app, project_id: str, book: dict) -> dict:
    return app.state.new_quotes.create_draft(
        project_id, "insp-1", book["id"], {"title": "测试草稿"}
    )


def test_price_book_row_edit_persists(tmp_path):
    app, client = _client(tmp_path)
    project_id = _project(client)
    book = _price_book(app, project_id)
    store = app.state.new_quotes

    store.update_price_book_row(
        project_id, book["id"], 1, material="4000", installation="500", unit="套"
    )
    rows = {
        int(row["rowNumber"]): row
        for row in store.get_price_book(project_id, book["id"])["book"]["rows"]
    }
    assert rows[1]["material"] == "4000"
    assert rows[1]["installation"] == "500"
    assert rows[1]["unit"] == "套"

    with pytest.raises(ValueError):
        store.update_price_book_row(
            project_id, book["id"], 99, material="1", installation="1", unit="台"
        )


def test_price_book_row_delete_only_for_project_price(tmp_path):
    app, client = _client(tmp_path)
    project_id = _project(client)
    book = _price_book(app, project_id)
    store = app.state.new_quotes

    store.delete_price_book_row(project_id, book["id"], 2)
    numbers = [
        int(row["rowNumber"])
        for row in store.get_price_book(project_id, book["id"])["book"]["rows"]
    ]
    assert numbers == [1]

    with pytest.raises(ValueError):
        store.delete_price_book_row(project_id, book["id"], 1)


def test_price_book_delete_blocked_by_draft(tmp_path):
    app, client = _client(tmp_path)
    project_id = _project(client)
    book = _price_book(app, project_id)
    store = app.state.new_quotes
    _draft(app, project_id, book)

    with pytest.raises(ValueError) as exc:
        store.delete_price_book(project_id, book["id"])
    assert "草稿" in str(exc.value)

    store.delete_draft(project_id, store.list_drafts(project_id)[0]["id"])
    store.delete_price_book(project_id, book["id"])
    assert store.get_price_book(project_id, book["id"]) is None


def test_draft_copy_and_delete(tmp_path):
    app, client = _client(tmp_path)
    project_id = _project(client)
    book = _price_book(app, project_id)
    store = app.state.new_quotes
    draft = _draft(app, project_id, book)

    copied = store.copy_draft(project_id, draft["id"])
    assert copied["id"] != draft["id"]
    assert copied["data"]["title"] == "测试草稿（副本）"
    assert len(store.list_drafts(project_id)) == 2

    store.delete_draft(project_id, copied["id"])
    assert len(store.list_drafts(project_id)) == 1
    assert store.delete_draft(project_id, copied["id"]) is False


def test_app_price_book_management_routes(tmp_path):
    app, client = _client(tmp_path)
    project_id = _project(client)
    book = _price_book(app, project_id)

    edit = client.post(
        f"/projects/{project_id}/price-books/{book['id']}/rows/1",
        data={"material": "4100", "installation": "510", "unit": "台"},
        follow_redirects=False,
    )
    assert edit.status_code == 303
    page = client.get(f"/projects/{project_id}/price-books/{book['id']}")
    assert "4100" in page.text

    imported_delete = client.post(
        f"/projects/{project_id}/price-books/{book['id']}/rows/1/delete"
    )
    assert imported_delete.status_code == 400

    row_delete = client.post(
        f"/projects/{project_id}/price-books/{book['id']}/rows/2/delete",
        follow_redirects=False,
    )
    assert row_delete.status_code == 303

    book_delete = client.post(
        f"/projects/{project_id}/price-books/{book['id']}/delete",
        follow_redirects=False,
    )
    assert book_delete.status_code == 303
    assert app.state.new_quotes.get_price_book(project_id, book["id"]) is None

    actions = {
        str(row["action"]) for row in app.state.db.query("SELECT action FROM audit_logs")
    }
    assert {"price_book_row_updated", "price_book_row_deleted", "price_book_deleted"} <= actions


def test_app_price_book_delete_blocked_with_draft(tmp_path):
    app, client = _client(tmp_path)
    project_id = _project(client)
    book = _price_book(app, project_id)
    _draft(app, project_id, book)

    blocked = client.post(
        f"/projects/{project_id}/price-books/{book['id']}/delete"
    )
    assert blocked.status_code == 400
    assert "草稿" in blocked.text
    assert app.state.new_quotes.get_price_book(project_id, book["id"]) is not None


def test_app_draft_copy_delete_routes(tmp_path):
    app, client = _client(tmp_path)
    project_id = _project(client)
    book = _price_book(app, project_id)
    draft = _draft(app, project_id, book)

    copied = client.post(
        f"/projects/{project_id}/drafts/{draft['id']}/copy",
        follow_redirects=False,
    )
    assert copied.status_code == 303
    new_id = copied.headers["location"].rsplit("/", 1)[-1]
    assert len(app.state.new_quotes.list_drafts(project_id)) == 2

    copied_revision = app.state.new_quotes.get_draft(
        project_id, new_id
    )["revision"]
    deleted = client.post(
        f"/projects/{project_id}/drafts/{new_id}/delete",
        data={"expected_revision": str(copied_revision)},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert len(app.state.new_quotes.list_drafts(project_id)) == 1

    actions = {
        str(row["action"]) for row in app.state.db.query("SELECT action FROM audit_logs")
    }
    assert {"draft_copied", "draft_deleted"} <= actions


def test_app_quote_delete_route_removes_record_and_run_dir(tmp_path):
    app, client = _client(tmp_path)
    project_id = _project(client)
    run_id = "run-20261021-120000-abc123"
    app.state.projects.add_quote(
        "admin",
        project_id,
        run_id=run_id,
        slice_key="custom",
        title="测试报价",
        status="ready",
        total="100.00",
        supplemented=False,
    )
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "quote.json").write_text("{}", encoding="utf-8")

    deleted = client.post(
        f"/projects/{project_id}/quotes/{run_id}/delete",
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    page = client.get(f"/projects/{project_id}")
    assert "测试报价" not in page.text
    assert not run_dir.exists()

    actions = {
        str(row["action"]) for row in app.state.db.query("SELECT action FROM audit_logs")
    }
    assert "quote_deleted" in actions
