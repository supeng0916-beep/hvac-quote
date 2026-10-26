"""本地账号与会话。

口令只在本机 SQLite 保存 PBKDF2 哈希；会话是 HMAC 签名的 HttpOnly Cookie。
首次启动由用户创建管理员，没有默认口令，也没有假装登录的演示账号。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .db import Database

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
PASSWORD_MIN_LENGTH = 8
PBKDF2_ITERATIONS = 600_000
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
COOKIE_NAME = "hvac_session"


class AuthError(ValueError):
    """可直接展示给用户的账号错误。"""


@dataclass(frozen=True)
class User:
    username: str
    display_name: str
    password_hash: str
    created_at: str
    is_admin: bool = False


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return (
        f"pbkdf2_sha256${PBKDF2_ITERATIONS}$"
        f"{_b64encode(salt)}${_b64encode(digest)}"
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = _b64decode(salt_text)
        expected = _b64decode(digest_text)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(actual, expected)


def validate_username(username: str) -> str:
    text = username.strip()
    if not USERNAME_RE.fullmatch(text):
        raise AuthError("用户名需为 2-32 位字母、数字、点、下划线或短横线")
    return text


def validate_password(password: str) -> None:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise AuthError(f"密码至少 {PASSWORD_MIN_LENGTH} 位")
    if len(password) > 128:
        raise AuthError("密码不能超过 128 位")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


class AuthStore:
    """账号和会话签名密钥都放在本机数据目录。"""

    def __init__(self, data_dir: Path, db: Database | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = db or Database(self.data_dir / "hvac.db")
        self.key_path = self.data_dir / "session.key"
        self.legacy_users_path = self.data_dir / "users.json"
        self._import_legacy_users()

    def _import_legacy_users(self) -> None:
        if int(self.db.scalar("SELECT COUNT(*) FROM users") or 0) > 0:
            return
        if not self.legacy_users_path.is_file():
            return
        try:
            payload = json.loads(
                self.legacy_users_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return
        rows = []
        for row in payload.get("users", []):
            try:
                username = validate_username(str(row["username"]))
                display_name = str(row["displayName"]).strip()
                password_hash = str(row["passwordHash"])
                created_at = str(row["createdAt"])
            except (KeyError, TypeError, AuthError):
                continue
            if display_name and password_hash:
                rows.append(
                    (username, display_name, password_hash, created_at)
                )
        if not rows:
            return
        with self.db.transaction() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO users"
                "(username, display_name, password_hash, created_at) "
                "VALUES(?,?,?,?)",
                rows,
            )
        self._ensure_admin_exists()

    def _ensure_admin_exists(self) -> None:
        """没有任何管理员时，把最早创建的账号提升为管理员。"""
        if self.db.scalar("SELECT 1 FROM users WHERE is_admin=1 LIMIT 1"):
            return
        earliest = self.db.one(
            "SELECT username FROM users ORDER BY created_at ASC, rowid ASC "
            "LIMIT 1"
        )
        if earliest is not None:
            self.db.execute(
                "UPDATE users SET is_admin=1 WHERE username=?",
                (earliest["username"],),
            )

    def has_users(self) -> bool:
        return int(self.db.scalar("SELECT COUNT(*) FROM users") or 0) > 0

    def list_users(self) -> tuple[User, ...]:
        rows = self.db.query(
            "SELECT * FROM users ORDER BY lower(username)"
        )
        return tuple(self._user(row) for row in rows)

    def get(self, username: str) -> User | None:
        row = self.db.one(
            "SELECT * FROM users WHERE lower(username)=lower(?)",
            (username.strip(),),
        )
        return self._user(row) if row is not None else None

    @staticmethod
    def _user(row: sqlite3.Row) -> User:
        return User(
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            password_hash=str(row["password_hash"]),
            created_at=str(row["created_at"]),
            is_admin=bool(row["is_admin"]),
        )

    def create_user(
        self, username: str, password: str, display_name: str
    ) -> User:
        username = validate_username(username)
        validate_password(password)
        display_name = display_name.strip()
        if not display_name:
            raise AuthError("请填写显示名称")
        if len(display_name) > 40:
            raise AuthError("显示名称不能超过 40 个字符")
        user = User(
            username=username,
            display_name=display_name,
            password_hash=hash_password(password),
            created_at=_now_text(),
            # 首个账号即管理员；auth.py 模块文档约定的最小管理员闭环。
            is_admin=not self.has_users(),
        )
        try:
            self.db.execute(
                "INSERT INTO users(username, display_name, password_hash, "
                "created_at, is_admin) VALUES(?,?,?,?,?)",
                (
                    user.username,
                    user.display_name,
                    user.password_hash,
                    user.created_at,
                    int(user.is_admin),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise AuthError("该用户名已存在") from exc
        return user

    def set_password(self, username: str, new_password: str) -> User:
        """管理员重置或本人修改密码；找不到用户时按可展示错误处理。"""
        if self.get(username) is None:
            raise AuthError("用户不存在")
        validate_password(new_password)
        self.db.execute(
            "UPDATE users SET password_hash=? WHERE lower(username)=lower(?)",
            (hash_password(new_password), username.strip()),
        )
        updated = self.get(username)
        if updated is None:  # pragma: no cover - 刚更新过的行必然存在
            raise AuthError("用户不存在")
        return updated

    def verify(self, username: str, password: str) -> User | None:
        user = self.get(username)
        if user is None:
            # 与已知用户保持接近的计算成本，避免用户名被时间差探测。
            hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                b"hvac-unknown-user",
                PBKDF2_ITERATIONS,
            )
            return None
        return user if verify_password(password, user.password_hash) else None

    def _secret(self) -> bytes:
        if self.key_path.is_file():
            text = self.key_path.read_text(encoding="ascii").strip()
            return bytes.fromhex(text)
        secret = secrets.token_bytes(32)
        _atomic_write(self.key_path, secret.hex())
        return secret

    def issue_session(self, user: User, now: float | None = None) -> str:
        issued = time.time() if now is None else now
        payload = {
            "u": user.username,
            "exp": int(issued + SESSION_TTL_SECONDS),
        }
        body = _b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        )
        signature = hmac.new(
            self._secret(), body.encode("ascii"), hashlib.sha256
        ).digest()
        return f"{body}.{_b64encode(signature)}"

    def read_session(self, token: str | None) -> User | None:
        if not token or len(token) > 4096:
            return None
        try:
            body, signature_text = token.split(".", 1)
            expected = hmac.new(
                self._secret(), body.encode("ascii"), hashlib.sha256
            ).digest()
            if not hmac.compare_digest(expected, _b64decode(signature_text)):
                return None
            payload = json.loads(_b64decode(body))
            if int(payload["exp"]) < int(time.time()):
                return None
            username = str(payload["u"])
        except (
            ValueError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
            binascii.Error,
        ):
            return None
        return self.get(username)


class LoginThrottle:
    """本机内存限速：同一用户名/来源连续失败后短暂锁定。"""

    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: int = 300,
        lock_seconds: int = 60,
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.lock_seconds = lock_seconds
        from threading import Lock

        self._lock = Lock()
        self._events: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def retry_after(self, key: str) -> int:
        now = time.time()
        with self._lock:
            locked = self._locked_until.get(key, 0)
            if locked > now:
                return int(locked - now) + 1
            if locked:
                self._locked_until.pop(key, None)
            events = [
                item
                for item in self._events.get(key, [])
                if now - item < self.window_seconds
            ]
            self._events[key] = events
            return 0

    def fail(self, key: str) -> None:
        now = time.time()
        with self._lock:
            events = [
                item
                for item in self._events.get(key, [])
                if now - item < self.window_seconds
            ]
            events.append(now)
            self._events[key] = events
            if len(events) >= self.max_failures:
                self._locked_until[key] = now + self.lock_seconds
                self._events[key] = []

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)
            self._locked_until.pop(key, None)
