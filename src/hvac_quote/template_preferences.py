"""账号级整份报价模板偏好。

记住老板第一次成功导入/选用的多联机模板与价格书快照，新项目自动准备同一
受保护模板副本和价格书。只保存模板文件与价格书数据，不复制项目、草稿或报价。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime
from pathlib import Path

from .db import Database
from .new_quote_store import NewQuoteStore
from .projects import ProjectFile, ProjectStore


class TemplatePreferenceError(ValueError):
    pass


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class TemplatePreferenceStore:
    def __init__(self, data_root: Path, db: Database) -> None:
        self.data_root = Path(data_root)
        self.db = db

    @property
    def root(self) -> Path:
        path = (self.data_root / "template-preferences").resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get(self, owner: str) -> dict | None:
        row = self.db.one(
            "SELECT * FROM account_template_preferences WHERE owner=?",
            (owner,),
        )
        if row is None:
            return None
        return {
            "owner": str(row["owner"]),
            "projectId": str(row["project_id"]),
            "sourceFileId": str(row["source_file_id"]),
            "fileName": str(row["file_name"]),
            "sha256": str(row["file_sha256"]),
            "protectedName": str(row["protected_name"]),
            "priceBookName": str(row["price_book_name"]),
            "book": json.loads(row["book_json"]),
            "updatedAt": str(row["updated_at"]),
        }

    def remember(
        self,
        *,
        owner: str,
        project_id: str,
        file_name: str,
        file_bytes: bytes,
        file_sha256: str,
        price_book_name: str,
        book: dict,
    ) -> dict:
        """Persist one protected template copy for the account."""
        if not owner or not project_id:
            raise TemplatePreferenceError("缺少账号或项目")
        actual = hashlib.sha256(file_bytes).hexdigest()
        if file_sha256 and actual != file_sha256:
            raise TemplatePreferenceError("模板文件内容校验失败")
        digest = actual
        folder = (self.root / owner).resolve()
        if not folder.is_relative_to(self.root):
            raise TemplatePreferenceError("账号模板目录无效")
        folder.mkdir(parents=True, exist_ok=True)
        protected_name = f"template-{digest[:24]}.xlsx"
        target = folder / protected_name
        if not target.is_file() or hashlib.sha256(
            target.read_bytes()
        ).hexdigest() != digest:
            temp = folder / f".{protected_name}.{secrets.token_hex(4)}.tmp"
            temp.write_bytes(file_bytes)
            os.replace(temp, target)
        snapshot = dict(book)
        snapshot.setdefault("sourceSha256", digest)
        now = _now_text()
        self.db.execute(
            "INSERT INTO account_template_preferences(owner, project_id, "
            "source_file_id, file_name, file_sha256, protected_name, "
            "price_book_name, book_json, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(owner) DO UPDATE SET project_id=excluded.project_id, "
            "source_file_id=excluded.source_file_id, "
            "file_name=excluded.file_name, "
            "file_sha256=excluded.file_sha256, "
            "protected_name=excluded.protected_name, "
            "price_book_name=excluded.price_book_name, "
            "book_json=excluded.book_json, updated_at=excluded.updated_at",
            (
                owner,
                project_id,
                "",
                file_name[:240],
                digest,
                protected_name,
                price_book_name[:200],
                json.dumps(
                    snapshot, ensure_ascii=False, separators=(",", ":")
                ),
                now,
            ),
        )
        return self.get(owner) or {}

    def remember_if_absent(self, **kwargs) -> dict:
        owner = str(kwargs.get("owner") or "")
        existing = self.get(owner)
        if existing is not None:
            return existing
        return self.remember(**kwargs)

    def protected_path(self, preference: dict) -> Path:
        folder = (self.root / str(preference["owner"])).resolve()
        path = (folder / str(preference["protectedName"])).resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            raise TemplatePreferenceError("账号默认模板文件不存在，请重新设置")
        return path

    def clear(self, owner: str) -> bool:
        cursor = self.db.execute(
            "DELETE FROM account_template_preferences WHERE owner=?",
            (owner,),
        )
        return bool(cursor.rowcount)

    def ensure_project(
        self,
        *,
        owner: str,
        project_id: str,
        projects: ProjectStore,
        new_quotes: NewQuoteStore,
    ) -> dict | None:
        """Prepare the account default template and price book in a project."""
        preference = self.get(owner)
        if preference is None:
            return None
        project = projects.get(owner, project_id)
        if project is None:
            return None
        digest = str(preference["sha256"])
        existing_file: ProjectFile | None = next(
            (
                item
                for item in project.files
                if item.kind == "template" and item.sha256 == digest
            ),
            None,
        )
        if existing_file is None:
            source = self.protected_path(preference)
            content = source.read_bytes()
            if hashlib.sha256(content).hexdigest() != digest:
                raise TemplatePreferenceError(
                    "账号默认模板已损坏，请重新设置"
                )
            stored_name = f"default-{digest[:16]}.xlsx"
            folder = (
                self.data_root / "projects" / project_id / "files"
            ).resolve()
            if not folder.is_relative_to(self.data_root):
                raise TemplatePreferenceError("项目资料目录无效")
            folder.mkdir(parents=True, exist_ok=True)
            target = (folder / stored_name).resolve()
            if not target.is_relative_to(folder):
                raise TemplatePreferenceError("项目资料文件名无效")
            target.write_bytes(content)
            existing_file = projects.add_file(
                owner,
                project_id,
                original_name=str(preference["fileName"]),
                stored_name=stored_name,
                kind="template",
                size=len(content),
                sha256=digest,
                content_format="xlsx",
            )
        row = self.db.one(
            "SELECT id FROM price_books WHERE project_id=? AND "
            "source_sha256=? ORDER BY created_at LIMIT 1",
            (project_id, digest),
        )
        if row is not None:
            return new_quotes.get_price_book(project_id, str(row["id"]))
        book = dict(preference["book"])
        book["sourceSha256"] = digest
        return new_quotes.save_price_book(
            project_id,
            existing_file.id,
            str(preference["priceBookName"]),
            book,
        )


__all__ = ["TemplatePreferenceError", "TemplatePreferenceStore"]
