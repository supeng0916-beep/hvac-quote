"""客户与联系人：经营系统的共享主数据。

客户是长期对象，项目必须明确归属客户；同一客户可以有多个工程。
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime

from .db import Database

CUSTOMER_ID_RE = re.compile(r"^cus-\d{8}-\d{6}-[0-9a-f]{6}$")


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_customer_id() -> str:
    return f"cus-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def _new_contact_id() -> str:
    return f"ct-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def _text(value: str, *, limit: int) -> str:
    return str(value or "").strip()[:limit]


class CustomerStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _contact(row) -> dict:
        return {
            "id": str(row["id"]),
            "customerId": str(row["customer_id"]),
            "name": str(row["name"]),
            "title": str(row["title"]),
            "phone": str(row["phone"]),
            "wechat": str(row["wechat"]),
            "isPrimary": bool(row["is_primary"]),
            "note": str(row["note"]),
            "createdAt": str(row["created_at"]),
        }

    @staticmethod
    def _customer(row, contacts: tuple[dict, ...] = ()) -> dict:
        return {
            "id": str(row["id"]),
            "owner": str(row["owner"]),
            "name": str(row["name"]),
            "shortName": str(row["short_name"]),
            "taxId": str(row["tax_id"]),
            "address": str(row["address"]),
            "note": str(row["note"]),
            "createdAt": str(row["created_at"]),
            "updatedAt": str(row["updated_at"]),
            "contacts": contacts,
        }

    def list_for(self, owner: str) -> tuple[dict, ...]:
        rows = self.db.query(
            "SELECT * FROM customers WHERE owner=? ORDER BY updated_at DESC",
            (owner,),
        )
        contacts = self._contacts_by_customer(
            tuple(str(row["id"]) for row in rows)
        )
        return tuple(
            self._customer(row, contacts.get(str(row["id"]), ()))
            for row in rows
        )

    def _contacts_by_customer(
        self, customer_ids: tuple[str, ...]
    ) -> dict[str, tuple[dict, ...]]:
        if not customer_ids:
            return {}
        placeholders = ",".join("?" for _ in customer_ids)
        rows = self.db.query(
            "SELECT * FROM customer_contacts WHERE customer_id IN "
            f"({placeholders}) ORDER BY is_primary DESC, created_at",
            customer_ids,
        )
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            grouped.setdefault(str(row["customer_id"]), []).append(
                self._contact(row)
            )
        return {key: tuple(value) for key, value in grouped.items()}

    def get(self, owner: str, customer_id: str) -> dict | None:
        if not CUSTOMER_ID_RE.fullmatch(customer_id):
            return None
        row = self.db.one(
            "SELECT * FROM customers WHERE id=? AND owner=?",
            (customer_id, owner),
        )
        if row is None:
            return None
        contacts = self._contacts_by_customer((customer_id,))
        return self._customer(row, contacts.get(customer_id, ()))

    def create(
        self,
        owner: str,
        *,
        name: str,
        short_name: str = "",
        tax_id: str = "",
        address: str = "",
        note: str = "",
    ) -> dict:
        name = _text(name, limit=80)
        if not name:
            raise ValueError("请填写客户名称")
        customer_id = _new_customer_id()
        now = _now_text()
        self.db.execute(
            "INSERT INTO customers(id, owner, name, short_name, tax_id, "
            "address, note, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                customer_id,
                owner,
                name,
                _text(short_name, limit=40),
                _text(tax_id, limit=40),
                _text(address, limit=200),
                _text(note, limit=500),
                now,
                now,
            ),
        )
        return self.get(owner, customer_id)  # type: ignore[return-value]

    def update(
        self,
        owner: str,
        customer_id: str,
        *,
        name: str,
        short_name: str = "",
        tax_id: str = "",
        address: str = "",
        note: str = "",
    ) -> dict | None:
        if self.get(owner, customer_id) is None:
            return None
        name = _text(name, limit=80)
        if not name:
            raise ValueError("请填写客户名称")
        self.db.execute(
            "UPDATE customers SET name=?, short_name=?, tax_id=?, address=?, "
            "note=?, updated_at=? WHERE id=? AND owner=?",
            (
                name,
                _text(short_name, limit=40),
                _text(tax_id, limit=40),
                _text(address, limit=200),
                _text(note, limit=500),
                _now_text(),
                customer_id,
                owner,
            ),
        )
        return self.get(owner, customer_id)

    def add_contact(
        self,
        owner: str,
        customer_id: str,
        *,
        name: str,
        title: str = "",
        phone: str = "",
        wechat: str = "",
        is_primary: bool = False,
        note: str = "",
    ) -> dict | None:
        if self.get(owner, customer_id) is None:
            return None
        name = _text(name, limit=40)
        if not name:
            raise ValueError("请填写联系人姓名")
        contact_id = _new_contact_id()
        now = _now_text()
        with self.db.transaction() as connection:
            if is_primary:
                connection.execute(
                    "UPDATE customer_contacts SET is_primary=0 "
                    "WHERE customer_id=?",
                    (customer_id,),
                )
            connection.execute(
                "INSERT INTO customer_contacts(id, customer_id, name, title, "
                "phone, wechat, is_primary, note, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    contact_id,
                    customer_id,
                    name,
                    _text(title, limit=40),
                    _text(phone, limit=40),
                    _text(wechat, limit=60),
                    1 if is_primary else 0,
                    _text(note, limit=300),
                    now,
                ),
            )
        row = self.db.one(
            "SELECT * FROM customer_contacts WHERE id=?", (contact_id,)
        )
        return self._contact(row) if row is not None else None

    def delete_contact(
        self, owner: str, customer_id: str, contact_id: str
    ) -> bool:
        if self.get(owner, customer_id) is None:
            return False
        cursor = self.db.execute(
            "DELETE FROM customer_contacts WHERE id=? AND customer_id=?",
            (contact_id, customer_id),
        )
        return cursor.rowcount > 0


__all__ = ["CustomerStore", "CUSTOMER_ID_RE"]
