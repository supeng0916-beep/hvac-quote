"""账号管理：自助改密、管理员建号/重置密码、首个账号为管理员。"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from hvac_quote.app import create_app
from hvac_quote.auth import AuthError, AuthStore
from hvac_quote.db import Database


def _make_app(tmp_path):
    app = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    return app, TestClient(app)


def _setup_admin(client: TestClient) -> None:
    response = client.post(
        "/setup",
        data={
            "display_name": "管理员",
            "username": "admin",
            "password": "password123",
            "password_confirm": "password123",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/login",
        data={"username": username, "password": password, "next": "/projects"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _actions(app) -> set[str]:
    rows = app.state.db.query("SELECT action FROM audit_logs")
    return {str(row["action"]) for row in rows}


def test_first_created_user_is_admin(tmp_path):
    store = AuthStore(tmp_path)
    first = store.create_user("admin", "password123", "管理员")
    second = store.create_user("bob", "password456", "报价员")
    assert first.is_admin is True
    assert second.is_admin is False
    assert store.get("admin").is_admin is True
    assert store.get("bob").is_admin is False


def test_set_password_updates_hash_and_validates(tmp_path):
    store = AuthStore(tmp_path)
    store.create_user("admin", "password123", "管理员")

    store.set_password("admin", "new-password456")
    assert store.verify("admin", "password123") is None
    assert store.verify("admin", "new-password456") is not None

    with pytest.raises(AuthError):
        store.set_password("nobody", "new-password456")
    with pytest.raises(AuthError):
        store.set_password("admin", "short")


def test_existing_database_promotes_earliest_user_to_admin(tmp_path):
    path = tmp_path / "hvac.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE users (
            username TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO users VALUES('bob', '报价员', 'x', '2026-01-02T00:00:00');
        INSERT INTO users VALUES('alice', '负责人', 'x', '2026-01-01T00:00:00');
        """
    )
    legacy.commit()
    legacy.close()

    db = Database(path)
    rows = {
        str(row["username"]): int(row["is_admin"])
        for row in db.query("SELECT username, is_admin FROM users")
    }
    assert rows == {"alice": 1, "bob": 0}


def test_account_self_password_change(tmp_path):
    app, client = _make_app(tmp_path)
    _setup_admin(client)

    page = client.get("/account")
    assert page.status_code == 200
    assert "修改密码" in page.text

    wrong = client.post(
        "/account",
        data={
            "current_password": "wrong-pass",
            "new_password": "new-password456",
            "password_confirm": "new-password456",
        },
    )
    assert wrong.status_code == 400
    assert "当前密码不正确" in wrong.text

    mismatch = client.post(
        "/account",
        data={
            "current_password": "password123",
            "new_password": "new-password456",
            "password_confirm": "other-password",
        },
    )
    assert mismatch.status_code == 400
    assert "两次输入的密码不一致" in mismatch.text

    changed = client.post(
        "/account",
        data={
            "current_password": "password123",
            "new_password": "new-password456",
            "password_confirm": "new-password456",
        },
        follow_redirects=False,
    )
    assert changed.status_code == 303

    failed = client.post(
        "/login", data={"username": "admin", "password": "password123"}
    )
    assert failed.status_code == 401
    _login(client, "admin", "new-password456")
    assert "password_changed" in _actions(app)


def test_admin_pages_hidden_from_regular_users(tmp_path):
    app, client = _make_app(tmp_path)
    _setup_admin(client)
    app.state.auth.create_user("bob", "password456", "报价员")
    _login(client, "bob", "password456")

    assert client.get("/admin/users").status_code == 404
    assert (
        client.post(
            "/admin/users",
            data={
                "username": "eve",
                "display_name": "测试",
                "password": "password789",
                "password_confirm": "password789",
            },
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/admin/users/bob/password",
            data={"new_password": "hacked-pass1", "password_confirm": "hacked-pass1"},
        ).status_code
        == 404
    )


def test_admin_creates_user_and_resets_password(tmp_path):
    app, client = _make_app(tmp_path)
    _setup_admin(client)

    page = client.get("/admin/users")
    assert page.status_code == 200
    assert "新增账号" in page.text

    created = client.post(
        "/admin/users",
        data={
            "username": "yang",
            "display_name": "杨工",
            "password": "password789",
            "password_confirm": "password789",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    duplicate = client.post(
        "/admin/users",
        data={
            "username": "yang",
            "display_name": "杨工",
            "password": "password789",
            "password_confirm": "password789",
        },
    )
    assert duplicate.status_code == 400
    assert "该用户名已存在" in duplicate.text

    # 新账号由 CLI/管理员创建后立即可登录，无需重启服务。
    _login(client, "yang", "password789")

    _login(client, "admin", "password123")
    reset = client.post(
        "/admin/users/yang/password",
        data={
            "new_password": "reset-pass123",
            "password_confirm": "reset-pass123",
        },
        follow_redirects=False,
    )
    assert reset.status_code == 303

    stale = client.post(
        "/login", data={"username": "yang", "password": "password789"}
    )
    assert stale.status_code == 401
    _login(client, "yang", "reset-pass123")

    actions = _actions(app)
    assert "user_created" in actions
    assert "password_reset" in actions


def test_cli_user_set_password(tmp_path, capsys, monkeypatch):
    from hvac_quote import cli

    store = AuthStore(tmp_path)
    store.create_user("admin", "password123", "负责人")

    rc = cli.main(
        [
            "user-set-password",
            "--username",
            "admin",
            "--password",
            "new-password456",
            "--data",
            str(tmp_path),
        ]
    )
    assert rc == 0
    assert AuthStore(tmp_path).verify("admin", "new-password456") is not None

    rc = cli.main(
        [
            "user-set-password",
            "--username",
            "nobody",
            "--password",
            "new-password456",
            "--data",
            str(tmp_path),
        ]
    )
    assert rc == 2

    rc = cli.main(["user-list", "--data", str(tmp_path)])
    assert rc == 0
    assert "负责人（管理员）" in capsys.readouterr().out
