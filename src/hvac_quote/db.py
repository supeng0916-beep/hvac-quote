"""本地 SQLite 数据库：账号、项目、报价、资料、检查、价格书和日志。"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Iterator, Sequence

SCHEMA_VERSION = 14

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    customer TEXT NOT NULL DEFAULT '',
    customer_id TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS projects_owner_updated
    ON projects(owner, updated_at DESC);

CREATE TABLE IF NOT EXISTS project_members (
    project_id TEXT NOT NULL,
    username TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'editor',
    added_by TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY(project_id, username),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    short_name TEXT NOT NULL DEFAULT '',
    tax_id TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS customers_owner_name
    ON customers(owner, name);
CREATE INDEX IF NOT EXISTS customers_owner_updated
    ON customers(owner, updated_at DESC);

CREATE TABLE IF NOT EXISTS customer_contacts (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    name TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    wechat TEXT NOT NULL DEFAULT '',
    is_primary INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS customer_contacts_customer
    ON customer_contacts(customer_id, created_at);

CREATE TABLE IF NOT EXISTS contracts (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    project_id TEXT NOT NULL,
    customer_id TEXT,
    title TEXT NOT NULL,
    contract_no TEXT NOT NULL DEFAULT '',
    counterparty TEXT NOT NULL DEFAULT '',
    signing_entity TEXT NOT NULL DEFAULT '',
    amount TEXT,
    settlement_amount TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    signed_date TEXT,
    payment_terms TEXT NOT NULL DEFAULT '',
    file_id TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE SET NULL,
    FOREIGN KEY(file_id) REFERENCES project_files(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS contracts_project
    ON contracts(project_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS contracts_owner_status
    ON contracts(owner, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS quotes (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    slice_key TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    total TEXT,
    supplemented INTEGER NOT NULL DEFAULT 0,
    include_in_total INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS quotes_project_created
    ON quotes(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS project_files (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    content_format TEXT NOT NULL DEFAULT '',
    format_version TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS project_files_project
    ON project_files(project_id, uploaded_at DESC);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    username TEXT,
    action TEXT NOT NULL,
    project_id TEXT,
    run_id TEXT,
    detail_json TEXT,
    client_ip TEXT
);
CREATE INDEX IF NOT EXISTS audit_logs_created
    ON audit_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS audit_logs_project
    ON audit_logs(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS drawing_inspections (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    status TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(file_id) REFERENCES project_files(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS drawing_inspections_project
    ON drawing_inspections(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS price_books (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    name TEXT NOT NULL,
    rows_json TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(file_id) REFERENCES project_files(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS price_books_project
    ON price_books(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS takeoff_drafts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    inspection_id TEXT NOT NULL,
    price_book_id TEXT,
    status TEXT NOT NULL,
    data_json TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(inspection_id) REFERENCES drawing_inspections(id) ON DELETE CASCADE,
    FOREIGN KEY(price_book_id) REFERENCES price_books(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS takeoff_drafts_project
    ON takeoff_drafts(project_id, updated_at DESC);


CREATE TABLE IF NOT EXISTS template_mapping_profiles (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    customer_key TEXT NOT NULL DEFAULT '',
    signature TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    header_row INTEGER NOT NULL,
    columns_json TEXT NOT NULL,
    headers_json TEXT NOT NULL,
    file_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner, customer_key, signature)
);
CREATE INDEX IF NOT EXISTS template_mapping_profiles_customer
    ON template_mapping_profiles(owner, customer_key, updated_at DESC);

CREATE TABLE IF NOT EXISTS mapping_rules (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_key TEXT NOT NULL,
    price_row INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, kind, source_key),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS mapping_rules_project
    ON mapping_rules(project_id, kind);

CREATE TABLE IF NOT EXISTS exceptions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    draft_id TEXT,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    detail_json TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(draft_id) REFERENCES takeoff_drafts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS exceptions_project_status
    ON exceptions(project_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS copper_price_records (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    project_id TEXT NOT NULL,
    record_date TEXT NOT NULL,
    region TEXT NOT NULL,
    source_name TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    material_grade TEXT NOT NULL,
    outer_diameter_mm TEXT NOT NULL,
    wall_thickness_mm TEXT NOT NULL,
    copper_price_per_ton TEXT NOT NULL,
    processing_fee_per_ton TEXT NOT NULL,
    weight_kg_per_m TEXT NOT NULL,
    price_per_m TEXT NOT NULL,
    price_book_id TEXT,
    template_row INTEGER,
    template_material_price TEXT,
    delta_per_m TEXT,
    delta_percent TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    version INTEGER NOT NULL DEFAULT 0,
    confirmed_by TEXT,
    confirmed_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(price_book_id) REFERENCES price_books(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS copper_price_records_project
    ON copper_price_records(project_id, record_date DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS copper_price_records_status
    ON copper_price_records(project_id, status, version DESC);

CREATE TABLE IF NOT EXISTS publication_intents (
    idem_key TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    project_id TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    request_digest TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    run_id TEXT,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS publication_intents_project
    ON publication_intents(project_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS user_preferences (
    owner TEXT PRIMARY KEY,
    preferences_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_template_preferences (
    owner TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    source_file_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    protected_name TEXT NOT NULL,
    price_book_name TEXT NOT NULL,
    book_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vision_tasks (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    project_id TEXT NOT NULL,
    inspection_id TEXT NOT NULL,
    inspection_version TEXT NOT NULL,
    draft_id TEXT NOT NULL DEFAULT '',
    file_id TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    task_fingerprint TEXT NOT NULL DEFAULT '',
    task_json TEXT NOT NULL,
    candidate_snapshot_json TEXT NOT NULL,
    images_json TEXT NOT NULL,
    crop_dir TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    schema_version TEXT NOT NULL DEFAULT '',
    task_version INTEGER NOT NULL DEFAULT 0,
    suggestion_version TEXT NOT NULL DEFAULT '',
    suggestion_json TEXT NOT NULL DEFAULT '{}',
    rejected_json TEXT NOT NULL DEFAULT '[]',
    usage_json TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    actor TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(owner, idempotency_key)
);
CREATE INDEX IF NOT EXISTS vision_tasks_project
    ON vision_tasks(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS vision_tasks_status
    ON vision_tasks(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS vision_previews (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    project_id TEXT NOT NULL,
    inspection_id TEXT NOT NULL,
    inspection_version TEXT NOT NULL,
    draft_id TEXT NOT NULL DEFAULT '',
    candidate_ids_json TEXT NOT NULL,
    candidate_snapshot_json TEXT NOT NULL,
    image_json TEXT NOT NULL,
    png_path TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    crop_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS vision_previews_project
    ON vision_previews(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS vision_adoptions (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    project_id TEXT NOT NULL,
    inspection_id TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    task_version INTEGER NOT NULL,
    suggestion_version TEXT NOT NULL,
    status TEXT NOT NULL,
    before_revision INTEGER NOT NULL,
    after_revision INTEGER NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    decisions_json TEXT NOT NULL,
    derived_json TEXT NOT NULL,
    derived_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS vision_adoptions_draft
    ON vision_adoptions(project_id, draft_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS vision_decision_items (
    id TEXT PRIMARY KEY,
    adoption_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    suggestion_item_id TEXT NOT NULL,
    candidate_ids_json TEXT NOT NULL,
    decision TEXT NOT NULL,
    model_suggestion_json TEXT NOT NULL DEFAULT '{}',
    human_edits_json TEXT NOT NULL DEFAULT '{}',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY(adoption_id) REFERENCES vision_adoptions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS vision_decision_items_adoption
    ON vision_decision_items(adoption_id);

CREATE TABLE IF NOT EXISTS deployment_receipts (
    installation_id TEXT PRIMARY KEY,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    """单进程、多线程共享连接；WAL 让读写互不阻塞。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            timeout=10.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            columns = {
                row["name"]
                for row in self._conn.execute(
                    "PRAGMA table_info(quotes)"
                ).fetchall()
            }
            if "include_in_total" not in columns:
                self._conn.execute(
                    "ALTER TABLE quotes ADD COLUMN include_in_total "
                    "INTEGER NOT NULL DEFAULT 1"
                )
            project_columns = {
                row["name"]
                for row in self._conn.execute(
                    "PRAGMA table_info(projects)"
                ).fetchall()
            }
            if "customer_id" not in project_columns:
                self._conn.execute(
                    "ALTER TABLE projects ADD COLUMN customer_id TEXT"
                )
            user_columns = {
                row["name"]
                for row in self._conn.execute(
                    "PRAGMA table_info(users)"
                ).fetchall()
            }
            file_columns = {
                row["name"]
                for row in self._conn.execute(
                    "PRAGMA table_info(project_files)"
                ).fetchall()
            }
            if "content_format" not in file_columns:
                self._conn.execute(
                    "ALTER TABLE project_files ADD COLUMN content_format "
                    "TEXT NOT NULL DEFAULT ''"
                )
            if "format_version" not in file_columns:
                self._conn.execute(
                    "ALTER TABLE project_files ADD COLUMN format_version "
                    "TEXT NOT NULL DEFAULT ''"
                )
            if "is_admin" not in user_columns:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN is_admin "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            draft_columns = {
                row["name"]
                for row in self._conn.execute(
                    "PRAGMA table_info(takeoff_drafts)"
                ).fetchall()
            }
            if "revision" not in draft_columns:
                self._conn.execute(
                    "ALTER TABLE takeoff_drafts ADD COLUMN revision "
                    "INTEGER NOT NULL DEFAULT 1"
                )
            if not self._conn.execute(
                "SELECT 1 FROM users WHERE is_admin=1 LIMIT 1"
            ).fetchone():
                self._conn.execute(
                    "UPDATE users SET is_admin=1 WHERE rowid=("
                    "SELECT rowid FROM users "
                    "ORDER BY created_at ASC, rowid ASC LIMIT 1)"
                )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS projects_customer "
                "ON projects(owner, customer_id)"
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.one(sql, params)
        return row[0] if row is not None else None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def vacuum(self) -> None:
        with self._lock:
            self._conn.execute("VACUUM")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def backup_to(self, target: Path) -> Path:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            destination = sqlite3.connect(str(target))
            try:
                self._conn.backup(destination)
            finally:
                destination.close()
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return target


def decode_json(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback
