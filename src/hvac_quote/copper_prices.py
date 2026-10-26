"""铜管参考价记录与计算。

参考价只用于对比和提示，不修改客户模板、草稿或已生成报价。
"""

from __future__ import annotations

import re
import secrets
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .db import Database, decode_json

GRADE_COEFFICIENTS = {
    "TP2": Decimal("0.02796"),
    "H62": Decimal("0.02670"),
    "H65": Decimal("0.02670"),
}
STATUS_LABELS = {"draft": "草稿", "confirmed": "已确认"}
URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id() -> str:
    return f"cu-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def _decimal(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field}不是有效数字") from exc
    return number


def _text(value: Any, *, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def _plain(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def calculate_reference(
    *,
    grade: str,
    outer_diameter_mm: Any,
    wall_thickness_mm: Any,
    copper_price_per_ton: Any,
    processing_fee_per_ton: Any,
) -> dict[str, str]:
    grade = grade.strip().upper()
    if grade not in GRADE_COEFFICIENTS:
        raise ValueError("牌号只能是 TP2、H62 或 H65")
    outer = _decimal(outer_diameter_mm, "外径")
    wall = _decimal(wall_thickness_mm, "壁厚")
    copper = _decimal(copper_price_per_ton, "铜价")
    fee = _decimal(processing_fee_per_ton, "加工费")
    if outer <= 0 or wall <= 0:
        raise ValueError("外径和壁厚必须大于 0")
    if outer <= wall:
        raise ValueError("外径必须大于壁厚")
    if copper < 0 or fee < 0:
        raise ValueError("铜价和加工费不能为负数")
    weight = GRADE_COEFFICIENTS[grade] * wall * (outer - wall)
    price_per_m = (copper + fee) / Decimal("1000") * weight
    weight = weight.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    price_per_m = price_per_m.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return {
        "grade": grade,
        "outerDiameterMm": _plain(outer),
        "wallThicknessMm": _plain(wall),
        "copperPricePerTon": _plain(copper),
        "processingFeePerTon": _plain(fee),
        "weightKgPerM": _plain(weight),
        "pricePerM": _plain(price_per_m),
        "formula": (
            f"({_plain(copper)} + {_plain(fee)}) ÷ 1000 × "
            f"{_plain(GRADE_COEFFICIENTS[grade])} × {_plain(wall)} × "
            f"({_plain(outer)} - {_plain(wall)})"
        ),
    }


class CopperPriceStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _record(row) -> dict:
        return {
            "id": str(row["id"]),
            "projectId": str(row["project_id"]),
            "recordDate": str(row["record_date"]),
            "region": str(row["region"]),
            "sourceName": str(row["source_name"]),
            "sourceUrl": str(row["source_url"]),
            "grade": str(row["material_grade"]),
            "outerDiameterMm": str(row["outer_diameter_mm"]),
            "wallThicknessMm": str(row["wall_thickness_mm"]),
            "copperPricePerTon": str(row["copper_price_per_ton"]),
            "processingFeePerTon": str(row["processing_fee_per_ton"]),
            "weightKgPerM": str(row["weight_kg_per_m"]),
            "pricePerM": str(row["price_per_m"]),
            "priceBookId": row["price_book_id"],
            "templateRow": row["template_row"],
            "templateMaterialPrice": row["template_material_price"],
            "deltaPerM": row["delta_per_m"],
            "deltaPercent": row["delta_percent"],
            "status": str(row["status"]),
            "statusLabel": STATUS_LABELS.get(str(row["status"]), str(row["status"])),
            "version": int(row["version"]),
            "confirmedBy": row["confirmed_by"],
            "confirmedAt": row["confirmed_at"],
            "note": str(row["note"]),
            "createdBy": str(row["created_by"]),
            "createdAt": str(row["created_at"]),
        }

    def list_for_project(self, project_id: str) -> tuple[dict, ...]:
        rows = self.db.query(
            "SELECT * FROM copper_price_records WHERE project_id=? "
            "ORDER BY record_date DESC, created_at DESC",
            (project_id,),
        )
        return tuple(self._record(row) for row in rows)

    def get(self, project_id: str, record_id: str) -> dict | None:
        row = self.db.one(
            "SELECT * FROM copper_price_records WHERE project_id=? AND id=?",
            (project_id, record_id),
        )
        return self._record(row) if row is not None else None

    def create(
        self,
        *,
        owner: str,
        project_id: str,
        record_date: str,
        region: str,
        source_name: str,
        source_url: str,
        grade: str,
        outer_diameter_mm: Any,
        wall_thickness_mm: Any,
        copper_price_per_ton: Any,
        processing_fee_per_ton: Any,
        price_book_id: str | None = None,
        template_row: int | None = None,
        template_material_price: str | None = None,
        note: str = "",
        created_by: str = "",
    ) -> dict:
        try:
            datetime.strptime(record_date, "%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise ValueError("日期格式应为 YYYY-MM-DD") from exc
        if not region:
            raise ValueError("请填写地区")
        if source_url and not URL_RE.match(source_url):
            raise ValueError("来源链接必须以 http:// 或 https:// 开头")
        calc = calculate_reference(
            grade=grade,
            outer_diameter_mm=outer_diameter_mm,
            wall_thickness_mm=wall_thickness_mm,
            copper_price_per_ton=copper_price_per_ton,
            processing_fee_per_ton=processing_fee_per_ton,
        )
        delta_per_m = None
        delta_percent = None
        if template_material_price not in (None, ""):
            try:
                template_price = Decimal(str(template_material_price))
                delta = Decimal(calc["pricePerM"]) - template_price
                delta_per_m = str(delta.quantize(Decimal("0.0001")))
                if template_price != 0:
                    delta_percent = str(
                        (delta / template_price * 100).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                    )
            except (InvalidOperation, TypeError, ValueError):
                template_material_price = None
        record_id = _new_id()
        created = _now_text()
        self.db.execute(
            "INSERT INTO copper_price_records("
            "id, owner, project_id, record_date, region, source_name, source_url, "
            "material_grade, outer_diameter_mm, wall_thickness_mm, "
            "copper_price_per_ton, processing_fee_per_ton, weight_kg_per_m, "
            "price_per_m, price_book_id, template_row, template_material_price, "
            "delta_per_m, delta_percent, status, version, note, created_by, "
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record_id,
                owner,
                project_id,
                record_date,
                _text(region, limit=40),
                _text(source_name, limit=120),
                _text(source_url, limit=500),
                calc["grade"],
                calc["outerDiameterMm"],
                calc["wallThicknessMm"],
                calc["copperPricePerTon"],
                calc["processingFeePerTon"],
                calc["weightKgPerM"],
                calc["pricePerM"],
                price_book_id,
                int(template_row) if template_row else None,
                str(template_material_price) if template_material_price not in (None, "") else None,
                delta_per_m,
                delta_percent,
                "draft",
                0,
                _text(note, limit=1000),
                created_by,
                created,
            ),
        )
        return self.get(project_id, record_id)  # type: ignore[return-value]

    def confirm(
        self, project_id: str, record_id: str, username: str
    ) -> dict | None:
        record = self.get(project_id, record_id)
        if record is None:
            return None
        if record["status"] == "confirmed":
            return record
        next_version = int(
            self.db.scalar(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM copper_price_records "
                "WHERE project_id=? AND status='confirmed'",
                (project_id,),
            )
            or 1
        )
        self.db.execute(
            "UPDATE copper_price_records SET status='confirmed', version=?, "
            "confirmed_by=?, confirmed_at=? WHERE project_id=? AND id=?",
            (next_version, username, _now_text(), project_id, record_id),
        )
        return self.get(project_id, record_id)

    def delete_draft(self, project_id: str, record_id: str) -> bool:
        row = self.db.one(
            "SELECT status FROM copper_price_records WHERE project_id=? AND id=?",
            (project_id, record_id),
        )
        if row is None or str(row["status"]) != "draft":
            return False
        self.db.execute(
            "DELETE FROM copper_price_records WHERE project_id=? AND id=?",
            (project_id, record_id),
        )
        return True


__all__ = [
    "CopperPriceStore",
    "GRADE_COEFFICIENTS",
    "calculate_reference",
]
