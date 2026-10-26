"""合同登记：与项目、客户、原件关联的最小合同台账。"""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .db import Database

CONTRACT_ID_RE = re.compile(r"^con-\d{8}-\d{6}-[0-9a-f]{6}$")
CONTRACT_STATUSES = {
    "draft": "草稿",
    "signed": "已签署",
    "effective": "已生效",
    "void": "已作废",
}


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id() -> str:
    return f"con-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def _text(value: Any, *, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _amount(value: Any, field: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}不是有效金额") from exc
    if number < 0:
        raise ValueError(f"{field}不能为负数")
    return str(number)


def _date(value: Any, field: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}格式应为 YYYY-MM-DD") from exc
    return text


class ContractStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _record(row) -> dict:
        return {
            "id": str(row["id"]),
            "owner": str(row["owner"]),
            "projectId": str(row["project_id"]),
            "customerId": row["customer_id"],
            "title": str(row["title"]),
            "contractNo": str(row["contract_no"]),
            "counterparty": str(row["counterparty"]),
            "signingEntity": str(row["signing_entity"]),
            "amount": row["amount"],
            "settlementAmount": row["settlement_amount"],
            "status": str(row["status"]),
            "statusLabel": CONTRACT_STATUSES.get(
                str(row["status"]), str(row["status"])
            ),
            "signedDate": row["signed_date"],
            "paymentTerms": str(row["payment_terms"]),
            "fileId": row["file_id"],
            "note": str(row["note"]),
            "createdAt": str(row["created_at"]),
            "updatedAt": str(row["updated_at"]),
        }

    def list_for_project(self, project_id: str) -> tuple[dict, ...]:
        rows = self.db.query(
            "SELECT * FROM contracts WHERE project_id=? "
            "ORDER BY updated_at DESC",
            (project_id,),
        )
        return tuple(self._record(row) for row in rows)

    def get(self, owner: str, project_id: str, contract_id: str) -> dict | None:
        if not CONTRACT_ID_RE.fullmatch(contract_id):
            return None
        row = self.db.one(
            "SELECT * FROM contracts WHERE id=? AND project_id=? AND owner=?",
            (contract_id, project_id, owner),
        )
        return self._record(row) if row is not None else None

    def create(
        self,
        owner: str,
        *,
        project_id: str,
        customer_id: str | None,
        title: str,
        contract_no: str = "",
        counterparty: str = "",
        signing_entity: str = "",
        amount: Any = "",
        settlement_amount: Any = "",
        status: str = "draft",
        signed_date: Any = "",
        payment_terms: str = "",
        file_id: str | None = None,
        note: str = "",
    ) -> dict:
        title = _text(title, limit=100)
        if not title:
            raise ValueError("请填写合同名称")
        if status not in CONTRACT_STATUSES:
            raise ValueError("合同状态无效")
        contract_id = _new_id()
        now = _now_text()
        self.db.execute(
            "INSERT INTO contracts(id, owner, project_id, customer_id, title, "
            "contract_no, counterparty, signing_entity, amount, "
            "settlement_amount, status, signed_date, payment_terms, file_id, "
            "note, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                contract_id,
                owner,
                project_id,
                customer_id,
                title,
                _text(contract_no, limit=80),
                _text(counterparty, limit=100),
                _text(signing_entity, limit=100),
                _amount(amount, "合同额"),
                _amount(settlement_amount, "结算额"),
                status,
                _date(signed_date, "签署日期"),
                _text(payment_terms, limit=2000),
                file_id,
                _text(note, limit=1000),
                now,
                now,
            ),
        )
        return self.get(owner, project_id, contract_id)  # type: ignore[return-value]

    def update(
        self,
        owner: str,
        project_id: str,
        contract_id: str,
        *,
        title: str,
        contract_no: str = "",
        counterparty: str = "",
        signing_entity: str = "",
        amount: Any = "",
        settlement_amount: Any = "",
        status: str = "draft",
        signed_date: Any = "",
        payment_terms: str = "",
        file_id: str | None = None,
        note: str = "",
    ) -> dict | None:
        if self.get(owner, project_id, contract_id) is None:
            return None
        title = _text(title, limit=100)
        if not title:
            raise ValueError("请填写合同名称")
        if status not in CONTRACT_STATUSES:
            raise ValueError("合同状态无效")
        self.db.execute(
            "UPDATE contracts SET title=?, contract_no=?, counterparty=?, "
            "signing_entity=?, amount=?, settlement_amount=?, status=?, "
            "signed_date=?, payment_terms=?, file_id=?, note=?, updated_at=? "
            "WHERE id=? AND project_id=? AND owner=?",
            (
                title,
                _text(contract_no, limit=80),
                _text(counterparty, limit=100),
                _text(signing_entity, limit=100),
                _amount(amount, "合同额"),
                _amount(settlement_amount, "结算额"),
                status,
                _date(signed_date, "签署日期"),
                _text(payment_terms, limit=2000),
                file_id,
                _text(note, limit=1000),
                _now_text(),
                contract_id,
                project_id,
                owner,
            ),
        )
        return self.get(owner, project_id, contract_id)

    def delete(self, owner: str, project_id: str, contract_id: str) -> bool:
        record = self.get(owner, project_id, contract_id)
        if record is None or record["status"] != "draft":
            return False
        self.db.execute(
            "DELETE FROM contracts WHERE id=? AND project_id=? AND owner=?",
            (contract_id, project_id, owner),
        )
        return True


__all__ = ["ContractStore", "CONTRACT_ID_RE", "CONTRACT_STATUSES"]
