"""客户级模板列映射配置：手工选过一次，下次同类报价表自动套用。"""

from __future__ import annotations

import json
import secrets
from datetime import datetime

from .db import Database, decode_json
from .projects import Project


def customer_key(project: Project) -> str:
    if project.customer_id:
        return f"customer:{project.customer_id}"
    customer = project.customer.strip()
    if customer:
        return f"name:{customer}"
    return "account"


class MappingProfileStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list_for(self, owner: str, key: str) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM template_mapping_profiles "
            "WHERE owner=? AND customer_key=? ORDER BY updated_at DESC",
            (owner, key),
        )
        return [self._profile(row) for row in rows]

    def get(self, owner: str, key: str, profile_id: str) -> dict | None:
        row = self.db.one(
            "SELECT * FROM template_mapping_profiles "
            "WHERE owner=? AND customer_key=? AND id=?",
            (owner, key, profile_id),
        )
        return self._profile(row) if row is not None else None

    def delete(self, owner: str, key: str, profile_id: str) -> bool:
        if self.get(owner, key, profile_id) is None:
            return False
        self.db.execute(
            "DELETE FROM template_mapping_profiles "
            "WHERE owner=? AND customer_key=? AND id=?",
            (owner, key, profile_id),
        )
        return True

    def upsert(
        self,
        *,
        owner: str,
        key: str,
        signature: str,
        sheet_name: str,
        header_row: int,
        columns: dict[str, str],
        headers: dict[str, str],
        file_name: str,
    ) -> dict:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        existing = self.db.one(
            "SELECT id, created_at FROM template_mapping_profiles "
            "WHERE owner=? AND customer_key=? AND signature=?",
            (owner, key, signature),
        )
        profile_id = (
            str(existing["id"])
            if existing is not None
            else f"map-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        )
        created = (
            str(existing["created_at"]) if existing is not None else now
        )
        self.db.execute(
            "INSERT INTO template_mapping_profiles("
            "id, owner, customer_key, signature, sheet_name, header_row, "
            "columns_json, headers_json, file_name, created_at, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(owner, customer_key, signature) DO UPDATE SET "
            "sheet_name=excluded.sheet_name, header_row=excluded.header_row, "
            "columns_json=excluded.columns_json, headers_json=excluded.headers_json, "
            "file_name=excluded.file_name, updated_at=excluded.updated_at",
            (
                profile_id,
                owner,
                key,
                signature,
                sheet_name,
                int(header_row),
                json.dumps(columns, ensure_ascii=False, separators=(",", ":")),
                json.dumps(headers, ensure_ascii=False, separators=(",", ":")),
                file_name,
                created,
                now,
            ),
        )
        row = self.db.one(
            "SELECT * FROM template_mapping_profiles WHERE id=?", (profile_id,)
        )
        return self._profile(row)

    @staticmethod
    def _profile(row) -> dict:
        return {
            "id": str(row["id"]),
            "customerKey": str(row["customer_key"]),
            "signature": str(row["signature"]),
            "sheetName": str(row["sheet_name"]),
            "headerRow": int(row["header_row"]),
            "columns": decode_json(row["columns_json"], {}),
            "headers": decode_json(row["headers_json"], {}),
            "fileName": str(row["file_name"]),
            "createdAt": str(row["created_at"]),
            "updatedAt": str(row["updated_at"]),
        }
