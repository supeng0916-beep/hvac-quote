"""项目、报价记录和项目资料；持久化在本地 SQLite。

项目是长期报价工作的归档单位；每个项目可以有多份报价切片/版本。
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .db import Database

PROJECT_ID_RE = re.compile(r"^prj-\d{8}-\d{6}-[0-9a-f]{6}$")
RUN_ID_RE = re.compile(r"^run-\d{8}-\d{6}-[0-9a-f]{6}$")

QUOTE_STATUS_LABELS = {
    "needs_supplement": "待补充",
    "ready": "已生成",
}


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class QuoteRecord:
    run_id: str
    slice_key: str
    title: str
    status: str
    total: str | None
    supplemented: bool
    include_in_total: bool
    created_at: str
    updated_at: str

    @property
    def status_label(self) -> str:
        return QUOTE_STATUS_LABELS.get(self.status, self.status)

    def to_dict(self) -> dict:
        return {
            "runId": self.run_id,
            "sliceKey": self.slice_key,
            "title": self.title,
            "status": self.status,
            "total": self.total,
            "supplemented": self.supplemented,
            "includeInTotal": self.include_in_total,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


@dataclass
class ProjectFile:
    id: str
    original_name: str
    stored_name: str
    kind: str
    size: int
    sha256: str
    uploaded_at: str
    content_format: str = ""
    format_version: str = ""


@dataclass
class Project:
    id: str
    owner: str
    name: str
    customer: str
    note: str
    created_at: str
    updated_at: str
    customer_id: str | None = None
    quotes: list[QuoteRecord] = field(default_factory=list)
    files: list[ProjectFile] = field(default_factory=list)


class ProjectStore:
    def __init__(
        self,
        data_root: Path,
        db: Database | None = None,
    ) -> None:
        path = Path(data_root)
        if path.suffix == ".json":
            self.data_root = path.parent
            self.legacy_path = path
        else:
            self.data_root = path
            self.legacy_path = path / "projects.json"
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.db = db or Database(self.data_root / "hvac.db")
        self._import_legacy_projects()

    # ---- 旧 JSON 一次性迁移 -------------------------------------------------

    def _import_legacy_projects(self) -> None:
        if int(self.db.scalar("SELECT COUNT(*) FROM projects") or 0) > 0:
            return
        if not self.legacy_path.is_file():
            return
        try:
            payload = json.loads(
                self.legacy_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return
        project_rows: list[tuple] = []
        quote_rows: list[tuple] = []
        file_rows: list[tuple] = []
        for raw in payload.get("projects", []):
            try:
                project_id = str(raw["id"])
                owner = str(raw["owner"])
                name = str(raw["name"])
            except (KeyError, TypeError):
                continue
            project_rows.append(
                (
                    project_id,
                    owner,
                    name,
                    str(raw.get("customer", "")),
                    str(raw.get("note", "")),
                    str(raw["createdAt"]),
                    str(raw.get("updatedAt") or raw["createdAt"]),
                )
            )
            for quote in raw.get("quotes", []):
                try:
                    quote_rows.append(
                        (
                            str(quote["runId"]),
                            project_id,
                            str(quote["sliceKey"]),
                            str(quote["title"]),
                            str(quote["status"]),
                            str(quote["total"]) if quote.get("total") is not None else None,
                            1 if quote.get("supplemented") else 0,
                            str(quote["createdAt"]),
                            str(quote.get("updatedAt") or quote["createdAt"]),
                        )
                    )
                except (KeyError, TypeError):
                    continue
            for item in raw.get("files", []):
                try:
                    file_rows.append(
                        (
                            str(item["id"]),
                            project_id,
                            str(item["originalName"]),
                            str(item["storedName"]),
                            str(item.get("kind", "other")),
                            int(item.get("size", 0)),
                            str(item.get("sha256", "")),
                            str(item["uploadedAt"]),
                        )
                    )
                except (KeyError, TypeError):
                    continue
        if not project_rows:
            return
        with self.db.transaction() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO projects(id, owner, name, customer, "
                "note, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                project_rows,
            )
            connection.executemany(
                "INSERT OR IGNORE INTO quotes(run_id, project_id, slice_key, "
                "title, status, total, supplemented, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                quote_rows,
            )
            connection.executemany(
                "INSERT OR IGNORE INTO project_files(id, project_id, "
                "original_name, stored_name, kind, size, sha256, uploaded_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                file_rows,
            )

    # ---- 读取 ---------------------------------------------------------------

    @staticmethod
    def _quote(row: sqlite3.Row) -> QuoteRecord:
        return QuoteRecord(
            run_id=str(row["run_id"]),
            slice_key=str(row["slice_key"]),
            title=str(row["title"]),
            status=str(row["status"]),
            total=row["total"],
            supplemented=bool(row["supplemented"]),
            include_in_total=bool(row["include_in_total"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _file(row: sqlite3.Row) -> ProjectFile:
        return ProjectFile(
            id=str(row["id"]),
            original_name=str(row["original_name"]),
            stored_name=str(row["stored_name"]),
            kind=str(row["kind"]),
            size=int(row["size"]),
            sha256=str(row["sha256"]),
            uploaded_at=str(row["uploaded_at"]),
            content_format=str(row["content_format"] or ""),
            format_version=str(row["format_version"] or ""),
        )

    def _quotes_for(
        self, project_ids: tuple[str, ...]
    ) -> dict[str, list[QuoteRecord]]:
        if not project_ids:
            return {}
        marks = ",".join("?" for _ in project_ids)
        rows = self.db.query(
            f"SELECT * FROM quotes WHERE project_id IN ({marks}) "
            "ORDER BY created_at DESC",
            project_ids,
        )
        grouped: dict[str, list[QuoteRecord]] = {}
        for row in rows:
            grouped.setdefault(str(row["project_id"]), []).append(
                self._quote(row)
            )
        return grouped

    def _files_for(
        self, project_ids: tuple[str, ...]
    ) -> dict[str, list[ProjectFile]]:
        if not project_ids:
            return {}
        marks = ",".join("?" for _ in project_ids)
        rows = self.db.query(
            f"SELECT * FROM project_files WHERE project_id IN ({marks}) "
            "ORDER BY uploaded_at DESC",
            project_ids,
        )
        grouped: dict[str, list[ProjectFile]] = {}
        for row in rows:
            grouped.setdefault(str(row["project_id"]), []).append(
                self._file(row)
            )
        return grouped

    def _hydrate(
        self,
        rows: list[sqlite3.Row],
    ) -> tuple[Project, ...]:
        ids = tuple(str(row["id"]) for row in rows)
        quotes = self._quotes_for(ids)
        files = self._files_for(ids)
        return tuple(
            Project(
                id=str(row["id"]),
                owner=str(row["owner"]),
                name=str(row["name"]),
                customer=str(row["customer"]),
                note=str(row["note"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
                customer_id=(
                    str(row["customer_id"]) if row["customer_id"] else None
                ),
                quotes=quotes.get(str(row["id"]), []),
                files=files.get(str(row["id"]), []),
            )
            for row in rows
        )

    def list_for(self, owner: str) -> tuple[Project, ...]:
        rows = self.db.query(
            "SELECT * FROM projects WHERE owner=? ORDER BY updated_at DESC",
            (owner,),
        )
        return self._hydrate(rows)

    def list_visible(self, username: str) -> tuple[Project, ...]:
        """自己拥有的项目 + 被共享为成员的项目。"""
        rows = self.db.query(
            "SELECT * FROM projects WHERE owner=? "
            "UNION SELECT p.* FROM projects p "
            "JOIN project_members m ON m.project_id=p.id AND m.username=? "
            "ORDER BY updated_at DESC",
            (username, username),
        )
        return self._hydrate(rows)

    def get(self, owner: str, project_id: str) -> Project | None:
        if not PROJECT_ID_RE.fullmatch(project_id):
            return None
        row = self.db.one(
            "SELECT * FROM projects WHERE id=? AND owner=?",
            (project_id, owner),
        )
        if row is None:
            return None
        hydrated = self._hydrate([row])
        return hydrated[0] if hydrated else None

    def get_by_id(self, project_id: str) -> Project | None:
        if not PROJECT_ID_RE.fullmatch(project_id):
            return None
        row = self.db.one(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        )
        if row is None:
            return None
        hydrated = self._hydrate([row])
        return hydrated[0] if hydrated else None

    def role_for(self, username: str, project_id: str) -> str | None:
        """owner / editor / viewer；无访问权限返回 None。"""
        if not PROJECT_ID_RE.fullmatch(project_id):
            return None
        row = self.db.one(
            "SELECT owner FROM projects WHERE id=?", (project_id,)
        )
        if row is None:
            return None
        if str(row["owner"]) == username:
            return "owner"
        member = self.db.one(
            "SELECT role FROM project_members WHERE project_id=? AND username=?",
            (project_id, username),
        )
        return str(member["role"]) if member is not None else None

    def list_members(self, project_id: str) -> tuple[dict, ...]:
        rows = self.db.query(
            "SELECT username, role, added_by, added_at FROM project_members "
            "WHERE project_id=? ORDER BY added_at",
            (project_id,),
        )
        return tuple(
            {
                "username": str(row["username"]),
                "role": str(row["role"]),
                "addedBy": str(row["added_by"]),
                "addedAt": str(row["added_at"]),
            }
            for row in rows
        )

    def add_member(
        self, project_id: str, username: str, role: str, added_by: str
    ) -> dict:
        if role not in ("editor", "viewer"):
            raise ValueError("成员角色只能是可编辑或只读")
        project = self.get_by_id(project_id)
        if project is None:
            raise ValueError("项目不存在")
        if project.owner == username:
            raise ValueError("项目所有者无需添加为成员")
        now = _now_text()
        try:
            self.db.execute(
                "INSERT INTO project_members(project_id, username, role, "
                "added_by, added_at) VALUES(?,?,?,?,?)",
                (project_id, username, role, added_by, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("该账号已是项目成员") from exc
        return {
            "username": username,
            "role": role,
            "addedBy": added_by,
            "addedAt": now,
        }

    def remove_member(self, project_id: str, username: str) -> bool:
        cursor = self.db.execute(
            "DELETE FROM project_members WHERE project_id=? AND username=?",
            (project_id, username),
        )
        return bool(cursor.rowcount)

    # ---- 项目 ---------------------------------------------------------------

    def create(
        self,
        owner: str,
        name: str,
        customer: str = "",
        note: str = "",
        customer_id: str | None = None,
    ) -> Project:
        name = name.strip()
        customer = customer.strip()
        note = note.strip()
        if not name:
            raise ValueError("请填写项目名称")
        if len(name) > 60:
            raise ValueError("项目名称不能超过 60 个字符")
        if len(customer) > 60:
            raise ValueError("客户名称不能超过 60 个字符")
        if len(note) > 500:
            raise ValueError("备注不能超过 500 个字符")
        now = _now_text()
        project_id = (
            f"prj-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        )
        self.db.execute(
            "INSERT INTO projects(id, owner, name, customer, customer_id, "
            "note, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                project_id,
                owner,
                name,
                customer,
                customer_id,
                note,
                now,
                now,
            ),
        )
        return Project(
            id=project_id,
            owner=owner,
            name=name,
            customer=customer,
            note=note,
            created_at=now,
            updated_at=now,
            customer_id=customer_id,
        )

    def delete(self, owner: str, project_id: str) -> tuple[str, ...]:
        project = self.get(owner, project_id)
        if project is None:
            raise ValueError("项目不存在")
        run_ids = tuple(quote.run_id for quote in project.quotes)
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM projects WHERE id=? AND owner=?",
                (project_id, owner),
            )
        return run_ids

    # ---- 报价 ---------------------------------------------------------------

    def add_quote(
        self,
        owner: str,
        project_id: str,
        *,
        run_id: str,
        slice_key: str,
        title: str,
        status: str,
        total: str | None,
        supplemented: bool,
    ) -> QuoteRecord:
        if not RUN_ID_RE.fullmatch(run_id):
            raise ValueError("报价编号无效")
        if status not in QUOTE_STATUS_LABELS:
            raise ValueError("报价状态无效")
        project = self.get(owner, project_id)
        if project is None:
            raise ValueError("项目不存在")
        now = _now_text()
        try:
            with self.db.transaction() as connection:
                connection.execute(
                    "INSERT INTO quotes(run_id, project_id, slice_key, title, "
                    "status, total, supplemented, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        project_id,
                        slice_key,
                        title,
                        status,
                        total,
                        1 if supplemented else 0,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE projects SET updated_at=? WHERE id=?",
                    (now, project_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("该报价已归档") from exc
        return QuoteRecord(
            run_id=run_id,
            slice_key=slice_key,
            title=title,
            status=status,
            total=total,
            supplemented=supplemented,
            include_in_total=True,
            created_at=now,
            updated_at=now,
        )

    def update_quote(
        self,
        owner: str,
        project_id: str,
        run_id: str,
        *,
        status: str | None = None,
        total: str | None = None,
        supplemented: bool | None = None,
    ) -> QuoteRecord:
        found = self.find_quote(owner, project_id, run_id)
        if found is None:
            raise ValueError("报价不存在")
        _, quote = found
        if status is not None:
            if status not in QUOTE_STATUS_LABELS:
                raise ValueError("报价状态无效")
            quote.status = status
        if total is not None:
            quote.total = total
        if supplemented is not None:
            quote.supplemented = supplemented
        now = _now_text()
        quote.updated_at = now
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE quotes SET status=?, total=?, supplemented=?, "
                "updated_at=? WHERE run_id=? AND project_id=?",
                (
                    quote.status,
                    quote.total,
                    1 if quote.supplemented else 0,
                    now,
                    run_id,
                    project_id,
                ),
            )
            connection.execute(
                "UPDATE projects SET updated_at=? WHERE id=?",
                (now, project_id),
            )
        return quote

    def set_quote_include(
        self,
        owner: str,
        project_id: str,
        run_id: str,
        included: bool,
    ) -> QuoteRecord:
        found = self.find_quote(owner, project_id, run_id)
        if found is None:
            raise ValueError("报价不存在")
        _, quote = found
        quote.include_in_total = included
        quote.updated_at = _now_text()
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE quotes SET include_in_total=?, updated_at=? "
                "WHERE run_id=? AND project_id=?",
                (1 if included else 0, quote.updated_at, run_id, project_id),
            )
            connection.execute(
                "UPDATE projects SET updated_at=? WHERE id=?",
                (quote.updated_at, project_id),
            )
        return quote

    def find_quote(
        self, owner: str, project_id: str, run_id: str
    ) -> tuple[Project, QuoteRecord] | None:
        project = self.get(owner, project_id)
        if project is None:
            return None
        quote = next(
            (item for item in project.quotes if item.run_id == run_id), None
        )
        return (project, quote) if quote is not None else None

    def delete_quote(self, owner: str, project_id: str, run_id: str) -> bool:
        found = self.find_quote(owner, project_id, run_id)
        if found is None:
            raise ValueError("报价不存在")
        now = _now_text()
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM quotes WHERE run_id=? AND project_id=?",
                (run_id, project_id),
            )
            connection.execute(
                "UPDATE projects SET updated_at=? WHERE id=?",
                (now, project_id),
            )
        return True

    def find_quote_any_owner(
        self, run_id: str
    ) -> tuple[Project, QuoteRecord] | None:
        row = self.db.one("SELECT * FROM quotes WHERE run_id=?", (run_id,))
        if row is None:
            return None
        project_row = self.db.one(
            "SELECT * FROM projects WHERE id=?", (row["project_id"],)
        )
        if project_row is None:
            return None
        project = self._hydrate([project_row])[0]
        return project, self._quote(row)

    def all_run_ids(self) -> set[str]:
        return {
            str(row["run_id"])
            for row in self.db.query("SELECT run_id FROM quotes")
        }

    # ---- 项目资料 -----------------------------------------------------------

    def add_file(
        self,
        owner: str,
        project_id: str,
        *,
        original_name: str,
        stored_name: str,
        kind: str,
        size: int,
        sha256: str,
        content_format: str = "",
        format_version: str = "",
    ) -> ProjectFile:
        if kind not in {"drawing", "template", "reference"}:
            raise ValueError("资料类型无效")
        if "/" in stored_name or "\\" in stored_name or not stored_name:
            raise ValueError("资料文件名无效")
        original_name = original_name.strip()
        if not original_name or len(original_name) > 240:
            raise ValueError("资料文件名无效")
        project = self.get(owner, project_id)
        if project is None:
            raise ValueError("项目不存在")
        now = _now_text()
        item = ProjectFile(
            id=f"file-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}",
            original_name=original_name,
            stored_name=stored_name,
            kind=kind,
            size=max(0, int(size)),
            sha256=sha256,
            uploaded_at=now,
            content_format=content_format,
            format_version=format_version,
        )
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO project_files(id, project_id, original_name, "
                "stored_name, kind, size, sha256, uploaded_at, "
                "content_format, format_version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    item.id,
                    project_id,
                    item.original_name,
                    item.stored_name,
                    item.kind,
                    item.size,
                    item.sha256,
                    item.uploaded_at,
                    item.content_format,
                    item.format_version,
                ),
            )
            connection.execute(
                "UPDATE projects SET updated_at=? WHERE id=?",
                (now, project_id),
            )
        return item

    def delete_file(
        self, owner: str, project_id: str, file_id: str
    ) -> ProjectFile:
        found = self.find_file(owner, project_id, file_id)
        if found is None:
            raise ValueError("资料不存在")
        _, item = found
        with self.db.transaction() as connection:
            connection.execute("DELETE FROM project_files WHERE id=?", (file_id,))
            connection.execute(
                "UPDATE projects SET updated_at=? WHERE id=?",
                (_now_text(), project_id),
            )
        return item

    def find_file(
        self, owner: str, project_id: str, file_id: str
    ) -> tuple[Project, ProjectFile] | None:
        project = self.get(owner, project_id)
        if project is None:
            return None
        item = next((row for row in project.files if row.id == file_id), None)
        return (project, item) if item is not None else None
