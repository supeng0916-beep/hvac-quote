"""正式网页视觉辅助：任务、局部裁图、人工决策和派生权威记录。

模型只给出语义建议；数量/长度来自原生 CAD 证据，价格来自客户模板。所有
任务、建议、人工决策和撤销记录持久化在 SQLite，页面刷新或服务重启不会
重新调用模型。
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .db import Database, decode_json
from .generic_quote import (
    _equipment_match,
    _insulation_match,
    _route_match,
    _row_label,
    draft_exceptions,
    generate_generic_quote,
    reconcile_equipment_sets,
    route_proposal_key,
    route_row_error,
)
from .new_quote_store import RevisionConflict
from .vision import (
    ALLOWED_CATEGORIES,
    ALLOWED_MEDIA,
    ALLOWED_ROLES,
    VisionAdvisor,
    VisionConfig,
    VisionUnavailable,
    canonical_json,
    prepare_task,
    task_fingerprint,
)
from .vision_crop import CropError, render_crop

TASK_ACTIVE = {"queued", "running"}
TASK_FAILED = {"failed", "timeout", "cancelled", "interrupted"}
PREVIEW_TTL_SECONDS = 30 * 60
MAX_PREVIEW_CANDIDATES = 12


class VisionTaskConflict(ValueError):
    pass


def get_preferences(db: Database, owner: str) -> dict:
    row = db.one(
        "SELECT preferences_json FROM user_preferences WHERE owner=?",
        (owner,),
    )
    if row is None:
        return {}
    value = decode_json(row["preferences_json"], {})
    return value if isinstance(value, dict) else {}


def set_preference(
    db: Database, owner: str, key: str, value
) -> dict:
    preferences = get_preferences(db, owner)
    preferences[str(key)] = value
    db.execute(
        "INSERT INTO user_preferences(owner, preferences_json, updated_at) "
        "VALUES(?,?,?) ON CONFLICT(owner) DO UPDATE SET "
        "preferences_json=excluded.preferences_json, "
        "updated_at=excluded.updated_at",
        (
            owner,
            json.dumps(
                preferences, ensure_ascii=False, separators=(",", ":")
            ),
            _now_text(),
        ),
    )
    return preferences


def _now() -> datetime:
    return datetime.now().astimezone()


def _now_text() -> str:
    return _now().isoformat(timespec="seconds")


def inspection_content_hash(inspection: dict) -> str:
    return hashlib.sha256(
        canonical_json(inspection.get("report") or {}).encode("utf-8")
    ).hexdigest()


def inspection_version(inspection: dict) -> str:
    return f"{inspection.get('id')}:{inspection_content_hash(inspection)}"


def _finite_bbox(value) -> list[float] | None:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            for item in value
        )
    ):
        return None
    box = [float(item) for item in value]
    if box[0] > box[2] or box[1] > box[3]:
        return None
    return box


def _candidate_id(kind: str, source_key: str) -> str:
    return f"{kind}-" + hashlib.sha256(
        f"{kind}|{source_key}".encode("utf-8")
    ).hexdigest()[:20]


def _medium(value: str, category: str = "") -> str:
    text = str(value or "").strip()
    if text in ALLOWED_MEDIA:
        return text
    mapping = {
        "冷媒管": "refrigerant",
        "气管": "supply",
        "液管": "return",
        "冷凝水": "condensate",
        "排水": "drain",
        "冷却水": "cooling",
        "补水": "makeup",
        "风管": "duct",
    }
    if text in mapping:
        return mapping[text]
    if "保温" in text or "保温" in str(category):
        return "unknown"
    return "unknown"


def _role(value: str) -> str:
    text = str(value or "").strip()
    return text if text in ALLOWED_ROLES else "unknown"


def _category(kind: str, value: str) -> str:
    text = str(value or "").strip()
    if kind == "equipment":
        return "equipment"
    if text in ALLOWED_CATEGORIES and text != "equipment":
        return "pipe" if text == "pipe" else text
    return "pipe"


def candidate_snapshot(inspection: dict) -> list[dict]:
    """Server-owned candidate identities bound to native groups/instances."""
    report = inspection.get("report") or {}
    result: list[dict] = []
    for group in report.get("equipmentGroups") or []:
        bbox = _finite_bbox(group.get("bbox"))
        source_key = str(group.get("key") or "")
        if not source_key or bbox is None:
            continue
        model = str(
            group.get("modelHint")
            or group.get("displayName")
            or group.get("blockName")
            or ""
        )
        result.append(
            {
                "candidateId": _candidate_id("equipment", source_key),
                "kind": "equipment",
                "sourceKey": source_key,
                "label": str(
                    group.get("displayName") or model or group.get("key")
                ),
                "category": "equipment",
                "medium": "unknown",
                "role": _role(str(group.get("role") or "")),
                "model": model,
                "spec": "",
                "layer": str(group.get("layer") or ""),
                "blockName": str(group.get("blockName") or ""),
                "view": str(group.get("view") or ""),
                "floorLabel": str(group.get("floorLabel") or ""),
                "quantity": int(group.get("quantity") or 0),
                "lengthM": None,
                "bbox": bbox,
                "handles": [str(item) for item in group.get("handles") or []],
                "instancePath": [],
                "nearbyText": [
                    str(item) for item in (group.get("textHints") or [])[:12]
                ],
                "reviewReasons": list(group.get("issues") or [])
                + list(group.get("reviewReasons") or []),
                "eligibility": "review"
                if group.get("issues")
                else "eligible",
                "autoInclude": bool(group.get("autoInclude")),
                "systemHint": str(group.get("systemHint") or ""),
            }
        )
    for group in report.get("routeGroups") or []:
        bbox = _finite_bbox(group.get("bbox"))
        source_key = str(group.get("key") or "")
        if not source_key or bbox is None:
            continue
        category = _category("route", str(group.get("category") or ""))
        result.append(
            {
                "candidateId": _candidate_id("route", source_key),
                "kind": "route",
                "sourceKey": source_key,
                "label": str(
                    group.get("layer")
                    or group.get("category")
                    or group.get("key")
                ),
                "category": category,
                "medium": _medium(
                    str(group.get("medium") or ""),
                    str(group.get("category") or ""),
                ),
                "role": _role(str(group.get("role") or "")),
                "model": "",
                "spec": str(group.get("spec") or ""),
                "layer": str(group.get("layer") or ""),
                "blockName": "",
                "view": str(group.get("view") or ""),
                "floorLabel": str(group.get("floorLabel") or ""),
                "quantity": int(group.get("count") or 0),
                "lengthM": group.get("lengthM"),
                "bbox": bbox,
                "handles": [str(item) for item in group.get("handles") or []],
                "instancePath": [],
                "nearbyText": [
                    str(item) for item in (group.get("textHints") or [])[:12]
                ],
                "reviewReasons": list(group.get("reviewReasons") or []),
                "eligibility": str(group.get("eligibility") or "review"),
                "autoInclude": bool(group.get("autoInclude")),
                "systemHint": "refrigerant",
            }
        )
    return result


def candidate_by_id(inspection: dict) -> dict[str, dict]:
    return {
        item["candidateId"]: item
        for item in candidate_snapshot(inspection)
    }


def _union_bbox(candidates: list[dict]) -> list[float] | None:
    boxes = [
        _finite_bbox(item.get("bbox"))
        for item in candidates
        if item.get("kind")
    ]
    boxes = [box for box in boxes if box is not None]
    if not boxes:
        return None
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def suggestion_items(task_id: str, suggestion: dict) -> list[dict]:
    items: list[dict] = []
    for index, decision in enumerate(suggestion.get("decisions") or []):
        item_id = hashlib.sha256(
            f"{task_id}|decision|{index}|{canonical_json(decision)}".encode(
                "utf-8"
            )
        ).hexdigest()[:24]
        items.append(
            {
                "suggestionItemId": item_id,
                **decision,
            }
        )
    return items


@dataclass
class TaskRow:
    id: str
    owner: str
    project_id: str
    inspection_id: str
    inspection_version: str
    draft_id: str
    file_id: str
    status: str
    idempotency_key: str
    task_fingerprint: str
    task: dict
    candidates: list[dict]
    images: list[dict]
    crop_dir: str
    provider: str
    model: str
    task_version: int
    suggestion_version: str
    suggestion: dict
    rejected: list[str]
    usage: dict
    attempts: int
    duration_ms: int
    error_code: str
    error_message: str
    cancel_requested: bool
    created_at: str
    updated_at: str


class VisionService:
    def __init__(self, db: Database, runs_root: Path) -> None:
        self.db = db
        self.runs_root = Path(runs_root).resolve()

    @property
    def preview_root(self) -> Path:
        return self.runs_root / "vision" / "previews"

    @property
    def task_root(self) -> Path:
        return self.runs_root / "vision" / "tasks"

    def enabled_for_task(self, owner: str, project_id: str) -> bool:
        """Read both the initiating account and the shared project's owner."""
        project = self.db.one("SELECT owner FROM projects WHERE id=?", (project_id,))
        if project is None:
            return False
        accounts = {owner, str(project["owner"])}
        return all(
            get_preferences(self.db, account).get("visionEnabled") is True
            for account in accounts
        )

    def require_enabled(self, owner: str, project_id: str) -> None:
        if not self.enabled_for_task(owner, project_id):
            raise VisionTaskConflict("视觉辅助已关闭，请先在账号设置中启用后重试")

    def _row(self, row) -> TaskRow:
        return TaskRow(
            id=str(row["id"]),
            owner=str(row["owner"]),
            project_id=str(row["project_id"]),
            inspection_id=str(row["inspection_id"]),
            inspection_version=str(row["inspection_version"]),
            draft_id=str(row["draft_id"] or ""),
            file_id=str(row["file_id"]),
            status=str(row["status"]),
            idempotency_key=str(row["idempotency_key"]),
            task_fingerprint=str(row["task_fingerprint"]),
            task=decode_json(row["task_json"], {}),
            candidates=decode_json(row["candidate_snapshot_json"], []),
            images=decode_json(row["images_json"], []),
            crop_dir=str(row["crop_dir"] or ""),
            provider=str(row["provider"] or ""),
            model=str(row["model"] or ""),
            task_version=int(row["task_version"] or 0),
            suggestion_version=str(row["suggestion_version"] or ""),
            suggestion=decode_json(row["suggestion_json"], {}),
            rejected=decode_json(row["rejected_json"], []),
            usage=decode_json(row["usage_json"], {}),
            attempts=int(row["attempts"] or 0),
            duration_ms=int(row["duration_ms"] or 0),
            error_code=str(row["error_code"] or ""),
            error_message=str(row["error_message"] or ""),
            cancel_requested=bool(row["cancel_requested"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get_task(
        self, owner: str, project_id: str, task_id: str
    ) -> TaskRow | None:
        row = self.db.one(
            "SELECT * FROM vision_tasks WHERE owner=? AND project_id=? "
            "AND id=?",
            (owner, project_id, task_id),
        )
        return self._row(row) if row is not None else None

    def list_tasks(
        self, owner: str, project_id: str, *, draft_id: str = ""
    ) -> tuple[TaskRow, ...]:
        if draft_id:
            rows = self.db.query(
                "SELECT * FROM vision_tasks WHERE owner=? AND project_id=? "
                "AND draft_id=? ORDER BY created_at DESC",
                (owner, project_id, draft_id),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM vision_tasks WHERE owner=? AND project_id=? "
                "ORDER BY created_at DESC",
                (owner, project_id),
            )
        return tuple(self._row(row) for row in rows)

    def create_preview(
        self,
        *,
        owner: str,
        project_id: str,
        inspection: dict,
        draft_id: str,
        candidate_ids: list[str],
        drawing_path: Path,
    ) -> dict:
        candidates = candidate_by_id(inspection)
        selected: list[dict] = []
        for candidate_id in candidate_ids:
            item = candidates.get(candidate_id)
            if item is None:
                raise VisionTaskConflict("选中的候选不属于当前检查")
            selected.append(item)
        if not selected:
            raise VisionTaskConflict("请至少选择一个疑难局部")
        if len(selected) > MAX_PREVIEW_CANDIDATES:
            raise VisionTaskConflict(
                f"一次最多选择 {MAX_PREVIEW_CANDIDATES} 个候选"
            )
        bbox = _union_bbox(selected)
        if bbox is None:
            raise VisionTaskConflict("所选候选缺少可渲染的图面范围")
        preview_id = f"vprev-{_now():%Y%m%d-%H%M%S}-{hashlib.sha256((owner+project_id+inspection['id']+str(time.time())).encode()).hexdigest()[:6]}"
        workdir = (self.preview_root / preview_id).resolve()
        if not workdir.is_relative_to(self.preview_root):
            raise VisionTaskConflict("预览目录无效")
        workdir.mkdir(parents=True, exist_ok=True)
        candidates_for_crop = [
            {
                "candidateId": item["candidateId"],
                "bbox": item["bbox"],
            }
            for item in selected
        ]
        try:
            crop = render_crop(
                drawing_path,
                str(inspection.get("fileId") or "drawing"),
                workdir / "crop-work",
                bbox=bbox,
                candidates=candidates_for_crop,
                padding=0.12,
                max_size=1400,
            )
        except CropError as exc:
            shutil.rmtree(workdir, ignore_errors=True)
            raise VisionTaskConflict(str(exc)) from exc
        png_path = workdir / "crop.png"
        png_path.write_bytes(crop.png)
        manifest = dict(crop.mapping.get("manifest") or {})
        (workdir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        image = {
            "cropId": str(crop.mapping.get("cropId") or ""),
            "mediaType": "image/png",
            "pngSha256": str(crop.mapping.get("pngSha256") or ""),
            "manifestHash": hashlib.sha256(
                canonical_json(manifest).encode("utf-8")
            ).hexdigest(),
            "manifest": manifest,
            "width": int(manifest.get("width") or 0),
            "height": int(manifest.get("height") or 0),
        }
        snapshot = {
            "previewId": preview_id,
            "owner": owner,
            "projectId": project_id,
            "inspectionId": str(inspection.get("id") or ""),
            "inspectionVersion": inspection_version(inspection),
            "draftId": draft_id,
            "candidateIds": [item["candidateId"] for item in selected],
            "candidates": selected,
            "image": image,
            "pngPath": str(png_path),
            "manifest": manifest,
            "cropId": image["cropId"],
            "createdAt": _now_text(),
            "expiresAt": (
                _now() + timedelta(seconds=PREVIEW_TTL_SECONDS)
            ).isoformat(timespec="seconds"),
        }
        self.db.execute(
            "INSERT INTO vision_previews(id, owner, project_id, "
            "inspection_id, inspection_version, draft_id, "
            "candidate_ids_json, candidate_snapshot_json, image_json, "
            "png_path, manifest_json, crop_id, created_at, expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                preview_id,
                owner,
                project_id,
                str(inspection.get("id") or ""),
                snapshot["inspectionVersion"],
                draft_id,
                json.dumps(
                    snapshot["candidateIds"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                json.dumps(
                    selected, ensure_ascii=False, separators=(",", ":")
                ),
                json.dumps(
                    image, ensure_ascii=False, separators=(",", ":")
                ),
                str(png_path),
                json.dumps(
                    manifest, ensure_ascii=False, separators=(",", ":")
                ),
                image["cropId"],
                snapshot["createdAt"],
                snapshot["expiresAt"],
            ),
        )
        return snapshot

    def get_preview(
        self, owner: str, project_id: str, preview_id: str
    ) -> dict | None:
        row = self.db.one(
            "SELECT * FROM vision_previews WHERE owner=? AND project_id=? "
            "AND id=?",
            (owner, project_id, preview_id),
        )
        if row is None:
            return None
        return {
            "previewId": str(row["id"]),
            "owner": str(row["owner"]),
            "projectId": str(row["project_id"]),
            "inspectionId": str(row["inspection_id"]),
            "inspectionVersion": str(row["inspection_version"]),
            "draftId": str(row["draft_id"] or ""),
            "candidateIds": decode_json(row["candidate_ids_json"], []),
            "candidates": decode_json(
                row["candidate_snapshot_json"], []
            ),
            "image": decode_json(row["image_json"], {}),
            "pngPath": str(row["png_path"]),
            "manifest": decode_json(row["manifest_json"], {}),
            "cropId": str(row["crop_id"]),
            "createdAt": str(row["created_at"]),
            "expiresAt": str(row["expires_at"]),
        }

    def create_task(
        self,
        *,
        owner: str,
        project_id: str,
        inspection: dict,
        draft_id: str,
        preview: dict,
        idempotency_key: str,
        opt_in: bool,
    ) -> TaskRow:
        if not opt_in:
            raise VisionTaskConflict("视觉辅助需要用户明确启用")
        self.require_enabled(owner, project_id)
        if not idempotency_key:
            raise VisionTaskConflict("缺少幂等标识")
        if (
            preview.get("owner") != owner
            or preview.get("projectId") != project_id
            or preview.get("draftId") != draft_id
        ):
            raise VisionTaskConflict("局部预览不属于当前账号、项目或目标草稿")
        if preview["inspectionId"] != str(inspection.get("id") or ""):
            raise VisionTaskConflict("预览与当前检查不一致")
        from .new_quote_store import NewQuoteStore

        target_draft = NewQuoteStore(self.db).get_draft(project_id, draft_id)
        if target_draft is None or str(inspection.get("projectId") or "") != project_id:
            raise VisionTaskConflict("目标草稿或图纸检查不属于当前项目")
        inspection_ids = set((target_draft.get("data") or {}).get("inspectionIds") or [])
        inspection_ids.add(str(target_draft.get("inspectionId") or ""))
        if str(inspection.get("id") or "") not in inspection_ids:
            raise VisionTaskConflict("目标草稿不属于这次图纸检查")
        if preview["inspectionVersion"] != inspection_version(inspection):
            raise VisionTaskConflict("检查内容已变化，请重新预览")
        try:
            expires = datetime.fromisoformat(preview["expiresAt"])
        except ValueError:
            expires = _now()
        if expires < _now():
            raise VisionTaskConflict("局部预览已过期，请重新选择")
        png_path = Path(str(preview["pngPath"]))
        if not png_path.is_file():
            raise VisionTaskConflict("局部裁图文件不存在，请重新预览")
        png = png_path.read_bytes()
        image = dict(preview["image"])
        manifest = dict(preview["manifest"])
        if hashlib.sha256(png).hexdigest() != str(image.get("pngSha256")):
            raise VisionTaskConflict("局部裁图内容已变化，请重新预览")
        candidates = list(preview["candidates"])
        existing = self.db.one(
            "SELECT * FROM vision_tasks WHERE owner=? AND idempotency_key=?",
            (owner, idempotency_key),
        )
        task_payload = {
            "targetDraftRevision": int(target_draft.get("revision") or 1),
            "sourceHash": str((inspection.get("report") or {}).get("sourceHash") or ""),
            "fileId": str(inspection.get("fileId") or ""),
            "inspectionId": str(inspection.get("id") or ""),
            "inspectionContentHash": inspection_content_hash(inspection),
            "scope": {
                "view": str((candidates[0] if candidates else {}).get("view") or ""),
                "bbox": _union_bbox(candidates) or [],
            },
            "question": "请复核所选疑难局部的设备类别、管线介质、角色及标注关联；不要计算长度、数量或价格。",
            "candidates": candidates,
            "annotations": [],
            "edges": [],
            "vertices": [],
            "allowedEdges": [],
            "allowedVertices": [],
            "cropIds": [str(image.get("cropId") or "")],
        }
        descriptors = [
            {
                "cropId": image["cropId"],
                "mediaType": "image/png",
                "pngSha256": image["pngSha256"],
                "manifestHash": image["manifestHash"],
            }
        ]
        prepared = prepare_task(task_payload, descriptors)
        fingerprint = task_fingerprint(task_payload, images=descriptors)
        if existing is not None:
            row = self._row(existing)
            if (
                row.project_id != project_id
                or row.draft_id != draft_id
                or row.task_fingerprint != fingerprint
            ):
                raise VisionTaskConflict(
                    "同一个任务标识的内容已经变化，请重新预览后再提交"
                )
            return row
        task_id = (
            f"vtask-{_now():%Y%m%d-%H%M%S}-"
            f"{hashlib.sha256((owner+project_id+idempotency_key).encode()).hexdigest()[:6]}"
        )
        crop_dir = (self.task_root / task_id).resolve()
        crop_dir.mkdir(parents=True, exist_ok=True)
        (crop_dir / "crop.png").write_bytes(png)
        (crop_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        now = _now_text()
        try:
            self.db.execute(
                "INSERT INTO vision_tasks(id, owner, project_id, "
                "inspection_id, inspection_version, draft_id, file_id, "
                "status, idempotency_key, task_fingerprint, task_json, "
                "candidate_snapshot_json, images_json, crop_dir, provider, "
                "model, prompt_version, schema_version, task_version, "
                "suggestion_version, suggestion_json, rejected_json, "
                "usage_json, attempts, duration_ms, error_code, "
                "error_message, cancel_requested, actor, created_at, "
                "updated_at, started_at, finished_at) "
                "VALUES(?,?,?,?,?,?,?, 'queued', ?,?,?,?,?,?,'','',?,?,0,"
                "'','{}','[]','{}',0,0,'','',0,?,?,?,NULL,NULL)",
                (
                    task_id,
                    owner,
                    project_id,
                    str(inspection.get("id") or ""),
                    inspection_version(inspection),
                    draft_id,
                    str(inspection.get("fileId") or ""),
                    idempotency_key,
                    fingerprint,
                    json.dumps(
                        task_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        candidates, ensure_ascii=False, separators=(",", ":")
                    ),
                    json.dumps(
                        [{**image, "manifest": manifest}],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    str(crop_dir),
                    str(prepared.get("promptVersion") or ""),
                    str(prepared.get("schemaVersion") or ""),
                    owner,
                    now,
                    now,
                ),
            )
        except Exception:
            shutil.rmtree(crop_dir, ignore_errors=True)
            raise
        return self.get_task(owner, project_id, task_id)  # type: ignore[return-value]

    def mark_interrupted(self) -> int:
        cursor = self.db.execute(
            "UPDATE vision_tasks SET status='interrupted', "
            "error_code='server_restarted', "
            "error_message='服务重启，任务已中断；请由用户主动重试', "
            "updated_at=? WHERE status IN ('queued','running')",
            (_now_text(),),
        )
        return int(cursor.rowcount or 0)

    def claim_task(self, task_id: str) -> TaskRow | None:
        cursor = self.db.execute(
            "UPDATE vision_tasks SET status='running', "
            "task_version=task_version+1, started_at=?, updated_at=?, "
            "error_code='', error_message='' "
            "WHERE id=? AND status IN ('queued','interrupted')",
            (_now_text(), _now_text(), task_id),
        )
        if cursor.rowcount != 1:
            return None
        row = self.db.one("SELECT * FROM vision_tasks WHERE id=?", (task_id,))
        return self._row(row) if row is not None else None

    def finish_task(
        self,
        task: TaskRow,
        *,
        status: str,
        suggestion: dict | None = None,
        rejected: list[str] | None = None,
        usage: dict | None = None,
        attempts: int = 0,
        duration_ms: int = 0,
        provider: str = "",
        model: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        suggestion = suggestion or {}
        suggestion_version = (
            hashlib.sha256(
                canonical_json(suggestion).encode("utf-8")
            ).hexdigest()
            if status == "suggestions_ready" and suggestion
            else ""
        )
        # A task cancelled while the request was in flight must never become
        # adoptable; the late result is retained for audit only.
        current = self.db.one(
            "SELECT cancel_requested FROM vision_tasks WHERE id=?",
            (task.id,),
        )
        cancelled = bool(task.cancel_requested) or bool(
            current is not None and current["cancel_requested"]
        )
        final_status = (
            "cancelled" if cancelled and status != "failed" else status
        )
        self.db.execute(
            "UPDATE vision_tasks SET status=?, suggestion_json=?, "
            "rejected_json=?, usage_json=?, attempts=attempts+?, duration_ms=duration_ms+?, "
            "provider=?, model=?, suggestion_version=?, "
            "task_version=task_version+1, error_code=?, error_message=?, "
            "finished_at=?, updated_at=? WHERE id=? AND status='running'",
            (
                final_status,
                json.dumps(
                    suggestion, ensure_ascii=False, separators=(",", ":")
                ),
                json.dumps(
                    rejected or [], ensure_ascii=False, separators=(",", ":")
                ),
                json.dumps(
                    usage or {}, ensure_ascii=False, separators=(",", ":")
                ),
                int(attempts),
                int(duration_ms),
                provider,
                model,
                suggestion_version,
                error_code,
                error_message[:500],
                _now_text(),
                _now_text(),
                task.id,
            ),
        )

    def cancel_task(
        self, owner: str, project_id: str, task_id: str
    ) -> TaskRow | None:
        task = self.get_task(owner, project_id, task_id)
        if task is None:
            return None
        if task.status in {"queued", "interrupted"}:
            self.db.execute(
                "UPDATE vision_tasks SET status='cancelled', "
                "cancel_requested=1, updated_at=? WHERE id=?",
                (_now_text(), task_id),
            )
        elif task.status == "running":
            self.db.execute(
                "UPDATE vision_tasks SET cancel_requested=1, updated_at=? "
                "WHERE id=?",
                (_now_text(), task_id),
            )
        return self.get_task(owner, project_id, task_id)

    def retry_task(
        self, owner: str, project_id: str, task_id: str
    ) -> TaskRow | None:
        task = self.get_task(owner, project_id, task_id)
        if task is None:
            return None
        self.require_enabled(owner, project_id)
        if task.status not in TASK_FAILED:
            raise VisionTaskConflict("只有失败/取消/中断的任务可以重试")
        self.db.execute(
            "UPDATE vision_tasks SET status='queued', cancel_requested=0, "
            "error_code='', error_message='', updated_at=? WHERE id=?",
            (_now_text(), task_id),
        )
        return self.get_task(owner, project_id, task_id)


def run_task(
    service: VisionService,
    task_id: str,
    *,
    config: VisionConfig | None = None,
    transport=None,
) -> None:
    task = service.claim_task(task_id)
    if task is None:
        return
    try:
        if not service.enabled_for_task(task.owner, task.project_id):
            service.finish_task(
                task, status="failed", error_code="vision_disabled",
                error_message="视觉辅助已关闭，请先在账号设置中启用后重试",
            )
            return
        images = []
        for descriptor in task.images:
            png_path = Path(task.crop_dir) / "crop.png"
            manifest = dict(descriptor.get("manifest") or {})
            images.append(
                {
                    "png": png_path.read_bytes(),
                    "cropId": descriptor.get("cropId"),
                    "mediaType": descriptor.get("mediaType") or "image/png",
                    "manifest": manifest,
                }
            )
        def before_request() -> None:
            if not service.enabled_for_task(task.owner, task.project_id):
                raise VisionUnavailable("vision_disabled")

        advisor = VisionAdvisor(
            config or VisionConfig.from_env(), transport=transport,
            before_request=before_request,
        )
        result = advisor.advise(task.task, images=images)
        if result.status == "ok":
            service.finish_task(
                task,
                status="suggestions_ready",
                suggestion=result.suggestion,
                rejected=result.rejected,
                usage=result.usage,
                attempts=result.attempts,
                duration_ms=result.durationMs,
                provider=str(result.identity.get("provider") or ""),
                model=str(result.identity.get("model") or ""),
            )
        else:
            detail = "；".join(result.rejected[:3]) or (
                result.reason or "视觉模型调用失败"
            )
            if result.reason == "vision_disabled":
                detail = "视觉辅助已关闭，请先在账号设置中启用后重试"
            service.finish_task(
                task,
                status="failed",
                rejected=result.rejected,
                usage=result.usage,
                attempts=result.attempts,
                duration_ms=result.durationMs,
                provider=str(result.identity.get("provider") or ""),
                model=str(result.identity.get("model") or ""),
                error_code=result.reason or "vision_failed",
                error_message=detail,
            )
    except Exception as exc:  # noqa: BLE001 - task failure must remain visible
        service.finish_task(
            task,
            status="failed",
            error_code="internal_error",
            error_message=str(exc)[:300],
        )


def _apply_equipment(
    group: dict,
    suggestion: dict,
    human: dict,
    rows: list[dict],
) -> dict:
    before = {
        "model": group.get("model"),
        "role": group.get("role"),
        "priceRow": group.get("priceRow"),
        "include": group.get("include"),
        "quantity": group.get("quantity"),
    }
    model = str(human.get("model") or "").strip()
    if not model:
        model_value = suggestion.get("modelSuggestion")
        if isinstance(model_value, dict) and model_value.get("candidateId") in (
            suggestion.get("candidateIds") or []
        ):
            model = str(model_value.get("model") or "").strip()
    if model:
        group["model"] = model
        group["label"] = model
        group["modelSource"] = "visual_human" if human.get("model") else "visual_model"
    role = str(human.get("role") or suggestion.get("role") or "").strip()
    if role in ALLOWED_ROLES and role != "unknown":
        group["role"] = role
    price_row, confidence = _equipment_match(
        str(group.get("model") or ""),
        rows,
        str(group.get("systemHint") or "refrigerant"),
    )
    group["priceRow"] = (price_row or {}).get("rowNumber")
    group["priceLabel"] = _row_label(price_row) if price_row else ""
    group["confidence"] = confidence
    group["unit"] = (price_row or {}).get("unit") or group.get("unit") or "台"
    group["include"] = bool(
        price_row is not None and confidence in {"exact", "rule"}
    )
    after = {
        "model": group.get("model"),
        "role": group.get("role"),
        "priceRow": group.get("priceRow"),
        "include": group.get("include"),
        "quantity": group.get("quantity"),
    }
    return {"before": before, "after": after}


def _apply_route(
    group: dict,
    suggestion: dict,
    human: dict,
    rows: list[dict],
    scale,
) -> dict:
    before = {
        "category": group.get("category"),
        "medium": group.get("medium"),
        "spec": group.get("spec"),
        "role": group.get("role"),
        "priceRow": group.get("priceRow"),
        "include": group.get("include"),
        "lengthM": group.get("lengthM"),
    }
    category = str(human.get("category") or suggestion.get("category") or "").strip()
    if category in ALLOWED_CATEGORIES:
        group["category"] = "冷媒管" if category == "pipe" else category
    medium = str(human.get("medium") or suggestion.get("medium") or "").strip()
    if medium in ALLOWED_MEDIA and medium != "unknown":
        group["medium"] = medium
    role = str(human.get("role") or suggestion.get("role") or "").strip()
    if role in ALLOWED_ROLES and role != "unknown":
        group["role"] = role
    spec = str(human.get("spec") or "").strip()
    if spec:
        group["spec"] = spec
        group["specSource"] = "human_visual"
    price_row, confidence = _route_match(group, rows)
    linked = _insulation_match(group, rows)
    incompatible = (
        route_row_error(group, price_row) if price_row is not None else None
    )
    group["priceRow"] = (price_row or {}).get("rowNumber")
    group["priceLabel"] = _row_label(price_row) if price_row else ""
    group["confidence"] = confidence
    group["unit"] = (price_row or {}).get("unit") or group.get("unit") or "米"
    group["linkedMaterial"] = linked
    group["linkedMaterialMissing"] = bool(
        linked is None and "保温" not in str(group.get("category") or "")
    )
    proposal_key = route_proposal_key(group)
    group["routeProposalKey"] = proposal_key
    group["routeReview"] = {
        "confirmed": True,
        "evidenceKey": str(group.get("routeEvidenceKey") or ""),
        "proposalKey": proposal_key,
        "source": "vision_human",
    }
    length_known = group.get("lengthM") not in (None, "")
    scale_known = scale not in (None, "", "0", 0)
    group["include"] = bool(
        price_row is not None
        and length_known
        and scale_known
        and incompatible is None
        and group.get("medium") not in (None, "", "unknown")
        and str(group.get("spec") or "").strip()
        and str(group.get("category") or "") not in {"wall", "annotation", "leader"}
    )
    after = {
        "category": group.get("category"),
        "medium": group.get("medium"),
        "spec": group.get("spec"),
        "role": group.get("role"),
        "priceRow": group.get("priceRow"),
        "include": group.get("include"),
        "lengthM": group.get("lengthM"),
    }
    return {
        "before": before,
        "after": after,
        "incompatible": incompatible,
    }


def _authority_routes(inspection: dict) -> dict[str, dict]:
    return {
        str(item.get("key")): item
        for item in (inspection.get("report") or {}).get("routeGroups", [])
        if item.get("key")
    }


def _derived_routes(decisions: list[dict], inspection: dict) -> dict[str, dict]:
    authoritative = _authority_routes(inspection)
    derived: dict[str, dict] = {}
    for item in decisions:
        if item.get("kind") != "route":
            continue
        key = str(item.get("sourceKey") or "")
        original = authoritative.get(key)
        if original is None:
            continue
        after = item.get("after") or {}
        derived[key] = {
            "category": after.get("category"),
            "medium": after.get("medium"),
            "spec": after.get("spec"),
            "role": after.get("role"),
        }
    return derived


def _pricing_preview(
    *,
    project_name: str,
    data: dict,
    template_path: Path,
    price_book: dict,
    inspection: dict,
    derived_routes: dict[str, dict],
    workdir: Path,
    label: str = "after",
) -> dict:
    authoritative = _authority_routes(inspection)
    merged = {}
    for key, value in authoritative.items():
        merged[key] = {
            **value,
            **{
                field: derived_routes.get(key, {}).get(field, value.get(field))
                for field in ("category", "medium", "spec", "role")
            },
        }
    out_dir = workdir / label
    result = generate_generic_quote(
        project_name=project_name,
        draft={"data": data},
        template_path=template_path,
        out_dir=out_dir,
        price_book=price_book,
        authoritative_routes=merged,
        manual_route_records=dict(data.get("manualRoutes") or {}),
        project_id="",
    )
    payload = json.loads(result.quote_json.read_text(encoding="utf-8"))
    rows = []
    for item in payload.get("items") or payload.get("rows") or []:
        if isinstance(item, dict):
            rows.append(item)
    return {
        "subtotal": str(result.pricing.subtotal),
        "tax": str(result.pricing.tax),
        "total": str(result.pricing.total),
        "payload": payload,
        "rows": rows,
    }


def apply_decisions(
    service: VisionService,
    *,
    owner: str,
    project_id: str,
    project_name: str,
    task_id: str,
    draft: dict,
    inspection: dict,
    price_book: dict,
    template_path: Path,
    task_version: int,
    suggestion_version: str,
    expected_revision: int,
    decisions: list[dict],
    actor: str,
) -> dict:
    task = service.get_task(owner, project_id, task_id)
    if task is None:
        raise VisionTaskConflict("视觉任务不存在")
    if task.status != "suggestions_ready":
        raise VisionTaskConflict("视觉任务尚未成功完成，不能采纳")
    if int(task_version) != task.task_version:
        raise VisionTaskConflict("任务版本已变化，请刷新后重试")
    if str(suggestion_version) != task.suggestion_version:
        raise VisionTaskConflict("建议版本已变化，请刷新后重试")
    if (
        actor != owner
        or str(draft.get("projectId") or "") != project_id
        or not task.draft_id
        or str(draft.get("id") or "") != task.draft_id
    ):
        raise VisionTaskConflict("视觉任务不属于当前账号、项目或目标草稿")
    if (
        str(inspection.get("id") or "") != task.inspection_id
        or str(inspection.get("projectId") or "") != project_id
        or inspection_version(inspection) != task.inspection_version
    ):
        raise VisionTaskConflict("图纸检查内容已变化，请重新创建视觉任务")
    if str(draft.get("inspectionId") or "") != task.inspection_id and (
        task.inspection_id not in (draft.get("data") or {}).get("inspectionIds", [])
    ):
        raise VisionTaskConflict("目标草稿不属于这次图纸检查")
    if int(expected_revision) != int(draft.get("revision") or 1):
        raise RevisionConflict("草稿已被其他操作修改，请刷新后重试")
    target_revision = task.task.get("targetDraftRevision")
    if target_revision is None:
        raise VisionTaskConflict("旧视觉任务缺少目标草稿版本，请重新创建任务")
    if int(target_revision) != int(expected_revision):
        raise RevisionConflict("草稿在视觉任务创建后已修改，请重新创建任务")
    items = suggestion_items(task.id, task.suggestion)
    by_id = {item["suggestionItemId"]: item for item in items}
    candidates = {item["candidateId"]: item for item in task.candidates}
    rows = list((price_book.get("book") or {}).get("rows") or [])
    data = json.loads(json.dumps(draft.get("data") or {}, ensure_ascii=False))
    equipment = {
        str(item.get("key")): item for item in data.get("equipment") or []
    }
    routes = {
        str(item.get("key")): item for item in data.get("routes") or []
    }
    decision_items: list[dict] = []
    changed: list[dict] = []
    accepted_records: list[dict] = []
    for request_item in decisions:
        item_id = str(request_item.get("suggestionItemId") or "")
        suggestion = by_id.get(item_id)
        if suggestion is None:
            raise VisionTaskConflict("建议条目不在当前任务中")
        decision = str(request_item.get("decision") or "pending")
        if decision not in {"accept", "reject", "pending"}:
            raise VisionTaskConflict("决策只能是接受、拒绝或待核")
        human = dict(request_item.get("humanEdits") or {})
        allowed_human = {"category", "medium", "role", "model", "spec"}
        if set(human) - allowed_human:
            raise VisionTaskConflict("人工修改包含不允许的字段")
        for field, value in human.items():
            if value not in (None, "") and not isinstance(value, str):
                raise VisionTaskConflict("人工修改字段必须是文本")
            if isinstance(value, str) and len(value) > 200:
                raise VisionTaskConflict("人工修改文本过长")
        model_suggestion = dict(suggestion.get("modelSuggestion") or {})
        decision_item = {
            "suggestionItemId": item_id,
            "candidateIds": list(suggestion.get("candidateIds") or []),
            "decision": decision,
            "modelSuggestion": model_suggestion,
            "humanEdits": human,
            "before": {},
            "after": {},
            "actor": actor,
        }
        if decision == "accept":
            for candidate_id in suggestion.get("candidateIds") or []:
                candidate = candidates.get(candidate_id)
                if candidate is None:
                    raise VisionTaskConflict("建议引用的候选已变化")
                source_key = str(candidate.get("sourceKey") or "")
                target = (
                    equipment.get(source_key)
                    if candidate.get("kind") == "equipment"
                    else routes.get(source_key)
                )
                if target is None:
                    raise VisionTaskConflict(
                        "目标草稿中没有这条候选，请重新创建草稿"
                    )
                if candidate.get("kind") == "equipment":
                    result = _apply_equipment(
                        target, suggestion, human, rows
                    )
                    result["candidateId"] = candidate_id
                    result["kind"] = "equipment"
                    result["sourceKey"] = source_key
                else:
                    result = _apply_route(
                        target,
                        suggestion,
                        human,
                        rows,
                        data.get("scale"),
                    )
                    result["candidateId"] = candidate_id
                    result["kind"] = "route"
                    result["sourceKey"] = source_key
                decision_item.setdefault("before", {})[
                    candidate_id
                ] = result["before"]
                decision_item.setdefault("after", {})[
                    candidate_id
                ] = result["after"]
                accepted_records.append(result)
                changed.append(result)
        decision_items.append(decision_item)
    if not accepted_records:
        # Rejecting or leaving everything pending is a valid human decision;
        # record it without touching the draft or quote.
        adoption_id = (
            f"vadopt-{_now():%Y%m%d-%H%M%S}-"
            f"{hashlib.sha256((task.id+str(expected_revision)+'nochange').encode()).hexdigest()[:6]}"
        )
        adoption = {
            "id": adoption_id,
            "owner": owner,
            "projectId": project_id,
            "inspectionId": task.inspection_id,
            "draftId": str(draft.get("id") or ""),
            "taskId": task.id,
            "taskVersion": task.task_version,
            "suggestionVersion": task.suggestion_version,
            "status": "rejected",
            "beforeRevision": int(draft.get("revision") or 1),
            "afterRevision": int(draft.get("revision") or 1),
            "beforeData": draft.get("data") or {},
            "afterData": draft.get("data") or {},
            "decisions": decisions,
            "derived": {"routes": {}, "equipment": []},
            "derivedHash": hashlib.sha256(
                canonical_json(
                    {
                        "adoptionId": adoption_id,
                        "inspectionVersion": task.inspection_version,
                        "routes": {},
                        "equipment": [],
                    }
                ).encode("utf-8")
            ).hexdigest(),
            "decisionItems": decision_items,
        }
        from .new_quote_store import NewQuoteStore

        NewQuoteStore(service.db).record_vision_adoption(adoption)
        return {
            "adoptionId": adoption_id,
            "derivedHash": adoption["derivedHash"],
            "draft": draft,
            "preview": {
                "adoptionId": adoption_id,
                "requiresConfirmation": False,
                "noChange": True,
                "changed": [],
                "pricing": {},
            },
            "changed": [],
        }
    # A selected CAD candidate may already be a physical member of an adopted
    # priced set. Standalone rematching above must not make that member billable
    # again. Use the same reconciliation as form saving and quote generation
    # before persisting either the draft or the decision's displayed diff.
    data = reconcile_equipment_sets(
        data, {int(row["rowNumber"]): row for row in rows}
    )
    normalized_equipment = {
        str(group.get("key")): group for group in data.get("equipment") or []
    }
    for record in accepted_records:
        if record.get("kind") != "equipment":
            continue
        group = normalized_equipment.get(str(record.get("sourceKey")))
        if group is None:
            raise VisionTaskConflict("成套复核后目标设备已变化，请重新核对草稿")
        record["after"] = {
            key: group.get(key) for key in record["after"]
        }
        for item in decision_items:
            if record["candidateId"] in item["after"]:
                item["after"][record["candidateId"]] = record["after"]
    derived = _derived_routes(accepted_records, inspection)
    adoption_id = (
        f"vadopt-{_now():%Y%m%d-%H%M%S}-"
        f"{hashlib.sha256((task.id+str(expected_revision)).encode()).hexdigest()[:6]}"
    )
    derived_hash = hashlib.sha256(
        canonical_json(
            {
                "adoptionId": adoption_id,
                "inspectionVersion": task.inspection_version,
                "routes": derived,
                "equipment": [
                    {
                        "sourceKey": item.get("sourceKey"),
                        "after": item.get("after"),
                    }
                    for item in changed
                    if item.get("kind") == "equipment"
                ],
            }
        ).encode("utf-8")
    ).hexdigest()
    before_data = json.loads(
        json.dumps(draft.get("data") or {}, ensure_ascii=False)
    )
    data.setdefault("vision", {})
    data["vision"] = {
        **dict(data.get("vision") or {}),
        "lastTaskId": task.id,
        "lastAdoptionId": adoption_id,
        "suggestionVersion": task.suggestion_version,
        "appliedAt": _now_text(),
    }
    preview = {"beforeTotal": None, "afterTotal": None, "error": ""}
    workdir = (service.task_root / task.id / "preview").resolve()
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    from .new_quote_store import NewQuoteStore

    before_overrides = NewQuoteStore(
        service.db
    ).active_vision_route_overrides(project_id, str(draft.get("id") or ""))
    try:
        before = _pricing_preview(
            project_name=project_name,
            data=before_data,
            template_path=template_path,
            price_book=price_book,
            inspection=inspection,
            derived_routes=before_overrides,
            workdir=workdir,
            label="before",
        )
        preview["beforeTotal"] = before["total"]
        preview["beforeSubtotal"] = before["subtotal"]
        preview["beforeTax"] = before["tax"]
    except Exception as exc:  # noqa: BLE001
        preview["beforeError"] = str(exc)[:500]
    try:
        after = _pricing_preview(
            project_name=project_name,
            data=data,
            template_path=template_path,
            price_book=price_book,
            inspection=inspection,
            derived_routes=derived,
            workdir=workdir,
        )
        preview["afterTotal"] = after["total"]
        preview["afterSubtotal"] = after["subtotal"]
        preview["afterTax"] = after["tax"]
        preview["afterRows"] = after["rows"][:200]
        if preview.get("beforeTotal") not in (None, ""):
            try:
                preview["deltaTotal"] = str(
                    Decimal(after["total"]) - Decimal(preview["beforeTotal"])
                )
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        preview["error"] = str(exc)[:500]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    data["visionPreview"] = {
        "adoptionId": adoption_id,
        "taskId": task.id,
        "beforeRevision": int(draft.get("revision") or 1),
        "afterRevision": int(draft.get("revision") or 1) + 1,
        "changed": [
            {
                "candidateId": item.get("candidateId"),
                "kind": item.get("kind"),
                "sourceKey": item.get("sourceKey"),
                "before": item.get("before"),
                "after": item.get("after"),
            }
            for item in changed
        ],
        "pricing": preview,
        "requiresConfirmation": True,
        "createdAt": _now_text(),
    }
    exceptions = draft_exceptions(data, price_book)
    adoption = {
        "id": adoption_id,
        "owner": owner,
        "projectId": project_id,
        "inspectionId": task.inspection_id,
        "draftId": str(draft.get("id") or ""),
        "taskId": task.id,
        "taskVersion": task.task_version,
        "suggestionVersion": task.suggestion_version,
        "beforeRevision": int(draft.get("revision") or 1),
        "afterRevision": int(draft.get("revision") or 1) + 1,
        "beforeData": before_data,
        "afterData": data,
        "decisions": decisions,
        "derived": {
            "routes": derived,
            "equipment": [
                item for item in changed if item.get("kind") == "equipment"
            ],
        },
        "derivedHash": derived_hash,
        "decisionItems": decision_items,
    }
    # The draft update, adoption record and decision rows share one transaction.
    from .new_quote_store import NewQuoteStore

    store = NewQuoteStore(service.db)
    snapshot = store.update_draft(
        project_id,
        str(draft.get("id") or ""),
        data=data,
        expected_revision=int(expected_revision),
        exceptions=exceptions,
        vision_adoption=adoption,
    )
    return {
        "adoptionId": adoption_id,
        "derivedHash": derived_hash,
        "draft": snapshot,
        "preview": data["visionPreview"],
        "changed": changed,
    }
