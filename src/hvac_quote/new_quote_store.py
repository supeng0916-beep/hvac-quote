"""新图纸检查、模板价格书、报价草稿和异常队列的 SQLite 存取。"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any

from .db import Database, decode_json


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


class RevisionConflict(ValueError):
    """The draft changed after the caller read it; never overwrite that write."""


class PublicationConflict(ValueError):
    """A publication intent was reused for different content or is still running."""


def vision_adoption_business_data(data: dict) -> dict:
    """Compare business content across an unchanged rendered-form submission.

    The whitelist mirrors only default/empty controls and display provenance.
    Effective quantities, prices, selections, specs and confirmation evidence
    remain in the comparison, so a later business edit cannot be overwritten.
    """
    result = json.loads(json.dumps(data, ensure_ascii=False))
    for key in ("runId", "secondScheme", "vision", "visionPreview"):
        result.pop(key, None)
    defaults = {
        "globalCoefficient": "1", "equipmentCoefficient": "",
        "pipeCoefficient": "", "taxRate": "0", "taxBasis": "subtotal",
        "manualRoutes": {}, "secondPriceBookId": None,
    }
    for key, value in defaults.items():
        result.setdefault(key, value)
    equipment_by_key = {
        str(group.get("key")): group for group in result.get("equipment") or []
    }
    for kind, unit in (("equipment", "台"), ("routes", "米")):
        for group in result.get(kind) or []:
            if kind == "equipment" and group.get("isComponent") is True:
                parent = equipment_by_key.get(str(group.get("componentOfSet")))
                member_keys = [str(item.get("key")) for item in (parent or {}).get("setComponents") or []]
                if (
                    parent is not None
                    and parent.get("isSet") is True
                    and not parent.get("setIncomplete")
                    and not group.get("isComponentManual")
                    and not group.get("isSet")
                    and len(member_keys) >= 2
                    and len(set(member_keys)) == len(member_keys)
                    and member_keys.count(str(group.get("key"))) == 1
                    and all(
                        equipment_by_key.get(key, {}).get("isComponent") is True
                        and equipment_by_key[key].get("componentOfSet") == parent.get("key")
                        for key in member_keys
                    )
                ):
                    # Pre-fix visual adoptions could transiently re-enable an
                    # automatic set member as a standalone priced item. Only
                    # these ineffective display fields are canonicalized;
                    # quantities, prices and every member/parent key remain.
                    group["include"] = False
                    if group.get("unit") in (None, "", "台", "套"):
                        group["unit"] = "台"
                    if group.get("manualUnit") in (None, "", "台", "套"):
                        group["manualUnit"] = "台"
            for key in ("manualMaterial", "manualInstallation", "manualLabel", "manualSpec"):
                if group.get(key) in (None, ""):
                    group.pop(key, None)
            group["manualUnit"] = group.get("manualUnit") or group.get("unit") or unit
            for key in ("isComponentManual", "setConfirmed", "specConfirmed"):
                if group.get(key) in (None, False):
                    group.pop(key, None)
            review = group.get("routeReview")
            if isinstance(review, dict):
                # Reconfirming the same evidence only changes this origin tag.
                review.pop("source", None)
    for issue in result.get("issues") or []:
        if issue.get("severity") != "blocks_pricing":
            # These rows have no acknowledgment control in the rendered page.
            issue.pop("acknowledged", None)
    return result


class NewQuoteStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---- 图纸检查 -----------------------------------------------------------

    def save_inspection(
        self, project_id: str, file_id: str, report: dict
    ) -> dict:
        inspection_id = _new_id("insp")
        created = _now_text()
        self.db.execute(
            "INSERT INTO drawing_inspections(id, project_id, file_id, status, "
            "report_json, created_at) VALUES(?,?,?,?,?,?)",
            (
                inspection_id,
                project_id,
                file_id,
                "complete",
                json.dumps(report, ensure_ascii=False, separators=(",", ":")),
                created,
            ),
        )
        return {
            "id": inspection_id,
            "projectId": project_id,
            "fileId": file_id,
            "status": "complete",
            "report": report,
            "createdAt": created,
        }

    def list_inspections(self, project_id: str) -> tuple[dict, ...]:
        rows = self.db.query(
            "SELECT i.*, f.original_name FROM drawing_inspections i "
            "JOIN project_files f ON f.id=i.file_id "
            "WHERE i.project_id=? ORDER BY i.created_at DESC",
            (project_id,),
        )
        return tuple(self._inspection(row) for row in rows)

    def get_inspection(self, project_id: str, inspection_id: str) -> dict | None:
        row = self.db.one(
            "SELECT i.*, f.original_name FROM drawing_inspections i "
            "JOIN project_files f ON f.id=i.file_id "
            "WHERE i.project_id=? AND i.id=?",
            (project_id, inspection_id),
        )
        return self._inspection(row) if row is not None else None

    def latest_inspection_for_file(
        self, project_id: str, file_id: str
    ) -> dict | None:
        row = self.db.one(
            "SELECT i.*, f.original_name FROM drawing_inspections i "
            "JOIN project_files f ON f.id=i.file_id "
            "WHERE i.project_id=? AND i.file_id=? "
            "ORDER BY i.created_at DESC LIMIT 1",
            (project_id, file_id),
        )
        return self._inspection(row) if row is not None else None

    @staticmethod
    def _inspection(row) -> dict:
        return {
            "id": str(row["id"]),
            "projectId": str(row["project_id"]),
            "fileId": str(row["file_id"]),
            "fileName": str(row["original_name"]),
            "status": str(row["status"]),
            "report": decode_json(row["report_json"], {}),
            "createdAt": str(row["created_at"]),
        }

    # ---- 模板价格书 ---------------------------------------------------------

    def save_price_book(
        self,
        project_id: str,
        file_id: str,
        name: str,
        book: dict,
    ) -> dict:
        price_book_id = _new_id("pb")
        created = _now_text()
        self.db.execute(
            "INSERT INTO price_books(id, project_id, file_id, name, rows_json, "
            "source_sha256, created_at) VALUES(?,?,?,?,?,?,?)",
            (
                price_book_id,
                project_id,
                file_id,
                name[:200],
                json.dumps(book, ensure_ascii=False, separators=(",", ":")),
                str(book.get("sourceSha256") or ""),
                created,
            ),
        )
        return {
            "id": price_book_id,
            "projectId": project_id,
            "fileId": file_id,
            "name": name,
            "book": book,
            "createdAt": created,
        }

    def add_project_price_row(
        self,
        project_id: str,
        price_book_id: str,
        *,
        name: str,
        spec: str,
        unit: str,
        material: str,
        installation: str,
        category: str,
        system: str,
        aliases: list[str] | None = None,
    ) -> dict:
        price_book = self.get_price_book(project_id, price_book_id)
        if price_book is None:
            raise ValueError("价格书不存在")
        book = dict(price_book.get("book") or {})
        rows = list(book.get("rows") or [])
        max_row = max((int(row.get("rowNumber") or 0) for row in rows), default=0)
        row_number = max_row + 1
        while any(int(row.get("rowNumber") or 0) == row_number for row in rows):
            row_number += 1
        rows.append(
            {
                "rowNumber": row_number,
                "index": "项目价",
                "name": name,
                "spec": spec,
                "unit": unit or ("套" if category == "equipment" else "米"),
                "material": str(material),
                "installation": str(installation),
                "category": "equipment" if category == "equipment" else "pipe",
                "system": system or "general",
                "priceState": "priced",
                "projectPrice": True,
                "aliases": [alias for alias in (aliases or []) if alias],
            }
        )
        book["rows"] = rows
        book["counts"] = {
            "rows": len(rows),
            "priced": sum(
                1 for row in rows if row.get("priceState") == "priced"
            ),
            "equipment": sum(
                1 for row in rows if row.get("category") == "equipment"
            ),
            "pipe": sum(1 for row in rows if row.get("category") == "pipe"),
        }
        self.db.execute(
            "UPDATE price_books SET rows_json=? WHERE project_id=? AND id=?",
            (
                json.dumps(book, ensure_ascii=False, separators=(",", ":")),
                project_id,
                price_book_id,
            ),
        )
        return self.get_price_book(project_id, price_book_id)  # type: ignore[return-value]

    @staticmethod
    def _refresh_counts(book: dict) -> None:
        rows = list(book.get("rows") or [])
        book["counts"] = {
            "rows": len(rows),
            "priced": sum(1 for row in rows if row.get("priceState") == "priced"),
            "equipment": sum(1 for row in rows if row.get("category") == "equipment"),
            "pipe": sum(1 for row in rows if row.get("category") == "pipe"),
        }

    def _save_book(self, project_id: str, price_book_id: str, book: dict) -> dict:
        self._refresh_counts(book)
        self.db.execute(
            "UPDATE price_books SET rows_json=? WHERE project_id=? AND id=?",
            (
                json.dumps(book, ensure_ascii=False, separators=(",", ":")),
                project_id,
                price_book_id,
            ),
        )
        return self.get_price_book(project_id, price_book_id)  # type: ignore[return-value]

    def update_price_book_row(
        self,
        project_id: str,
        price_book_id: str,
        row_number: int,
        *,
        material: str,
        installation: str,
        unit: str,
    ) -> dict:
        price_book = self.get_price_book(project_id, price_book_id)
        if price_book is None:
            raise ValueError("价格书不存在")
        book = dict(price_book.get("book") or {})
        rows = list(book.get("rows") or [])
        target = next(
            (row for row in rows if int(row.get("rowNumber") or -1) == row_number),
            None,
        )
        if target is None:
            raise ValueError("价格书里没有这一行")
        target["material"] = str(material)
        target["installation"] = str(installation)
        if unit.strip():
            target["unit"] = unit.strip()
        book["rows"] = rows
        return self._save_book(project_id, price_book_id, book)

    def delete_price_book_row(
        self, project_id: str, price_book_id: str, row_number: int
    ) -> dict:
        price_book = self.get_price_book(project_id, price_book_id)
        if price_book is None:
            raise ValueError("价格书不存在")
        book = dict(price_book.get("book") or {})
        rows = list(book.get("rows") or [])
        target = next(
            (row for row in rows if int(row.get("rowNumber") or -1) == row_number),
            None,
        )
        if target is None:
            raise ValueError("价格书里没有这一行")
        if not target.get("projectPrice"):
            raise ValueError(
                "模板导入的行不能单独删除；可以修改它的单价，或删除整本价格书后重新导入。"
            )
        book["rows"] = [row for row in rows if row is not target]
        return self._save_book(project_id, price_book_id, book)

    def delete_price_book(self, project_id: str, price_book_id: str) -> None:
        if self.get_price_book(project_id, price_book_id) is None:
            raise ValueError("价格书不存在")
        draft_count = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM takeoff_drafts "
                "WHERE project_id=? AND price_book_id=?",
                (project_id, price_book_id),
            )
            or 0
        )
        if draft_count:
            raise ValueError(
                f"该价格书正被 {draft_count} 份草稿使用；请先删除这些草稿，"
                "或复制草稿后改用其他价格书。"
            )
        self.db.execute(
            "DELETE FROM price_books WHERE project_id=? AND id=?",
            (project_id, price_book_id),
        )

    def delete_draft(
        self,
        project_id: str,
        draft_id: str,
        *,
        expected_revision: int | None = None,
    ) -> bool:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT revision FROM takeoff_drafts "
                "WHERE project_id=? AND id=?",
                (project_id, draft_id),
            ).fetchone()
            if row is None:
                return False
            current_revision = int(row["revision"] or 1)
            if expected_revision is not None:
                try:
                    expected = int(expected_revision)
                except (TypeError, ValueError):
                    raise RevisionConflict(
                        "草稿版本无效，请刷新后重试。"
                    ) from None
                if expected != current_revision:
                    raise RevisionConflict(
                        "草稿已被其他操作修改，请刷新后重试。"
                    )
            cursor = connection.execute(
                "DELETE FROM takeoff_drafts "
                "WHERE project_id=? AND id=? AND revision=?",
                (project_id, draft_id, current_revision),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(
                    "草稿已被其他操作修改，请刷新后重试。"
                )
            return True

    def copy_draft(self, project_id: str, draft_id: str) -> dict:
        draft = self.get_draft(project_id, draft_id)
        if draft is None:
            raise ValueError("草稿不存在")
        data = dict(draft.get("data") or {})
        data["title"] = f"{data.get('title') or '草稿'}（副本）"
        return self.create_draft(
            project_id, draft["inspectionId"], draft["priceBookId"], data
        )

    def price_book_for_file(
        self, project_id: str, file_id: str
    ) -> dict | None:
        row = self.db.one(
            "SELECT p.*, f.original_name FROM price_books p "
            "JOIN project_files f ON f.id=p.file_id "
            "WHERE p.project_id=? AND p.file_id=? "
            "ORDER BY p.created_at DESC LIMIT 1",
            (project_id, file_id),
        )
        return self._price_book(row) if row is not None else None

    def list_price_books(self, project_id: str) -> tuple[dict, ...]:
        rows = self.db.query(
            "SELECT p.*, f.original_name FROM price_books p "
            "JOIN project_files f ON f.id=p.file_id "
            "WHERE p.project_id=? ORDER BY p.created_at DESC",
            (project_id,),
        )
        return tuple(self._price_book(row) for row in rows)

    def get_price_book(self, project_id: str, price_book_id: str) -> dict | None:
        row = self.db.one(
            "SELECT p.*, f.original_name FROM price_books p "
            "JOIN project_files f ON f.id=p.file_id "
            "WHERE p.project_id=? AND p.id=?",
            (project_id, price_book_id),
        )
        return self._price_book(row) if row is not None else None

    @staticmethod
    def _price_book(row) -> dict:
        return {
            "id": str(row["id"]),
            "projectId": str(row["project_id"]),
            "fileId": str(row["file_id"]),
            "fileName": str(row["original_name"]),
            "name": str(row["name"]),
            "book": decode_json(row["rows_json"], {}),
            "createdAt": str(row["created_at"]),
        }

    # ---- 草稿 ---------------------------------------------------------------

    def create_draft(
        self,
        project_id: str,
        inspection_id: str,
        price_book_id: str,
        data: dict,
    ) -> dict:
        draft_id = _new_id("draft")
        now = _now_text()
        self.db.execute(
            "INSERT INTO takeoff_drafts(id, project_id, inspection_id, "
            "price_book_id, status, data_json, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                draft_id,
                project_id,
                inspection_id,
                price_book_id,
                "draft",
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                now,
                now,
            ),
        )
        return self.get_draft(project_id, draft_id)  # type: ignore[return-value]

    def update_draft(
        self,
        project_id: str,
        draft_id: str,
        *,
        data: dict | None = None,
        status: str | None = None,
        price_book_id: str | None = None,
        expected_revision: int | None = None,
        exceptions: list[dict] | None = None,
        mapping_rules: dict[tuple[str, str], int] | None = None,
        quote_registration: dict | None = None,
        publication_intent: dict | None = None,
        vision_adoption: dict | None = None,
        vision_revoke: dict | None = None,
    ) -> dict:
        """CAS the draft and commit its exceptions/mapping/quote in one transaction.

        A rejected request must not leave a mapping-rule or quote-registration
        side effect, and a stale revision must never overwrite the competing
        writer's draft. File publication happens around this transaction and is
        compensated by the caller when the transaction fails.

        The returned draft snapshot is read inside the same transaction.  Once
        commit succeeds the caller must not perform another read just to build
        its response: a read failure after commit cannot undo the commit and
        must never be mistaken for a pre-commit failure.
        """
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM takeoff_drafts WHERE project_id=? AND id=?",
                (project_id, draft_id),
            ).fetchone()
            if row is None:
                raise ValueError("草稿不存在")
            current_revision = int(row["revision"] or 1)
            if expected_revision is not None:
                try:
                    expected = int(expected_revision)
                except (TypeError, ValueError):
                    raise RevisionConflict(
                        "草稿版本无效，请刷新后重试。"
                    ) from None
                if expected != current_revision:
                    raise RevisionConflict(
                        "草稿已被其他操作修改，请刷新后重试。"
                    )
            payload = (
                data
                if data is not None
                else decode_json(row["data_json"], {})
            )
            next_status = status or str(row["status"])
            next_price_book = (
                price_book_id
                if price_book_id is not None
                else row["price_book_id"]
            )
            cursor = connection.execute(
                "UPDATE takeoff_drafts SET data_json=?, status=?, price_book_id=?, "
                "revision=revision+1, updated_at=? "
                "WHERE project_id=? AND id=? AND revision=?",
                (
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    next_status,
                    next_price_book,
                    _now_text(),
                    project_id,
                    draft_id,
                    current_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise RevisionConflict(
                    "草稿已被其他操作修改，请刷新后重试。"
                )
            if exceptions is not None:
                self._replace_exceptions_conn(
                    connection, project_id, draft_id, exceptions
                )
            if mapping_rules:
                self._save_mapping_rules_conn(
                    connection, project_id, mapping_rules
                )
            if quote_registration:
                now = _now_text()
                connection.execute(
                    "INSERT INTO quotes(run_id, project_id, slice_key, title, "
                    "status, total, supplemented, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        str(quote_registration["run_id"]),
                        project_id,
                        str(quote_registration["slice_key"]),
                        str(quote_registration["title"]),
                        str(quote_registration["status"]),
                        quote_registration.get("total"),
                        1 if quote_registration.get("supplemented") else 0,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE projects SET updated_at=? WHERE id=?",
                    (now, project_id),
                )
            if publication_intent:
                self._complete_publication_intent_conn(
                    connection,
                    str(publication_intent.get("key") or ""),
                    run_id=str(publication_intent.get("run_id") or "")
                    or None,
                    status="ready",
                    error="",
                )
            if vision_adoption:
                self._insert_vision_adoption_conn(
                    connection, vision_adoption
                )
            if vision_revoke:
                adoption_id = str(vision_revoke["id"])
                cursor = connection.execute(
                    "UPDATE vision_adoptions SET status='revoked', "
                    "revoked_at=?, revoked_by=? WHERE project_id=? AND "
                    "draft_id=? AND id=? AND status='active'",
                    (_now_text(), str(vision_revoke["actor"]), project_id,
                     draft_id, adoption_id),
                )
                if cursor.rowcount != 1:
                    raise RevisionConflict("这次视觉采纳已经撤销或发生变化。")
                connection.execute(
                    "UPDATE vision_decision_items SET revoked_at=? WHERE adoption_id=?",
                    (_now_text(), adoption_id),
                )
            snapshot_row = connection.execute(
                "SELECT d.*, i.file_id, f.original_name AS drawing_name, "
                "p.name AS price_book_name "
                "FROM takeoff_drafts d "
                "JOIN drawing_inspections i ON i.id=d.inspection_id "
                "JOIN project_files f ON f.id=i.file_id "
                "LEFT JOIN price_books p ON p.id=d.price_book_id "
                "WHERE d.project_id=? AND d.id=?",
                (project_id, draft_id),
            ).fetchone()
            if snapshot_row is None:
                raise ValueError("草稿快照读取失败")
            snapshot = self._draft(snapshot_row)
        return snapshot

    # ---- 视觉任务/采纳 -------------------------------------------------------

    @staticmethod
    def _insert_vision_adoption_conn(connection, adoption: dict) -> None:
        now = _now_text()
        connection.execute(
            "INSERT INTO vision_adoptions(id, owner, project_id, "
            "inspection_id, draft_id, task_id, task_version, "
            "suggestion_version, status, before_revision, after_revision, "
            "before_json, after_json, decisions_json, derived_json, "
            "derived_hash, created_at, revoked_at, revoked_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,'')",
            (
                str(adoption["id"]),
                str(adoption["owner"]),
                str(adoption["projectId"]),
                str(adoption["inspectionId"]),
                str(adoption["draftId"]),
                str(adoption["taskId"]),
                int(adoption["taskVersion"]),
                str(adoption["suggestionVersion"]),
                str(adoption.get("status") or "active"),
                int(adoption["beforeRevision"]),
                int(adoption["afterRevision"]),
                json.dumps(
                    adoption["beforeData"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                json.dumps(
                    adoption["afterData"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                json.dumps(
                    adoption["decisions"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                json.dumps(
                    adoption["derived"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                str(adoption["derivedHash"]),
                now,
            ),
        )
        for item in adoption.get("decisionItems") or []:
            connection.execute(
                "INSERT INTO vision_decision_items(id, adoption_id, task_id, "
                "project_id, draft_id, suggestion_item_id, "
                "candidate_ids_json, decision, model_suggestion_json, "
                "human_edits_json, before_json, after_json, actor, "
                "created_at, revoked_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    _new_id("vdec"),
                    str(adoption["id"]),
                    str(adoption["taskId"]),
                    str(adoption["projectId"]),
                    str(adoption["draftId"]),
                    str(item["suggestionItemId"]),
                    json.dumps(
                        item.get("candidateIds") or [],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    str(item["decision"]),
                    json.dumps(
                        item.get("modelSuggestion") or {},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        item.get("humanEdits") or {},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        item.get("before") or {},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        item.get("after") or {},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    str(item.get("actor") or ""),
                    now,
                ),
            )

    def record_vision_adoption(self, adoption: dict) -> None:
        with self.db.transaction() as connection:
            self._insert_vision_adoption_conn(connection, adoption)

    def list_vision_adoptions(
        self, project_id: str, draft_id: str
    ) -> tuple[dict, ...]:
        rows = self.db.query(
            "SELECT * FROM vision_adoptions WHERE project_id=? AND "
            "draft_id=? ORDER BY created_at DESC",
            (project_id, draft_id),
        )
        return tuple(self._adoption(row) for row in rows)

    def get_vision_adoption(
        self, project_id: str, draft_id: str, adoption_id: str
    ) -> dict | None:
        row = self.db.one(
            "SELECT * FROM vision_adoptions WHERE project_id=? AND "
            "draft_id=? AND id=?",
            (project_id, draft_id, adoption_id),
        )
        return self._adoption(row) if row is not None else None

    @staticmethod
    def _adoption(row) -> dict:
        return {
            "id": str(row["id"]),
            "owner": str(row["owner"]),
            "projectId": str(row["project_id"]),
            "inspectionId": str(row["inspection_id"]),
            "draftId": str(row["draft_id"]),
            "taskId": str(row["task_id"]),
            "taskVersion": int(row["task_version"] or 0),
            "suggestionVersion": str(row["suggestion_version"]),
            "status": str(row["status"]),
            "beforeRevision": int(row["before_revision"] or 0),
            "afterRevision": int(row["after_revision"] or 0),
            "beforeData": decode_json(row["before_json"], {}),
            "afterData": decode_json(row["after_json"], {}),
            "decisions": decode_json(row["decisions_json"], []),
            "derived": decode_json(row["derived_json"], {}),
            "derivedHash": str(row["derived_hash"]),
            "createdAt": str(row["created_at"]),
            "revokedAt": row["revoked_at"],
            "revokedBy": str(row["revoked_by"] or ""),
        }

    def active_vision_route_overrides(
        self, project_id: str, draft_id: str
    ) -> dict[str, dict]:
        rows = self.db.query(
            "SELECT derived_json FROM vision_adoptions WHERE project_id=? "
            "AND draft_id=? AND status='active' ORDER BY created_at DESC, "
            "rowid DESC",
            (project_id, draft_id),
        )
        result: dict[str, dict] = {}
        for row in rows:
            derived = decode_json(row["derived_json"], {})
            for key, value in (derived.get("routes") or {}).items():
                if key not in result and isinstance(value, dict):
                    result[str(key)] = value
        return result

    def mark_vision_adoption_revoked(
        self,
        project_id: str,
        draft_id: str,
        adoption_id: str,
        *,
        actor: str,
    ) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE vision_adoptions SET status='revoked', "
                "revoked_at=?, revoked_by=? WHERE project_id=? AND "
                "draft_id=? AND id=?",
                (_now_text(), actor, project_id, draft_id, adoption_id),
            )
            connection.execute(
                "UPDATE vision_decision_items SET revoked_at=? "
                "WHERE adoption_id=?",
                (_now_text(), adoption_id),
            )

    def revoke_vision_adoption(
        self,
        project_id: str,
        draft_id: str,
        adoption_id: str,
        *,
        actor: str,
        expected_revision: int,
        exceptions: list[dict] | None = None,
    ) -> dict:
        adoption = self.get_vision_adoption(
            project_id, draft_id, adoption_id
        )
        if adoption is None:
            raise ValueError("采纳记录不存在")
        if adoption["status"] != "active":
            raise RevisionConflict("这次视觉采纳已经撤销。")
        draft = self.get_draft(project_id, draft_id)
        if draft is None:
            raise ValueError("草稿不存在")
        if int(expected_revision) != int(draft["revision"]):
            raise RevisionConflict("草稿版本已变化，请刷新后重试。")
        if vision_adoption_business_data(draft["data"]) != vision_adoption_business_data(adoption["afterData"]):
            raise RevisionConflict(
                "草稿在采纳后又被修改，不能直接撤销；请先核对当前草稿。"
            )
        restored = json.loads(
            json.dumps(adoption["beforeData"], ensure_ascii=False)
        )
        restored.setdefault("vision", {})
        restored["vision"] = {
            **dict(restored.get("vision") or {}),
            "lastRevokedAdoptionId": adoption_id,
            "lastRevokedAt": _now_text(),
        }
        return self.update_draft(
            project_id,
            draft_id,
            data=restored,
            status="draft",
            expected_revision=int(expected_revision),
            exceptions=exceptions if exceptions is not None else [],
            vision_revoke={"id": adoption_id, "actor": actor},
        )

    # ---- 报价发布意图 -------------------------------------------------------

    def claim_publication_intent(
        self,
        *,
        key: str,
        owner: str,
        project_id: str,
        draft_id: str,
        fingerprint: str,
        request_digest: str = "",
        stale_seconds: int = 600,
    ) -> dict:
        """Claim one generation intent or return its already-published result.

        The key is bound to the account, project, draft and confirmed content
        fingerprint.  A replay of the same intent never creates a second run;
        reusing the key for different content is rejected explicitly.
        """
        if not key or not fingerprint:
            raise PublicationConflict("生成请求缺少幂等标识。")
        now = _now_text()
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM publication_intents WHERE idem_key=?",
                (key,),
            ).fetchone()
            if row is not None:
                same_identity = (
                    str(row["owner"]) == owner
                    and str(row["project_id"]) == project_id
                    and str(row["draft_id"]) == draft_id
                )
                status = str(row["status"])
                if not same_identity:
                    raise PublicationConflict(
                        "同一个生成请求标识被用于不同内容或不同草稿；"
                        "请刷新页面后开始一次新的生成。"
                    )
                if status == "draft":
                    # The page-level key is claimed by the first real submit;
                    # edits made on that page before submitting are the
                    # content this intent commits to.
                    connection.execute(
                        "UPDATE publication_intents SET fingerprint=?, "
                        "request_digest=?, status='running', error='', "
                        "updated_at=? WHERE idem_key=?",
                        (fingerprint, request_digest, now, key),
                    )
                    return {"state": "claimed", "key": key, "retry": False}
                if (
                    str(row["fingerprint"]) != fingerprint
                    or (
                        request_digest
                        and str(row["request_digest"] or "")
                        and str(row["request_digest"]) != request_digest
                    )
                ):
                    raise PublicationConflict(
                        "同一个生成请求标识被用于不同内容或不同草稿；"
                        "请刷新页面后开始一次新的生成。"
                    )
                if status == "ready" and row["run_id"]:
                    return {
                        "state": "ready",
                        "runId": str(row["run_id"]),
                        "key": key,
                    }
                if status == "running":
                    updated = self._parse_time(str(row["updated_at"]))
                    if (
                        updated is not None
                        and (datetime.now().astimezone() - updated).total_seconds()
                        < stale_seconds
                    ):
                        return {"state": "running", "key": key}
                connection.execute(
                    "UPDATE publication_intents SET status='running', "
                    "request_digest=?, error='', updated_at=? "
                    "WHERE idem_key=?",
                    (request_digest or str(row["request_digest"] or ""), now, key),
                )
                return {"state": "claimed", "key": key, "retry": True}
            connection.execute(
                "INSERT INTO publication_intents(idem_key, owner, project_id, "
                "draft_id, fingerprint, request_digest, status, run_id, error, "
                "created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,'running',NULL,'',?,?)",
                (
                    key,
                    owner,
                    project_id,
                    draft_id,
                    fingerprint,
                    request_digest,
                    now,
                    now,
                ),
            )
            return {"state": "claimed", "key": key, "retry": False}

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    def fail_publication_intent(self, key: str, error: str) -> None:
        try:
            self.db.execute(
                "UPDATE publication_intents SET status='failed', error=?, "
                "updated_at=? WHERE idem_key=? AND status!='ready'",
                (str(error)[:500], _now_text(), key),
            )
        except Exception:  # noqa: BLE001 - must not mask the publication error
            pass

    def mark_publication_intent_ready(self, key: str, run_id: str) -> None:
        try:
            self.db.execute(
                "UPDATE publication_intents SET status='ready', run_id=?, "
                "error='', updated_at=? WHERE idem_key=?",
                (run_id, _now_text(), key),
            )
        except Exception:  # noqa: BLE001
            pass

    def mark_publication_intent_uncertain(self, key: str, error: str) -> None:
        try:
            self.db.execute(
                "UPDATE publication_intents SET status='uncertain', error=?, "
                "updated_at=? WHERE idem_key=? AND status!='ready'",
                (str(error)[:500], _now_text(), key),
            )
        except Exception:  # noqa: BLE001
            pass

    def get_publication_intent(self, key: str) -> dict | None:
        row = self.db.one(
            "SELECT * FROM publication_intents WHERE idem_key=?", (key,)
        )
        if row is None:
            return None
        return {
            "key": str(row["idem_key"]),
            "owner": str(row["owner"]),
            "projectId": str(row["project_id"]),
            "draftId": str(row["draft_id"]),
            "fingerprint": str(row["fingerprint"]),
            "requestDigest": str(row["request_digest"] or ""),
            "status": str(row["status"]),
            "runId": row["run_id"],
            "error": str(row["error"] or ""),
            "createdAt": str(row["created_at"]),
            "updatedAt": str(row["updated_at"]),
        }

    def ensure_publication_intent(
        self,
        *,
        key: str,
        owner: str,
        project_id: str,
        draft_id: str,
        fingerprint: str,
    ) -> dict:
        """Persist a page-level generation key so refresh/retry keeps it."""
        if not key or not fingerprint:
            return {"key": key, "status": "draft"}
        now = _now_text()
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM publication_intents WHERE idem_key=?",
                (key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO publication_intents(idem_key, owner, "
                    "project_id, draft_id, fingerprint, request_digest, "
                    "status, run_id, error, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,'','draft',NULL,'',?,?)",
                    (key, owner, project_id, draft_id, fingerprint, now, now),
                )
                return {"key": key, "status": "draft"}
            return {
                "key": key,
                "status": str(row["status"]),
                "runId": row["run_id"],
            }

    def quote_exists(self, project_id: str, run_id: str) -> bool:
        return (
            self.db.one(
                "SELECT 1 FROM quotes WHERE project_id=? AND run_id=?",
                (project_id, run_id),
            )
            is not None
        )

    @staticmethod
    def _complete_publication_intent_conn(
        connection,
        key: str,
        *,
        run_id: str | None,
        status: str,
        error: str,
    ) -> None:
        if not key:
            return
        connection.execute(
            "UPDATE publication_intents SET status=?, run_id=?, error=?, "
            "updated_at=? WHERE idem_key=?",
            (status, run_id, error, _now_text(), key),
        )

    def get_draft(self, project_id: str, draft_id: str) -> dict | None:
        row = self.db.one(
            "SELECT d.*, i.file_id, f.original_name AS drawing_name, "
            "p.name AS price_book_name "
            "FROM takeoff_drafts d "
            "JOIN drawing_inspections i ON i.id=d.inspection_id "
            "JOIN project_files f ON f.id=i.file_id "
            "LEFT JOIN price_books p ON p.id=d.price_book_id "
            "WHERE d.project_id=? AND d.id=?",
            (project_id, draft_id),
        )
        return self._draft(row) if row is not None else None

    def list_drafts(self, project_id: str) -> tuple[dict, ...]:
        rows = self.db.query(
            "SELECT d.*, i.file_id, f.original_name AS drawing_name, "
            "p.name AS price_book_name "
            "FROM takeoff_drafts d "
            "JOIN drawing_inspections i ON i.id=d.inspection_id "
            "JOIN project_files f ON f.id=i.file_id "
            "LEFT JOIN price_books p ON p.id=d.price_book_id "
            "WHERE d.project_id=? ORDER BY d.updated_at DESC",
            (project_id,),
        )
        return tuple(self._draft(row) for row in rows)

    @staticmethod
    def _draft(row) -> dict:
        return {
            "id": str(row["id"]),
            "projectId": str(row["project_id"]),
            "inspectionId": str(row["inspection_id"]),
            "priceBookId": row["price_book_id"],
            "drawingFileId": str(row["file_id"]),
            "drawingName": str(row["drawing_name"]),
            "priceBookName": row["price_book_name"],
            "status": str(row["status"]),
            "revision": int(row["revision"] or 1),
            "data": decode_json(row["data_json"], {}),
            "createdAt": str(row["created_at"]),
            "updatedAt": str(row["updated_at"]),
        }

    # ---- 模板行映射规则 -----------------------------------------------------

    def get_mapping_rules(self, project_id: str) -> dict[tuple[str, str], int]:
        rows = self.db.query(
            "SELECT kind, source_key, price_row FROM mapping_rules "
            "WHERE project_id=?",
            (project_id,),
        )
        return {
            (str(row["kind"]), str(row["source_key"])): int(row["price_row"])
            for row in rows
        }

    def save_mapping_rules(
        self,
        project_id: str,
        rules: dict[tuple[str, str], int],
    ) -> None:
        if not rules:
            return
        with self.db.transaction() as connection:
            self._save_mapping_rules_conn(connection, project_id, rules)

    @staticmethod
    def _save_mapping_rules_conn(
        connection,
        project_id: str,
        rules: dict[tuple[str, str], int],
    ) -> None:
        now = _now_text()
        for (kind, source_key), price_row in rules.items():
            connection.execute(
                "INSERT INTO mapping_rules(id, project_id, kind, "
                "source_key, price_row, updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(project_id, kind, source_key) DO UPDATE SET "
                "price_row=excluded.price_row, updated_at=excluded.updated_at",
                (
                    _new_id("map"),
                    project_id,
                    kind,
                    source_key,
                    int(price_row),
                    now,
                ),
            )

    # ---- 异常 ---------------------------------------------------------------

    def replace_exceptions(
        self,
        project_id: str,
        draft_id: str,
        exceptions: list[dict],
    ) -> None:
        with self.db.transaction() as connection:
            self._replace_exceptions_conn(
                connection, project_id, draft_id, exceptions
            )

    @staticmethod
    def _replace_exceptions_conn(
        connection,
        project_id: str,
        draft_id: str,
        exceptions: list[dict],
    ) -> None:
        connection.execute(
            "DELETE FROM exceptions WHERE project_id=? AND draft_id=?",
            (project_id, draft_id),
        )
        for item in exceptions:
            connection.execute(
                "INSERT INTO exceptions(id, project_id, draft_id, code, "
                "message, severity, status, detail_json, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    _new_id("exc"),
                    project_id,
                    draft_id,
                    str(item.get("code", "ISSUE")),
                    str(item.get("message", ""))[:500],
                    str(item.get("severity", "review")),
                    "open",
                    json.dumps(
                        item.get("detail") or {},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    _now_text(),
                ),
            )

    def list_exceptions(
        self, project_id: str, draft_id: str | None = None
    ) -> tuple[dict, ...]:
        if draft_id:
            rows = self.db.query(
                "SELECT * FROM exceptions WHERE project_id=? AND draft_id=? "
                "ORDER BY created_at DESC",
                (project_id, draft_id),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM exceptions WHERE project_id=? "
                "ORDER BY created_at DESC",
                (project_id,),
            )
        return tuple(
            {
                "id": str(row["id"]),
                "draftId": row["draft_id"],
                "code": str(row["code"]),
                "message": str(row["message"]),
                "severity": str(row["severity"]),
                "status": str(row["status"]),
                "detail": decode_json(row["detail_json"], {}),
                "createdAt": str(row["created_at"]),
                "resolvedAt": row["resolved_at"],
            }
            for row in rows
        )

    def resolve_exception(
        self, project_id: str, exception_id: str
    ) -> None:
        self.db.execute(
            "UPDATE exceptions SET status='resolved', resolved_at=? "
            "WHERE project_id=? AND id=?",
            (_now_text(), project_id, exception_id),
        )

    def get_price_book_rows(self, project_id: str, price_book_id: str) -> list[dict]:
        price_book = self.get_price_book(project_id, price_book_id)
        if price_book is None:
            return []
        return list((price_book.get("book") or {}).get("rows") or [])


__all__ = ["NewQuoteStore", "Any"]
