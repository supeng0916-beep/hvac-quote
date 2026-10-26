import json

from fastapi.testclient import TestClient

from hvac_quote.app import create_app
from hvac_quote.auth import AuthStore, hash_password, verify_password


def test_password_hash_roundtrip():
    encoded = hash_password("password123")
    assert "password123" not in encoded
    assert verify_password("password123", encoded)
    assert not verify_password("wrong-password", encoded)


def test_first_run_setup_and_login(tmp_path):
    app = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    client = TestClient(app)

    redirect = client.get("/projects", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"].startswith("/login")

    setup = client.get("/setup")
    assert setup.status_code == 200
    assert "创建管理员" in setup.text

    created = client.post(
        "/setup",
        data={
            "display_name": "测试管理员",
            "username": "admin",
            "password": "password123",
            "password_confirm": "password123",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/projects"

    page = client.get("/projects")
    assert page.status_code == 200
    assert "还没有项目" in page.text
    assert "测试管理员" in page.text

    users = app.state.db.query("SELECT * FROM users")
    assert users[0]["username"] == "admin"
    assert "password123" not in users[0]["password_hash"]
    assert (tmp_path / "data" / "hvac.db").is_file()

    duplicate_setup = client.get("/setup", follow_redirects=False)
    assert duplicate_setup.status_code == 303
    assert duplicate_setup.headers["location"] == "/login"


def test_login_rejects_wrong_password_and_logout(tmp_path):
    app = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    app.state.auth.create_user("admin", "password123", "管理员")
    client = TestClient(app)

    wrong = client.post(
        "/login",
        data={"username": "admin", "password": "wrong-pass", "next": "/projects"},
    )
    assert wrong.status_code == 401
    assert "用户名或密码不正确" in wrong.text

    ok = client.post(
        "/login",
        data={"username": "admin", "password": "password123", "next": "/projects"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    assert ok.headers["location"] == "/projects"
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie

    logout = client.post("/logout", follow_redirects=False)
    assert logout.status_code == 303
    assert logout.headers["location"] == "/login"
    assert client.get("/projects", follow_redirects=False).status_code == 303


def test_setup_validation_keeps_values(tmp_path):
    app = create_app(runs_dir=tmp_path / "runs", data_dir=tmp_path / "data")
    client = TestClient(app)

    short = client.post(
        "/setup",
        data={
            "display_name": "管理员",
            "username": "admin",
            "password": "short",
            "password_confirm": "short",
        },
    )
    assert short.status_code == 400
    assert "密码至少 8 位" in short.text

    mismatch = client.post(
        "/setup",
        data={
            "display_name": "管理员",
            "username": "admin",
            "password": "password123",
            "password_confirm": "password456",
        },
    )
    assert mismatch.status_code == 400
    assert "两次输入的密码不一致" in mismatch.text
    assert 'value="admin"' in mismatch.text


def test_session_store_rejects_tampering(tmp_path):
    store = AuthStore(tmp_path)
    user = store.create_user("admin", "password123", "管理员")
    token = store.issue_session(user)
    assert store.read_session(token) == user
    body, signature = token.split(".", 1)
    tampered = body[:-1] + ("A" if body[-1] != "A" else "B") + "." + signature
    assert store.read_session(tampered) is None
    assert store.read_session("not-a-token") is None
    assert store.read_session("!!!.???") is None
