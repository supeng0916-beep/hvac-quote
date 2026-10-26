"""本地报价工作台：登录、项目归档、报价生成与下载。"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlencode
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from . import ui
from .audit import ACTION_LABELS, AuditLog
from .bulk_supplement import (
    build_missing_price_workbook,
    apply_supplement,
    build_supplement_workbook,
    parse_supplement,
)
from .copper_prices import CopperPriceStore
from .customers import CUSTOMER_ID_RE, CustomerStore
from .contracts import CONTRACT_ID_RE, ContractStore
from .auth import (
    COOKIE_NAME,
    SESSION_TTL_SECONDS,
    AuthError,
    AuthStore,
    LoginThrottle,
)
from .db import Database
from .drawing_adapter import INSPECTION_ENGINE_VERSION, inspect_drawing
from hvac_cad.reader import CadReadError, read_preflight
from .generic_quote import (
    build_draft_data,
    build_multi_draft_data,
    draft_exceptions,
    apply_source_multipliers,
    apply_source_scales,
    blocking_exceptions,
    generate_generic_quote,
    link_route_material,
    reconcile_equipment_sets,
    route_proposal_key,
    route_row_error,
)
from .uploads import UploadValidationError, validate_upload
from .vision import VisionConfig
from .vision_service import (
    VisionService,
    VisionTaskConflict,
    apply_decisions,
    candidate_snapshot,
    get_preferences,
    inspection_version,
    run_task,
    set_preference,
    suggestion_items,
)
from .mapping_profiles import MappingProfileStore, customer_key
from .new_quote_store import (
    NewQuoteStore,
    PublicationConflict,
    RevisionConflict,
)
from .pipeline import RunResult
from .projects import (
    PROJECT_ID_RE,
    RUN_ID_RE,
    Project,
    ProjectStore,
    QuoteRecord,
)
from .template_book import (
    find_matching_mapping,
    import_price_book_auto,
    import_price_book_mapped,
    inspect_workbook_tables,
)
from .template_preferences import (
    TemplatePreferenceError,
    TemplatePreferenceStore,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"
def default_template_path(data_root: Path) -> Path | None:
    configured = os.environ.get("HVAC_DEFAULT_TEMPLATE", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        [
            Path(data_root) / "default-template.xlsx",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None

ALLOWED_VERSIONS = {"base", "v2"}
ALLOWED_DOWNLOADS = {"quote.xlsx", "quote.pdf", "quote.json", "quote-second.json"}
CUSTOMER_DOWNLOADS = {
    "quote.xlsx",
    "quote.pdf",
    "quote-second.xlsx",
    "quote-second.pdf",
}
PUBLIC_PATHS = {"/", "/login", "/setup", "/help", "/health", "/favicon.ico"}
_PROJECT_SCOPED_PATH_RE = re.compile(r"/projects/([^/]+)(?:/.*)?")
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
UPLOAD_KINDS = {
    "drawing": {".dwg", ".dxf"},
    "template": {".xlsx", ".xlsm"},
    "contract": {".pdf", ".docx", ".doc", ".xlsx", ".xlsm"},
    "reference": {".pdf", ".dwg", ".dxf", ".xlsx", ".xlsm"},
}
UPLOAD_KIND_LABELS = {
    "drawing": "设计图纸",
    "template": "报价模板",
    "contract": "合同/协议",
    "reference": "参考资料",
}


def _cad_error_message(code: str) -> str:
    messages = {
        "ENTITY_LIMIT_EXCEEDED": (
            "图块递归展开达到实体预算上限；请先预检并选择本次范围，"
            "不要反复重试全图"
        ),
        "CONVERSION_TIMEOUT": "DWG 转换超时；请稍后重试或按楼层拆分图纸",
        "CONVERSION_FAILED": "DWG 转换失败；请确认文件完整，或另存为 DWG 2018",
        "CONVERSION_LIMIT_EXCEEDED": "DWG 转换中间文件超过预算；请拆分图纸",
        "CAD_VERSION_UNSUPPORTED": "不支持此 CAD 版本；请另存为 2000–2018",
        "CONVERTER_UNAVAILABLE": "服务器缺少 DWG 转换器；请联系部署人员",
        "CAD_READ_FAILED": "CAD 读取失败；文件可能损坏或包含未支持结构",
        "SOURCE_CHANGED_DURING_READ": "读取期间源文件发生变化；请重新上传",
        "FILE_UNAVAILABLE_OR_TOO_LARGE": "文件不可用或超过大小上限",
        "SCOPE_EMPTY": "所选范围没有可读取的布局；请重新选择范围",
    }
    return messages.get(code, f"图纸检查失败（{code}）")


def _infer_upload_kind(suffix: str) -> str:
    if suffix in {".dwg", ".dxf"}:
        return "drawing"
    if suffix in {".xlsx", ".xlsm"}:
        return "template"
    if suffix == ".pdf":
        return "reference"
    return ""


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _user_payload(user) -> dict:
    return {
        "username": user.username,
        "displayName": user.display_name,
        "isAdmin": bool(user.is_admin),
    }


def _safe_next(value: str) -> str:
    text = (value or "").strip()
    if (
        not text.startswith("/")
        or text.startswith("//")
        or text.startswith("/\\")
    ):
        return "/projects"
    if text.startswith("/login") or text.startswith("/setup"):
        return "/projects"
    return text


def _dec(value: str, label: str) -> Decimal:
    text = (value or "").strip()
    if not text:
        return Decimal("0")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{label}不是有效数字：{text}") from exc
    if number < 0:
        raise ValueError(f"{label}不能为负")
    return number


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))








def _new_run_id() -> str:
    return f"run-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"


def _publication_request_digest(form) -> str:
    items = sorted(
        (str(key), str(value))
        for key, value in form.multi_items()
        if key != "publication_key"
    )
    return hashlib.sha256(
        json.dumps(
            items,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _draft_publication_key(
    new_quotes,
    *,
    owner: str,
    project_id: str,
    draft: dict,
) -> str:
    stable_data = copy.deepcopy(draft.get("data") or {})
    _sync_manual_route_records(stable_data, project_id)
    key, fingerprint = _publication_identity(
        owner=owner,
        project_id=project_id,
        draft_id=str(draft.get("id") or ""),
        expected_revision=int(draft.get("revision") or 1),
        price_book_id=str(draft.get("priceBookId") or ""),
        data=stable_data,
    )
    try:
        new_quotes.ensure_publication_intent(
            key=key,
            owner=owner,
            project_id=project_id,
            draft_id=str(draft.get("id") or ""),
            fingerprint=fingerprint,
        )
    except Exception:  # noqa: BLE001 - the form can still carry a key
        logger.exception("生成请求标识持久化失败")
    return key


def _wants_json(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    return "application/json" in accept


def _vision_task_payload(task) -> dict:
    items = suggestion_items(task.id, task.suggestion)
    return {
        "taskId": task.id,
        "projectId": task.project_id,
        "inspectionId": task.inspection_id,
        "inspectionVersion": task.inspection_version,
        "draftId": task.draft_id,
        "status": task.status,
        "taskVersion": task.task_version,
        "suggestionVersion": task.suggestion_version,
        "provider": task.provider,
        "model": task.model,
        "errorCode": task.error_code,
        "errorMessage": task.error_message,
        "attempts": task.attempts,
        "durationMs": task.duration_ms,
        "usage": task.usage,
        "rejected": task.rejected,
        "cancelRequested": task.cancel_requested,
        "createdAt": task.created_at,
        "updatedAt": task.updated_at,
        "candidates": task.candidates,
        "images": [
            {
                key: value
                for key, value in item.items()
                if key in {"cropId", "mediaType", "pngSha256", "manifestHash", "width", "height"}
            }
            for item in task.images
        ],
        "suggestion": {
            "decisions": items,
            "unresolved": (task.suggestion or {}).get("unresolved") or [],
        },
    }


def _publication_identity(
    *,
    owner: str,
    project_id: str,
    draft_id: str,
    expected_revision: int,
    price_book_id: str,
    data: dict,
) -> tuple[str, str]:
    """Stable idempotency key and content fingerprint for one generation intent."""
    stable = {
        key: value
        for key, value in data.items()
        if key not in {"runId", "secondScheme"}
    }
    payload = {
        "version": "publication-intent-1",
        "owner": owner,
        "projectId": project_id,
        "draftId": draft_id,
        "expectedRevision": int(expected_revision),
        "priceBookId": str(price_book_id or ""),
        "data": stable,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"pub-{fingerprint[:40]}", fingerprint


def _remove_staging_dir(staging_dir: Path) -> None:
    shutil.rmtree(staging_dir, ignore_errors=True)
    parent = staging_dir.parent
    if parent.name == ".staging":
        try:
            parent.rmdir()
        except OSError:
            pass


def _run_dir(runs_root: Path, run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(status_code=404, detail="报价不存在")
    path = (runs_root / run_id).resolve()
    if not path.is_relative_to(runs_root):
        raise HTTPException(status_code=404, detail="报价不存在")
    return path


def _project_files_dir(data_root: Path, project_id: str) -> Path:
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    path = (data_root / "projects" / project_id / "files").resolve()
    if not path.is_relative_to(data_root):
        raise HTTPException(status_code=404, detail="项目不存在")
    return path


def _project_file_path(
    data_root: Path, project_id: str, stored_name: str
) -> Path:
    files_dir = _project_files_dir(data_root, project_id)
    path = (files_dir / stored_name).resolve()
    if not path.is_relative_to(files_dir) or not path.is_file():
        raise HTTPException(status_code=404, detail="资料不存在")
    return path


def _clean_upload_name(name: str | None) -> str:
    text = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return text[:240]


def _read_quote_payload(run_dir: Path) -> dict:
    for candidate in (
        run_dir / "v2" / "quote.json",
        run_dir / "base" / "quote.json",
        run_dir / "quote.json",
    ):
        if candidate.is_file():
            return _read_json(candidate)
    raise HTTPException(status_code=404, detail="报价结果不存在")


def _quote_file(run_dir: Path, filename: str) -> Path | None:
    for candidate in (
        run_dir / "v2" / filename,
        run_dir / "base" / filename,
        run_dir / filename,
    ):
        if candidate.is_file():
            return candidate
    return None


def _quote_download_name(
    project_name: str,
    quote_title: str,
    created_at: str,
    filename: str,
) -> str:
    def clean(value: str, fallback: str) -> str:
        text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", str(value or ""))
        text = re.sub(r"\s+", " ", text).strip(" .")
        return text[:80].strip(" .") or fallback

    project_part = clean(project_name, "项目")
    title_part = clean(quote_title, "报价")
    digits = re.sub(r"\D", "", str(created_at or "")[:16])
    date_part = digits[:12] or "报价"
    if title_part == project_part or project_part in title_part:
        base = f"{project_part}-报价-{date_part}"
    else:
        base = f"{project_part}-{title_part}-{date_part}"
    suffix = "-第二套" if filename.startswith("quote-second") else ""
    extension = Path(filename).suffix or ".pdf"
    return f"{base}{suffix}{extension}"


def _price_row_key(row: dict) -> str:
    text = f"{row.get('name', '')}{row.get('spec', '')}"
    return re.sub(r"\s+", "", text).upper()


def _second_price_overrides(
    first_book: dict, second_book: dict
) -> tuple[dict[int, tuple[Decimal, Decimal]], list[int]]:
    first_rows = list((first_book.get("book") or {}).get("rows") or [])
    second_rows = list((second_book.get("book") or {}).get("rows") or [])
    second_by_row = {
        int(row["rowNumber"]): row
        for row in second_rows
        if row.get("rowNumber") is not None
    }
    second_by_key = {
        _price_row_key(row): row
        for row in second_rows
        if row.get("priceState") == "priced"
    }
    overrides: dict[int, tuple[Decimal, Decimal]] = {}
    changed_rows: list[int] = []
    for row in first_rows:
        if row.get("category") != "pipe" or row.get("priceState") != "priced":
            continue
        row_number = int(row["rowNumber"])
        candidate = second_by_row.get(row_number)
        if (
            candidate is None
            or _price_row_key(candidate) != _price_row_key(row)
        ):
            candidate = second_by_key.get(_price_row_key(row))
        if (
            candidate is None
            or candidate.get("priceState") != "priced"
            or candidate.get("material") is None
            or candidate.get("installation") is None
        ):
            continue
        try:
            material = Decimal(str(candidate["material"]))
            installation = Decimal(str(candidate["installation"]))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if (
            material == Decimal(str(row.get("material")))
            and installation == Decimal(str(row.get("installation")))
        ):
            continue
        overrides[row_number] = (material, installation)
        changed_rows.append(row_number)
    return overrides, changed_rows


def _labels_for(payload: dict) -> dict[int, dict]:
    # A customer's uploaded workbook is not the optional installation template.
    # Quote snapshots must keep displaying the labels used when they were made,
    # including after the account default has changed or the upload is removed.
    if payload.get("scope") == "custom-drawing":
        frozen = payload.get("templateLabels")
        if isinstance(frozen, dict):
            return {int(row): dict(label) for row, label in frozen.items()}
        return {
            int(item["templateRow"]): {
                "name": item.get("displayName") or item.get("spec") or "",
                "spec": item.get("displaySpec") or item.get("spec") or "",
                "unit": item.get("unit") or "",
            }
            for item in payload.get("items", [])
            if item.get("templateRow") is not None
        }
    return {}


def _route_evidence_conflict(
    new_quotes,
    project_id: str,
    data: dict,
    derived_routes: dict[str, dict] | None = None,
) -> str | None:
    """Compare draft route evidence with the persisted inspection snapshot."""
    default_source = str(data.get("inspectionId") or "")
    cache: dict[str, dict[str, dict]] = {}
    for group in data.get("routes") or []:
        if group.get("origin") == "customer_input":
            if not group.get("manualRecordId"):
                return "人工补录缺少服务器记录，请重新补录。"
            continue
        source_id = str(group.get("sourceInspectionId") or default_source)
        if not source_id:
            return "管线组缺少检查来源，请重新检查图纸后再确认。"
        if source_id not in cache:
            inspection = new_quotes.get_inspection(project_id, source_id)
            if inspection is None:
                return "管线组绑定的图纸检查记录不存在，请重新检查图纸。"
            cache[source_id] = {
                str(item.get("key")): item
                for item in (inspection.get("report") or {}).get(
                    "routeGroups", []
                )
            }
        authoritative = cache[source_id].get(str(group.get("key")))
        if authoritative is None:
            return (
                "管线组在当前检查快照中已不存在或已变化，"
                "请重新检查图纸并重新核对。"
            )
        override = (derived_routes or {}).get(str(group.get("key") or "")) or {}
        for field in (
            "routeEvidenceKey",
            "medium",
            "spec",
            "sourceRegionKey",
            "lengthRaw",
        ):
            expected = (
                override.get(field, authoritative.get(field))
                if field in {"medium", "spec"} and override
                else authoritative.get(field)
            )
            if group.get(field) != expected:
                if field == "lengthRaw" and _looks_like_legacy_multiplied(
                    group
                ):
                    return (
                        "检测到旧版代表层数已乘入图面原始量，"
                        "但无法与当前检查快照安全匹配；"
                        "请重新检查图纸后重新核对本组路线。"
                    )
                return (
                    "管线组的检查证据已变化，"
                    "请重新检查图纸后重新核对本组路线。"
                )
        if (
            group.get("eligibility") == "eligible"
            and authoritative.get("eligibility") != "eligible"
        ) or (
            group.get("autoInclude") is True
            and authoritative.get("autoInclude") is not True
        ):
            return (
                "管线组的采用状态不能高于权威检查快照，"
                "请重新检查图纸后重新核对本组路线。"
            )
        if sorted(group.get("reviewReasons") or []) != sorted(
            authoritative.get("reviewReasons") or []
        ):
            return (
                "管线组的审查原因已变化，"
                "请重新检查图纸后重新核对本组路线。"
            )
        if sorted(
            str(item.get("physicalSegmentId"))
            for item in group.get("segments") or []
            if item.get("physicalSegmentId")
        ) != sorted(
            str(item.get("physicalSegmentId"))
            for item in authoritative.get("segments") or []
            if item.get("physicalSegmentId")
        ):
            return (
                "管线组的物理段已变化，"
                "请重新检查图纸后重新核对本组路线。"
            )
        if sorted(
            str(item) for item in group.get("sourceHandles") or []
        ) != sorted(
            str(item) for item in authoritative.get("handles") or []
        ):
            return (
                "管线组的来源实体已变化，"
                "请重新检查图纸后重新核对本组路线。"
            )
    return None


def _authoritative_routes(
    new_quotes,
    project_id: str,
    data: dict,
    derived_routes: dict[str, dict] | None = None,
) -> dict[str, dict]:
    default_source = str(data.get("inspectionId") or "")
    source_ids = {
        str(group.get("sourceInspectionId") or default_source)
        for group in data.get("routes") or []
        if group.get("origin") != "customer_input"
    }
    result: dict[str, dict] = {}
    for source_id in source_ids:
        if not source_id:
            continue
        inspection = new_quotes.get_inspection(project_id, source_id)
        if inspection is None:
            continue
        for group in (inspection.get("report") or {}).get(
            "routeGroups", []
        ):
            key = str(group.get("key") or "")
            if not key:
                continue
            merged = dict(group)
            override = (derived_routes or {}).get(key) or {}
            for field in ("category", "medium", "spec", "role"):
                if override.get(field) is not None:
                    merged[field] = override[field]
            result[key] = merged
    return result


def _sync_manual_route_records(
    data: dict, project_id: str = ""
) -> dict[str, dict]:
    """Keep server-owned identities for manual routes inside the draft payload.

    HTTP callers never supply this map; it is rebuilt from the persisted draft
    and the form values the server itself accepted.
    """
    records = dict(data.get("manualRoutes") or {})
    for group in data.get("routes") or []:
        if group.get("origin") != "customer_input":
            continue
        record_id = str(group.get("manualRecordId") or "").strip()
        if not record_id:
            continue
        records[record_id] = {
            "manualRecordId": record_id,
            "key": str(group.get("key") or ""),
            "projectId": project_id,
            "label": str(group.get("label") or ""),
            "category": str(group.get("category") or ""),
            "medium": str(group.get("medium") or ""),
            "spec": str(group.get("manualSpec") or group.get("spec") or ""),
            "manualSpec": str(group.get("manualSpec") or ""),
            "lengthM": group.get("lengthM"),
            "priceRow": group.get("priceRow"),
            "manualMaterial": group.get("manualMaterial"),
            "manualInstallation": group.get("manualInstallation"),
            "manualUnit": str(
                group.get("manualUnit") or group.get("unit") or ""
            ),
            "unit": str(group.get("unit") or ""),
            "createdBy": "server",
        }
    data["manualRoutes"] = records
    return records


def _decimal_value(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _numbers_close(first: Decimal | None, second: Decimal | None) -> bool:
    if first is None or second is None:
        return False
    return abs(first - second) <= max(
        Decimal("1e-9"), max(abs(first), abs(second)) * Decimal("1e-12")
    )


def _legacy_route_identity_matches(group: dict, authoritative: dict) -> bool:
    for field in (
        "routeEvidenceKey",
        "sourceRegionKey",
        "medium",
        "spec",
    ):
        if group.get(field) != authoritative.get(field):
            return False
    if sorted(group.get("reviewReasons") or []) != sorted(
        authoritative.get("reviewReasons") or []
    ):
        return False
    if sorted(
        str(item.get("physicalSegmentId"))
        for item in group.get("segments") or []
        if item.get("physicalSegmentId")
    ) != sorted(
        str(item.get("physicalSegmentId"))
        for item in authoritative.get("segments") or []
        if item.get("physicalSegmentId")
    ):
        return False
    return sorted(
        str(item) for item in group.get("sourceHandles") or []
    ) == sorted(str(item) for item in authoritative.get("handles") or [])


def _looks_like_legacy_multiplied(group: dict) -> bool:
    multiplier = _decimal_value(group.get("sourceMultiplier"))
    base_raw = _decimal_value(group.get("baseLengthRaw"))
    raw = _decimal_value(group.get("lengthRaw"))
    if multiplier is None or base_raw is None or raw is None:
        return False
    return multiplier > 1 and _numbers_close(raw, base_raw * multiplier)


def _restore_legacy_multiplied_routes(
    new_quotes, project_id: str, data: dict
) -> int:
    """Recover 0.4.10 drafts whose raw length was multiplied in place.

    The stored baseLengthRaw is only a hint. The immutable value is the
    authoritative inspection raw; the draft is restored only when the
    evidence identity, base raw, multiplier and effective length all agree.
    """
    default_source = str(data.get("inspectionId") or "")
    cache: dict[str, dict[str, dict]] = {}
    recovered = 0
    for group in data.get("routes") or []:
        if group.get("origin") == "customer_input":
            continue
        source_id = str(group.get("sourceInspectionId") or default_source)
        if not source_id:
            continue
        if source_id not in cache:
            inspection = new_quotes.get_inspection(project_id, source_id)
            if inspection is None:
                cache[source_id] = {}
            else:
                cache[source_id] = {
                    str(item.get("key")): item
                    for item in (inspection.get("report") or {}).get(
                        "routeGroups", []
                    )
                }
        authoritative = cache[source_id].get(str(group.get("key") or ""))
        if authoritative is None:
            continue
        if not _legacy_route_identity_matches(group, authoritative):
            continue
        authoritative_raw = _decimal_value(authoritative.get("lengthRaw"))
        raw = _decimal_value(group.get("lengthRaw"))
        base_raw = _decimal_value(group.get("baseLengthRaw"))
        multiplier = _decimal_value(group.get("sourceMultiplier"))
        base_length = _decimal_value(group.get("baseLengthM"))
        effective_length = _decimal_value(group.get("lengthM"))
        if (
            authoritative_raw is None
            or raw is None
            or base_raw is None
            or multiplier is None
            or multiplier <= 1
            or base_length is None
            or effective_length is None
        ):
            continue
        if not _numbers_close(base_raw, authoritative_raw):
            continue
        if not _numbers_close(raw, base_raw * multiplier):
            continue
        if not _numbers_close(effective_length, base_length * multiplier):
            continue
        group["lengthRaw"] = float(authoritative_raw)
        group["baseLengthRaw"] = format(authoritative_raw.normalize(), "f")
        if group.get("lengthKnownRaw") is not None:
            group["lengthKnownRaw"] = float(authoritative_raw)
        group["legacyMultiplierRecovered"] = True
        recovered += 1
    return recovered


def _finalize_route_proposals(
    data: dict,
    previous_data: dict,
    review_requests: dict[int, bool],
) -> bool:
    """Rebind confirmations to the exact effective takeoff proposal.

    Returns True when at least one review group's effective quantity/spec/
    scope changed in this request; the old page checkbox must not confirm it.
    """
    previous_by_key = {
        str(item.get("key")): item
        for item in previous_data.get("routes") or []
        if item.get("key")
    }
    needs_reconfirm = False
    for index, group in enumerate(data.get("routes") or []):
        previous = previous_by_key.get(str(group.get("key") or ""))
        previous_key = ""
        if previous is not None:
            previous_key = str(previous.get("routeProposalKey") or "")
            if not previous_key:
                previous_key = route_proposal_key(previous)
        current_key = route_proposal_key(group)
        group["routeProposalKey"] = current_key
        changed = bool(
            previous is not None
            and previous_key
            and current_key != previous_key
        )
        requested = review_requests.get(index, False)
        if changed:
            group.pop("routeReview", None)
            if group.get("eligibility") == "eligible":
                # A user-edited effective quantity/spec is a new proposal and
                # must be explicitly reviewed, even if CAD evidence was clean.
                group["eligibility"] = "review"
                group["autoInclude"] = False
            needs_reconfirm = True
        elif requested and group.get("routeEvidenceKey"):
            group["routeReview"] = {
                "confirmed": True,
                "evidenceKey": str(group["routeEvidenceKey"]),
                "proposalKey": current_key,
            }
        elif not requested:
            group.pop("routeReview", None)
    return needs_reconfirm


def _friendly_error(exc: Exception, *, action: str = "生成报价") -> str:
    if isinstance(exc, (ValueError, AuthError)):
        return str(exc)
    logger.exception("%s失败", action)
    return f"{action}没有完成。请检查图纸和模板是否完整，然后重试。"








def create_app(
    runs_dir: Path | None = None,
    data_dir: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="HVAC Quote 工程报价")
    runs_root = Path(runs_dir or "runs").resolve()
    runs_root.mkdir(parents=True, exist_ok=True)
    data_root = Path(data_dir or "data").resolve()
    data_root.mkdir(parents=True, exist_ok=True)

    db = Database(data_root / "hvac.db")
    auth = AuthStore(data_root, db=db)
    projects = ProjectStore(data_root, db=db)
    new_quotes = NewQuoteStore(db)
    vision_service = VisionService(db, runs_root)
    vision_executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="hvac-vision"
    )
    mapping_profiles = MappingProfileStore(db)
    template_preferences = TemplatePreferenceStore(data_root, db)
    copper_prices = CopperPriceStore(db)
    customers = CustomerStore(db)
    contracts = ContractStore(db)
    audit = AuditLog(db)
    throttle = LoginThrottle()
    app.state.db = db
    app.state.auth = auth
    app.state.projects = projects
    app.state.new_quotes = new_quotes
    app.state.vision_service = vision_service
    app.state.vision_executor = vision_executor
    app.state.mapping_profiles = mapping_profiles
    app.state.template_preferences = template_preferences
    app.state.copper_prices = copper_prices
    app.state.customers = customers
    app.state.contracts = contracts
    app.state.audit = audit
    app.state.runs_root = runs_root
    app.state.data_root = data_root
    interrupted = vision_service.mark_interrupted()
    if interrupted:
        logger.warning("视觉任务在重启时标记中断：%s", interrupted)
    # 批量检查/导入任务：每项目一个串行任务，状态在内存中，重启即失效可重跑。
    batch_jobs: dict[str, dict] = {}
    batch_lock = threading.Lock()
    app.state.batch_jobs = batch_jobs

    def log_action(
        request: Request,
        action: str,
        *,
        project_id: str | None = None,
        run_id: str | None = None,
        detail: dict | None = None,
    ) -> None:
        user = request.state.user
        audit.log(
            action,
            username=user.username if user else None,
            project_id=project_id,
            run_id=run_id,
            detail=detail,
            client_ip=(
                request.client.host if request.client is not None else None
            ),
        )

    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        user = auth.read_session(request.cookies.get(COOKIE_NAME))
        request.state.user = user

        if request.method == "POST":
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            expected = f"{request.url.scheme}://{request.url.netloc}"
            if origin and origin != expected:
                return HTMLResponse("请求来源无效", status_code=403)
            if referer and not referer.startswith(expected + "/"):
                return HTMLResponse("请求来源无效", status_code=403)

        if path not in PUBLIC_PATHS and not path.startswith("/static/"):
            if user is None:
                if request.method in {"GET", "HEAD"}:
                    target = path
                    if request.url.query:
                        target += "?" + request.url.query
                    return RedirectResponse(
                        f"/login?next={quote(target, safe='/')}",
                        status_code=303,
                    )
                return RedirectResponse("/login", status_code=303)

        # 只读成员不能对项目做任何修改类 POST。
        if request.method == "POST" and user is not None:
            scoped = _PROJECT_SCOPED_PATH_RE.fullmatch(path)
            if scoped and projects.role_for(
                user.username, scoped.group(1)
            ) == "viewer":
                return HTMLResponse(
                    ui.error_page(
                        "这个项目对你是只读共享：可以查看和下载，"
                        "不能上传、修改或删除。",
                        user=_user_payload(user),
                        title="只读项目",
                        back_href=f"/projects/{scoped.group(1)}",
                        back_label="返回项目页",
                    ),
                    status_code=403,
                )

        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def current_user(request: Request):
        user = request.state.user
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录")
        return user

    def admin_user(request: Request):
        user = current_user(request)
        if not user.is_admin:
            # 对非管理员按不存在处理，不暴露管理入口。
            raise HTTPException(status_code=404, detail="页面不存在")
        return user

    def owned_project(request: Request, project_id: str) -> Project:
        user = current_user(request)
        # 访问来源只有两种：项目所有者、被显式添加的项目成员；
        # 账号间严格隔离（含管理员），需要访问就由所有者添加为成员。
        if projects.role_for(user.username, project_id) is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        project = projects.get_by_id(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        return project

    def owned_quote(
        request: Request, project_id: str, run_id: str
    ) -> tuple[Project, QuoteRecord]:
        project = owned_project(request, project_id)
        found = projects.find_quote(project.owner, project_id, run_id)
        if found is None:
            raise HTTPException(status_code=404, detail="报价不存在")
        return found

    def owned_customer(request: Request, customer_id: str) -> dict:
        user = current_user(request)
        if not CUSTOMER_ID_RE.fullmatch(customer_id):
            raise HTTPException(status_code=404, detail="客户不存在")
        customer = customers.get(user.username, customer_id)
        if customer is None:
            raise HTTPException(status_code=404, detail="客户不存在")
        return customer

    def owned_contract(
        request: Request, project_id: str, contract_id: str
    ) -> tuple[Project, dict]:
        project = owned_project(request, project_id)
        user = current_user(request)
        if not CONTRACT_ID_RE.fullmatch(contract_id):
            raise HTTPException(status_code=404, detail="合同不存在")
        contract = contracts.get(project.owner, project_id, contract_id)
        if contract is None:
            raise HTTPException(status_code=404, detail="合同不存在")
        return project, contract


    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/favicon.ico")
    def favicon() -> RedirectResponse:
        return RedirectResponse("/static/favicon.svg", status_code=307)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> str:
        user = request.state.user
        return ui.landing_page(
            _user_payload(user) if user else None,
            setup_available=not auth.has_users(),
        )

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request) -> str:
        user = request.state.user
        return ui.help_page(_user_payload(user) if user else None)



    @app.get("/login", response_class=HTMLResponse)
    def login_get(request: Request, next: str = "/projects"):
        if request.state.user is not None:
            return RedirectResponse("/projects", status_code=303)
        if not auth.has_users():
            return RedirectResponse("/setup", status_code=303)
        return HTMLResponse(
            ui.login_page(
                next_path=_safe_next(next),
                setup_available=False,
            )
        )

    @app.post("/login", response_class=HTMLResponse)
    def login_post(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        next: str = Form("/projects"),
    ):
        if not auth.has_users():
            return RedirectResponse("/setup", status_code=303)
        key = (
            (request.client.host if request.client else "local")
            + ":"
            + username.strip().lower()
        )
        retry = throttle.retry_after(key)
        if retry:
            return HTMLResponse(
                ui.login_page(
                    error=f"尝试次数过多，请 {retry} 秒后再试。",
                    username=username,
                    next_path=_safe_next(next),
                ),
                status_code=429,
            )
        user = auth.verify(username, password)
        if user is None:
            throttle.fail(key)
            audit.log(
                "login_failed",
                username=username.strip()[:32],
                client_ip=(
                    request.client.host if request.client is not None else None
                ),
                detail={"reason": "invalid_credentials"},
            )
            return HTMLResponse(
                ui.login_page(
                    error="用户名或密码不正确。",
                    username=username,
                    next_path=_safe_next(next),
                ),
                status_code=401,
            )
        throttle.reset(key)
        audit.log(
            "login_success",
            username=user.username,
            client_ip=(
                request.client.host if request.client is not None else None
            ),
        )
        response = RedirectResponse(_safe_next(next), status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            auth.issue_session(user),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/setup", response_class=HTMLResponse)
    def setup_get(request: Request):
        if auth.has_users():
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(ui.setup_page())

    @app.post("/setup", response_class=HTMLResponse)
    def setup_post(
        request: Request,
        display_name: str = Form(""),
        username: str = Form(""),
        password: str = Form(""),
        password_confirm: str = Form(""),
    ):
        if auth.has_users():
            return RedirectResponse("/login", status_code=303)
        values = {"display_name": display_name, "username": username}
        if password != password_confirm:
            return HTMLResponse(
                ui.setup_page(error="两次输入的密码不一致。", values=values),
                status_code=400,
            )
        try:
            user = auth.create_user(username, password, display_name)
        except AuthError as exc:
            return HTMLResponse(
                ui.setup_page(error=str(exc), values=values),
                status_code=400,
            )
        audit.log(
            "setup",
            username=user.username,
            client_ip=(
                request.client.host if request.client is not None else None
            ),
            detail={"displayName": user.display_name},
        )
        response = RedirectResponse("/projects", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            auth.issue_session(user),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/account", response_class=HTMLResponse)
    def account_get(request: Request, changed: str = "") -> HTMLResponse:
        user = current_user(request)
        return HTMLResponse(
            ui.account_page(
                _user_payload(user),
                changed=bool(changed),
                default_template=template_preferences.get(user.username),
                vision_enabled=bool(
                    get_preferences(db, user.username).get("visionEnabled")
                ),
            )
        )

    @app.post("/account", response_class=HTMLResponse)
    def account_post(
        request: Request,
        current_password: str = Form(""),
        new_password: str = Form(""),
        password_confirm: str = Form(""),
    ) -> HTMLResponse:
        user = current_user(request)
        error = None
        if auth.verify(user.username, current_password) is None:
            error = "当前密码不正确"
        elif new_password != password_confirm:
            error = "两次输入的密码不一致"
        else:
            try:
                auth.set_password(user.username, new_password)
            except AuthError as exc:
                error = str(exc)
        if error:
            return HTMLResponse(
                ui.account_page(_user_payload(user), error=error),
                status_code=400,
            )
        log_action(request, "password_changed")
        return RedirectResponse("/account?changed=1", status_code=303)

    @app.post("/account/vision-preference")
    def account_vision_preference(
        request: Request, enabled: str = Form("")
    ):
        user = current_user(request)
        value = str(enabled).strip().lower() in {"1", "on", "true", "yes"}
        set_preference(db, user.username, "visionEnabled", value)
        log_action(
            request,
            "vision_preference_changed",
            detail={"enabled": value},
        )
        return RedirectResponse("/account?vision=1", status_code=303)

    @app.post("/account/default-template/clear")
    def account_default_template_clear(request: Request):
        user = current_user(request)
        template_preferences.clear(user.username)
        log_action(request, "default_template_cleared")
        return RedirectResponse("/account", status_code=303)

    @app.post("/projects/{project_id}/price-books/{price_book_id}/set-default")
    def account_default_template_set(
        request: Request, project_id: str, price_book_id: str
    ):
        project = owned_project(request, project_id)
        user = current_user(request)
        if user.username != project.owner:
            raise HTTPException(
                status_code=403,
                detail="只有项目所有者可以设置账号默认模板",
            )
        price_book = new_quotes.get_price_book(project_id, price_book_id)
        if price_book is None:
            raise HTTPException(status_code=404, detail="价格书不存在")
        found = projects.find_file(
            project.owner, project_id, price_book["fileId"]
        )
        if found is None:
            raise HTTPException(status_code=404, detail="模板文件不存在")
        _, item = found
        path = _project_file_path(data_root, project_id, item.stored_name)
        try:
            template_preferences.remember(
                owner=project.owner,
                project_id=project.id,
                file_name=item.original_name,
                file_bytes=path.read_bytes(),
                file_sha256=item.sha256,
                price_book_name=price_book["name"],
                book=price_book["book"],
            )
        except (OSError, TemplatePreferenceError) as exc:
            logger.exception("设置账号默认模板失败")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log_action(
            request,
            "default_template_set",
            project_id=project.id,
            detail={"priceBookId": price_book_id, "sha256": item.sha256},
        )
        return RedirectResponse(
            f"/projects/{project.id}/price-books/{price_book_id}",
            status_code=303,
        )

    def _users_table_payload() -> list[dict]:
        return [
            {
                "username": item.username,
                "displayName": item.display_name,
                "createdAt": item.created_at.replace("T", " ")[:19],
                "isAdmin": item.is_admin,
            }
            for item in auth.list_users()
        ]

    @app.get("/admin/users", response_class=HTMLResponse)
    def admin_users_get(
        request: Request, created: str = "", reset: str = ""
    ) -> HTMLResponse:
        user = admin_user(request)
        return HTMLResponse(
            ui.admin_users_page(
                _user_payload(user),
                _users_table_payload(),
                created=created,
                reset=reset,
            )
        )

    @app.post("/admin/users", response_class=HTMLResponse)
    def admin_users_post(
        request: Request,
        username: str = Form(""),
        display_name: str = Form(""),
        password: str = Form(""),
        password_confirm: str = Form(""),
    ) -> HTMLResponse:
        user = admin_user(request)
        values = {"username": username, "display_name": display_name}
        error = None
        if password != password_confirm:
            error = "两次输入的密码不一致"
        else:
            try:
                auth.create_user(username, password, display_name)
            except AuthError as exc:
                error = str(exc)
        if error:
            return HTMLResponse(
                ui.admin_users_page(
                    _user_payload(user),
                    _users_table_payload(),
                    error=error,
                    values=values,
                ),
                status_code=400,
            )
        target = username.strip()
        log_action(request, "user_created", detail={"username": target})
        return RedirectResponse(
            f"/admin/users?created={quote(target, safe='')}", status_code=303
        )

    @app.post("/admin/users/{username}/password", response_class=HTMLResponse)
    def admin_reset_password(
        request: Request,
        username: str,
        new_password: str = Form(""),
        password_confirm: str = Form(""),
    ) -> HTMLResponse:
        user = admin_user(request)
        error = None
        if new_password != password_confirm:
            error = "两次输入的密码不一致"
        else:
            try:
                auth.set_password(username, new_password)
            except AuthError as exc:
                error = str(exc)
        if error:
            return HTMLResponse(
                ui.admin_users_page(
                    _user_payload(user),
                    _users_table_payload(),
                    error=error,
                ),
                status_code=400,
            )
        target = username.strip()
        log_action(request, "password_reset", detail={"username": target})
        return RedirectResponse(
            f"/admin/users?reset={quote(target, safe='')}", status_code=303
        )

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        if request.state.user is not None:
            log_action(request, "logout")
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response


    @app.get("/admin/logs", response_class=HTMLResponse)
    def admin_logs(request: Request) -> HTMLResponse:
        current_user(request)
        entries = audit.list_recent(limit=300)
        rows = "".join(
            "<tr>"
            f"<td>{ui.esc(entry.created_at.replace('T', ' ')[:19])}</td>"
            f"<td>{ui.esc(entry.username or '—')}</td>"
            f"<td>{ui.esc(ACTION_LABELS.get(entry.action, entry.action))}</td>"
            f"<td>{ui.esc(entry.project_id or '—')}</td>"
            f"<td>{ui.esc(entry.run_id or '—')}</td>"
            f"<td>{ui.esc(entry.client_ip or '—')}</td>"
            f"<td>{ui.esc(json.dumps(entry.detail, ensure_ascii=False) if entry.detail else '—')}</td>"
            "</tr>"
            for entry in entries
        )
        body = f"""
<div class="internal-card">
  <div style="display:flex;align-items:center;justify-content:space-between;gap:12px">
    <div><h2 style="margin:0">使用日志</h2><p class="muted" style="margin:4px 0 0">最近 {len(entries)} 条；不记录口令和客户文件内容。</p></div>
    <a href="/projects">返回工作台</a>
  </div>
  <div style="overflow-x:auto;margin-top:14px">
    <table class="internal-table">
      <thead><tr><th>时间</th><th>账号</th><th>操作</th><th>项目</th><th>报价</th><th>来源</th><th>详情</th></tr></thead>
      <tbody>{rows or '<tr><td colspan="7">还没有日志</td></tr>'}</tbody>
    </table>
  </div>
</div>"""
        return HTMLResponse(ui.internal_page("使用日志", body))



    @app.get("/projects", response_class=HTMLResponse)
    def projects_index(request: Request) -> str:
        user = current_user(request)
        return ui.projects_page(
            _user_payload(user), projects.list_visible(user.username)
        )

    @app.get(
        "/projects/{project_id}/contracts",
        response_class=HTMLResponse,
    )
    def contracts_index(request: Request, project_id: str) -> str:
        project = owned_project(request, project_id)
        return HTMLResponse(
            ui.contracts_page(
                _user_payload(current_user(request)),
                project,
                contracts.list_for_project(project_id),
            )
        )

    @app.post("/projects/{project_id}/contracts")
    def contracts_create(
        request: Request,
        project_id: str,
        title: str = Form(""),
        contract_no: str = Form(""),
        counterparty: str = Form(""),
        signing_entity: str = Form(""),
        amount: str = Form(""),
        settlement_amount: str = Form(""),
        status: str = Form("draft"),
        signed_date: str = Form(""),
        payment_terms: str = Form(""),
        file_id: str = Form(""),
        note: str = Form(""),
    ):
        project = owned_project(request, project_id)
        user = current_user(request)
        values = {
            "title": title,
            "contract_no": contract_no,
            "counterparty": counterparty,
            "signing_entity": signing_entity,
            "amount": amount,
            "settlement_amount": settlement_amount,
            "status": status,
            "signed_date": signed_date,
            "payment_terms": payment_terms,
            "file_id": file_id,
            "note": note,
        }
        if file_id and projects.find_file(
            project.owner, project_id, file_id
        ) is None:
            return HTMLResponse(
                ui.contracts_page(
                    _user_payload(user),
                    project,
                    contracts.list_for_project(project_id),
                    error="所选合同原件不存在。",
                    values=values,
                ),
                status_code=400,
            )
        try:
            contract = contracts.create(
                project.owner,
                project_id=project_id,
                customer_id=project.customer_id,
                title=title,
                contract_no=contract_no,
                counterparty=counterparty,
                signing_entity=signing_entity,
                amount=amount,
                settlement_amount=settlement_amount,
                status=status,
                signed_date=signed_date,
                payment_terms=payment_terms,
                file_id=file_id or None,
                note=note,
            )
        except ValueError as exc:
            return HTMLResponse(
                ui.contracts_page(
                    _user_payload(user),
                    project,
                    contracts.list_for_project(project_id),
                    error=str(exc),
                    values=values,
                ),
                status_code=400,
            )
        log_action(
            request,
            "contract_created",
            project_id=project.id,
            detail={
                "contractId": contract["id"],
                "title": contract["title"],
                "amount": contract["amount"],
            },
        )
        return RedirectResponse(
            f"/projects/{project.id}/contracts/{contract['id']}",
            status_code=303,
        )

    @app.get(
        "/projects/{project_id}/contracts/{contract_id}",
        response_class=HTMLResponse,
    )
    def contract_detail(
        request: Request, project_id: str, contract_id: str
    ) -> str:
        project, contract = owned_contract(request, project_id, contract_id)
        return HTMLResponse(
            ui.contract_detail_page(
                _user_payload(current_user(request)), project, contract
            )
        )

    @app.post(
        "/projects/{project_id}/contracts/{contract_id}",
        response_class=HTMLResponse,
    )
    def contract_update(
        request: Request,
        project_id: str,
        contract_id: str,
        title: str = Form(""),
        contract_no: str = Form(""),
        counterparty: str = Form(""),
        signing_entity: str = Form(""),
        amount: str = Form(""),
        settlement_amount: str = Form(""),
        status: str = Form("draft"),
        signed_date: str = Form(""),
        payment_terms: str = Form(""),
        file_id: str = Form(""),
        note: str = Form(""),
        action: str = Form("save"),
    ):
        project, contract = owned_contract(request, project_id, contract_id)
        user = current_user(request)
        if action == "delete":
            if not contracts.delete(project.owner, project_id, contract_id):
                return HTMLResponse(
                    ui.contract_detail_page(
                        _user_payload(user),
                        project,
                        contract,
                        error="只能删除草稿状态的合同。",
                    ),
                    status_code=400,
                )
            log_action(
                request,
                "contract_deleted",
                project_id=project.id,
                detail={"contractId": contract_id},
            )
            return RedirectResponse(
                f"/projects/{project.id}/contracts", status_code=303
            )
        if file_id and projects.find_file(
            project.owner, project_id, file_id
        ) is None:
            return HTMLResponse(
                ui.contract_detail_page(
                    _user_payload(user),
                    project,
                    contract,
                    error="所选合同原件不存在。",
                ),
                status_code=400,
            )
        try:
            updated = contracts.update(
                project.owner,
                project_id,
                contract_id,
                title=title,
                contract_no=contract_no,
                counterparty=counterparty,
                signing_entity=signing_entity,
                amount=amount,
                settlement_amount=settlement_amount,
                status=status,
                signed_date=signed_date,
                payment_terms=payment_terms,
                file_id=file_id or None,
                note=note,
            )
        except ValueError as exc:
            return HTMLResponse(
                ui.contract_detail_page(
                    _user_payload(user),
                    project,
                    contract,
                    error=str(exc),
                ),
                status_code=400,
            )
        log_action(
            request,
            "contract_updated",
            project_id=project.id,
            detail={"contractId": contract_id, "status": status},
        )
        return RedirectResponse(
            f"/projects/{project.id}/contracts/{contract_id}",
            status_code=303,
        )

    @app.get("/customers", response_class=HTMLResponse)
    def customers_index(request: Request) -> str:
        user = current_user(request)
        return ui.customers_page(
            _user_payload(user),
            customers.list_for(user.username),
            projects.list_for(user.username),
        )

    @app.post("/customers", response_class=HTMLResponse)
    def customers_create(
        request: Request,
        name: str = Form(""),
        short_name: str = Form(""),
        tax_id: str = Form(""),
        address: str = Form(""),
        note: str = Form(""),
    ):
        user = current_user(request)
        values = {
            "name": name,
            "short_name": short_name,
            "tax_id": tax_id,
            "address": address,
            "note": note,
        }
        try:
            customer = customers.create(
                user.username,
                name=name,
                short_name=short_name,
                tax_id=tax_id,
                address=address,
                note=note,
            )
        except ValueError as exc:
            return HTMLResponse(
                ui.customers_page(
                    _user_payload(user),
                    customers.list_for(user.username),
                    projects.list_for(user.username),
                    error=str(exc),
                    values=values,
                ),
                status_code=400,
            )
        log_action(
            request,
            "customer_created",
            detail={"customerId": customer["id"], "name": customer["name"]},
        )
        return RedirectResponse(
            f"/customers/{customer['id']}", status_code=303
        )

    @app.get("/customers/{customer_id}", response_class=HTMLResponse)
    def customer_detail(request: Request, customer_id: str) -> str:
        customer = owned_customer(request, customer_id)
        user = current_user(request)
        linked_projects = tuple(
            project
            for project in projects.list_for(user.username)
            if project.customer_id == customer_id
        )
        return HTMLResponse(
            ui.customer_page(
                _user_payload(user), customer, linked_projects
            )
        )

    @app.post("/customers/{customer_id}/contacts")
    def customer_contact_create(
        request: Request,
        customer_id: str,
        name: str = Form(""),
        title: str = Form(""),
        phone: str = Form(""),
        wechat: str = Form(""),
        note: str = Form(""),
        is_primary: str | None = Form(None),
    ):
        customer = owned_customer(request, customer_id)
        user = current_user(request)
        try:
            contact = customers.add_contact(
                user.username,
                customer_id,
                name=name,
                title=title,
                phone=phone,
                wechat=wechat,
                note=note,
                is_primary=bool(is_primary),
            )
        except ValueError as exc:
            linked_projects = tuple(
                project
                for project in projects.list_for(user.username)
                if project.customer_id == customer_id
            )
            return HTMLResponse(
                ui.customer_page(
                    _user_payload(user),
                    customer,
                    linked_projects,
                    error=str(exc),
                ),
                status_code=400,
            )
        log_action(
            request,
            "customer_contact_added",
            detail={
                "customerId": customer_id,
                "contactId": contact["id"] if contact else None,
            },
        )
        return RedirectResponse(
            f"/customers/{customer_id}", status_code=303
        )

    @app.get("/projects/new", response_class=HTMLResponse)
    def new_project_get(request: Request) -> str:
        user = current_user(request)
        return ui.new_project_page(
            _user_payload(user),
            customers=customers.list_for(user.username),
        )

    @app.post("/projects", response_class=HTMLResponse)
    def new_project_post(
        request: Request,
        name: str = Form(""),
        customer: str = Form(""),
        customer_id: str = Form(""),
        note: str = Form(""),
    ):
        user = current_user(request)
        values = {
            "name": name,
            "customer": customer,
            "customer_id": customer_id,
            "note": note,
        }
        linked_customer = None
        if customer_id:
            if not CUSTOMER_ID_RE.fullmatch(customer_id):
                linked_customer = None
            else:
                linked_customer = customers.get(user.username, customer_id)
            if linked_customer is None:
                return HTMLResponse(
                    ui.new_project_page(
                        _user_payload(user),
                        customers=customers.list_for(user.username),
                        error="所选客户不存在。",
                        values=values,
                    ),
                    status_code=400,
                )
            customer = linked_customer["name"]
        try:
            project = projects.create(
                user.username,
                name,
                customer,
                note,
                customer_id=customer_id or None,
            )
        except ValueError as exc:
            return HTMLResponse(
                ui.new_project_page(
                    _user_payload(user),
                    customers=customers.list_for(user.username),
                    error=str(exc),
                    values=values,
                ),
                status_code=400,
            )
        try:
            template_preferences.ensure_project(
                owner=project.owner,
                project_id=project.id,
                projects=projects,
                new_quotes=new_quotes,
            )
        except (OSError, ValueError):
            logger.exception("新项目自动准备账号默认模板失败")
        log_action(
            request,
            "project_created",
            project_id=project.id,
            detail={
                "name": project.name,
                "customer": project.customer,
                "customerId": project.customer_id,
            },
        )
        return RedirectResponse(
            f"/projects/{project.id}", status_code=303
        )

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    def project_detail(request: Request, project_id: str) -> str:
        project = owned_project(request, project_id)
        return ui.project_page(
            _user_payload(current_user(request)),
            project,
            members=projects.list_members(project_id),
        )

    @app.post("/projects/{project_id}/members")
    def project_members_add(
        request: Request,
        project_id: str,
        username: str = Form(""),
        role: str = Form("editor"),
    ):
        project = owned_project(request, project_id)
        user = current_user(request)
        if user.username != project.owner:
            return HTMLResponse(
                ui.error_page(
                    "只有项目所有者或管理员可以管理项目成员。",
                    user=_user_payload(user),
                    title="没有权限",
                    back_href=f"/projects/{project.id}",
                    back_label="返回项目页",
                ),
                status_code=403,
            )
        username = username.strip()
        target = auth.get(username)
        if target is None:
            return HTMLResponse(
                ui.error_page(
                    f"账号 {username or '（空）'} 不存在；请先在账号管理中创建。",
                    user=_user_payload(user),
                    title="成员添加失败",
                    back_href=f"/projects/{project.id}",
                    back_label="返回项目页",
                ),
                status_code=400,
            )
        try:
            projects.add_member(project_id, username, role, user.username)
        except ValueError as exc:
            return HTMLResponse(
                ui.error_page(
                    str(exc),
                    user=_user_payload(user),
                    title="成员添加失败",
                    back_href=f"/projects/{project.id}",
                    back_label="返回项目页",
                ),
                status_code=400,
            )
        log_action(
            request,
            "project_member_added",
            project_id=project.id,
            detail={"username": username, "role": role},
        )
        return RedirectResponse(
            f"/projects/{project.id}?notice=member-added", status_code=303
        )

    @app.post("/projects/{project_id}/members/{username}/delete")
    def project_members_remove(
        request: Request,
        project_id: str,
        username: str,
    ):
        project = owned_project(request, project_id)
        user = current_user(request)
        if user.username != project.owner:
            return HTMLResponse(
                ui.error_page(
                    "只有项目所有者或管理员可以管理项目成员。",
                    user=_user_payload(user),
                    title="没有权限",
                    back_href=f"/projects/{project.id}",
                    back_label="返回项目页",
                ),
                status_code=403,
            )
        if not projects.remove_member(project_id, username):
            raise HTTPException(
                status_code=404, detail="该账号不是项目成员"
            )
        log_action(
            request,
            "project_member_removed",
            project_id=project.id,
            detail={"username": username},
        )
        return RedirectResponse(
            f"/projects/{project.id}?notice=member-removed", status_code=303
        )

    @app.get("/projects/{project_id}/files", response_class=HTMLResponse)
    def project_files_get(
        request: Request,
        project_id: str,
        notice: str = "",
        uploaded: str = "",
        count: int = 0,
        duplicates: int = 0,
        failed: int = 0,
        kind: str = "",
    ) -> str:
        project = owned_project(request, project_id)
        notices = {
            "duplicate": "相同文件已在本项目中，未重复保存；可以直接使用已有资料。",
            "batch-started": "批量处理已开始：系统会在后台逐份检查图纸、导入价格；本页会自动刷新进度。",
            "batch-running": "已有批量任务正在执行，请等它完成后再发起新的批量处理。",
            "batch-empty": "没有需要处理的资料：图纸都已检查、模板都已导入。",
        }
        if notice == "uploaded":
            if uploaded == "template":
                notices["uploaded"] = (
                    "模板已上传。下一步：在下方模板行点“导入价格”，"
                    "再检查图纸；两步都完成后才能创建草稿。"
                )
            elif uploaded == "drawing":
                notices["uploaded"] = (
                    "图纸已上传。下一步：在下方图纸行点“检查图纸”，"
                    "并确认模板价格已经导入。"
                )
            else:
                notices["uploaded"] = "资料已上传，可以继续下一步。"
        elif notice == "batch":
            label = {
                "drawing": "图纸",
                "template": "模板",
                "reference": "参考资料",
                "auto": "资料",
            }.get(kind, "资料")
            message = f"本批{label}上传完成：成功 {count} 个"
            if duplicates:
                message += f"，重复 {duplicates} 个已跳过"
            if failed:
                message += f"，失败 {failed} 个（请检查格式或大小）"
            message += (
                "。请在下方文件行逐份点“检查图纸”或“导入价格”；"
                "多张图纸检查完成后，可在创建草稿时勾选合并。"
            )
            notices["batch"] = message
        return HTMLResponse(
            ui.project_files_page(
                _user_payload(current_user(request)),
                project,
                values={"kind": kind or uploaded or "drawing"},
                notice=notices.get(notice, ""),
                inspections=new_quotes.list_inspections(project_id),
                price_books=new_quotes.list_price_books(project_id),
                batch_job=batch_jobs.get(project_id),
            )
        )

    @app.post("/projects/{project_id}/files", response_class=HTMLResponse)
    async def project_files_post(
        request: Request,
        project_id: str,
        kind: str = Form("drawing"),
        upload: list[UploadFile] = File(...),
    ):
        project = owned_project(request, project_id)
        user = current_user(request)
        values = {"kind": kind}

        def error_page(message: str, status_code: int = 400) -> HTMLResponse:
            return HTMLResponse(
                ui.project_files_page(
                    _user_payload(user),
                    project,
                    error=message,
                    values=values,
                    inspections=new_quotes.list_inspections(project_id),
                    price_books=new_quotes.list_price_books(project_id),
                ),
                status_code=status_code,
            )

        if kind not in UPLOAD_KINDS and kind != "auto":
            for item in upload:
                await item.close()
            return error_page("资料类型无效。")
        selected = [item for item in upload if item.filename]
        if not selected:
            return error_page("请选择要上传的文件。")

        files_dir = _project_files_dir(data_root, project_id)
        files_dir.mkdir(parents=True, exist_ok=True)
        existing_by_key = {
            (item.kind, item.sha256): item for item in project.files
        }
        uploaded = 0
        duplicates = 0
        errors: list[str] = []

        for item in selected:
            original = _clean_upload_name(item.filename)
            suffix = Path(original).suffix.lower()
            if kind == "auto":
                actual_kind = _infer_upload_kind(suffix)
                if not actual_kind:
                    errors.append(f"{original or '未命名文件'}：无法按扩展名识别类型")
                    await item.close()
                    continue
            else:
                actual_kind = kind
            if not original or not suffix:
                errors.append("有一个文件没有文件名，已跳过")
                await item.close()
                continue
            if suffix not in UPLOAD_KINDS[actual_kind]:
                allowed = "、".join(sorted(UPLOAD_KINDS[actual_kind]))
                errors.append(
                    f"{original}：{UPLOAD_KIND_LABELS[actual_kind]}支持 {allowed}"
                )
                await item.close()
                continue

            stored_name = f"file-{uuid4().hex}{suffix}"
            temporary = files_dir / f".{stored_name}.upload"
            target = files_dir / stored_name
            digest = hashlib.sha256()
            size = 0
            try:
                with temporary.open("wb") as handle:
                    while True:
                        chunk = await item.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_UPLOAD_BYTES:
                            raise ValueError("单个文件不能超过 50 MB")
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                if size == 0:
                    raise ValueError("文件为空，请重新选择。")
                try:
                    info = validate_upload(
                        temporary,
                        filename=original,
                        declared_kind=kind,
                        size=size,
                    )
                except UploadValidationError as exc:
                    raise ValueError(
                        f"{exc}（{exc.code}）"
                    ) from exc
                actual_kind = info["businessKind"]
                digest_hex = digest.hexdigest()
                existing = existing_by_key.get((actual_kind, digest_hex))
                if existing is not None:
                    temporary.unlink(missing_ok=True)
                    duplicates += 1
                    log_action(
                        request,
                        "file_duplicate",
                        project_id=project.id,
                        detail={
                            "fileId": existing.id,
                            "name": original,
                            "kind": actual_kind,
                            "sha256": digest_hex,
                        },
                    )
                    continue
                os.replace(temporary, target)
                saved = projects.add_file(
                    project.owner,
                    project.id,
                    original_name=original,
                    stored_name=stored_name,
                    kind=actual_kind,
                    size=size,
                    sha256=digest_hex,
                    content_format=info["contentFormat"],
                    format_version=str(info["formatVersion"]),
                )
                existing_by_key[(actual_kind, digest_hex)] = saved
                uploaded += 1
                log_action(
                    request,
                    "file_uploaded",
                    project_id=project.id,
                    detail={
                        "fileId": saved.id,
                        "name": saved.original_name,
                        "kind": saved.kind,
                        "size": saved.size,
                        "sha256": saved.sha256,
                    },
                )
            except ValueError as exc:
                temporary.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                errors.append(f"{original}：{exc}")
            except Exception:  # noqa: BLE001
                logger.exception("项目资料保存失败")
                temporary.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                errors.append(f"{original}：保存失败")
            finally:
                await item.close()

        if uploaded == 0 and duplicates == 0:
            detail = "；".join(errors[:3]) or "没有可上传的文件。"
            if len(errors) > 3:
                detail += f"；另有 {len(errors) - 3} 个文件失败"
            return error_page(detail)
        if len(selected) == 1 and uploaded == 1 and duplicates == 0 and not errors:
            return RedirectResponse(
                f"/projects/{project.id}/files?notice=uploaded&uploaded={actual_kind}",
                status_code=303,
            )
        if len(selected) == 1 and uploaded == 0 and duplicates == 1 and not errors:
            return RedirectResponse(
                f"/projects/{project.id}/files?notice=duplicate",
                status_code=303,
            )
        query = urlencode(
            {
                "notice": "batch",
                "count": uploaded,
                "duplicates": duplicates,
                "failed": len(errors),
                "kind": kind,
            }
        )
        return RedirectResponse(
            f"/projects/{project.id}/files?{query}",
            status_code=303,
        )

    @app.get("/projects/{project_id}/files/{file_id}")
    def project_file_download(
        request: Request, project_id: str, file_id: str
    ) -> FileResponse:
        project = owned_project(request, project_id)
        found = projects.find_file(project.owner, project_id, file_id)
        if found is None:
            raise HTTPException(status_code=404, detail="资料不存在")
        project, item = found
        files_dir = _project_files_dir(data_root, project_id)
        path = (files_dir / item.stored_name).resolve()
        if not path.is_relative_to(files_dir) or not path.is_file():
            raise HTTPException(status_code=404, detail="资料不存在")
        log_action(
            request,
            "download",
            project_id=project.id,
            detail={"fileId": item.id, "name": item.original_name, "kind": "project_file"},
        )
        return FileResponse(path, filename=item.original_name)

    @app.post("/projects/{project_id}/files/{file_id}/delete")
    def project_file_delete(
        request: Request, project_id: str, file_id: str
    ):
        project = owned_project(request, project_id)
        found = projects.find_file(project.owner, project_id, file_id)
        if found is None:
            raise HTTPException(status_code=404, detail="资料不存在")
        project, item = found
        try:
            projects.delete_file(project.owner, project.id, file_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        files_dir = _project_files_dir(data_root, project_id)
        path = (files_dir / item.stored_name).resolve()
        if path.is_relative_to(files_dir):
            path.unlink(missing_ok=True)
        log_action(
            request,
            "file_deleted",
            project_id=project.id,
            detail={"fileId": item.id, "name": item.original_name},
        )
        return RedirectResponse(
            f"/projects/{project.id}/files", status_code=303
        )

    @app.post("/projects/{project_id}/files/{file_id}/preflight")
    def preflight_project_file(
        request: Request, project_id: str, file_id: str
    ):
        project = owned_project(request, project_id)
        found = projects.find_file(
            project.owner, project_id, file_id
        )
        if found is None:
            raise HTTPException(status_code=404, detail="资料不存在")
        _, item = found
        if item.kind != "drawing":
            raise HTTPException(
                status_code=400, detail="只有设计图纸可以预检"
            )
        path = _project_file_path(data_root, project_id, item.stored_name)
        workdir = (
            data_root
            / "projects"
            / project_id
            / "preflight-work"
            / uuid4().hex
        )
        try:
            report = read_preflight(path, item.id, workdir)
        except CadReadError as exc:
            diagnostic_id = uuid4().hex[:8]
            return HTMLResponse(
                ui.project_files_page(
                    _user_payload(current_user(request)),
                    project,
                    error=(
                        f"{_cad_error_message(exc.code)}"
                        f"（诊断编号 {diagnostic_id}）"
                    ),
                    inspections=new_quotes.list_inspections(project_id),
                    price_books=new_quotes.list_price_books(project_id),
                ),
                status_code=400,
            )
        log_action(
            request,
            "drawing_preflight",
            project_id=project.id,
            detail={
                "fileId": item.id,
                "totalEstimated": report.get("totalEstimated"),
                "totalEffective": report.get("totalEffective"),
            },
        )
        return HTMLResponse(
            ui.preflight_page(
                _user_payload(current_user(request)),
                project,
                item,
                report,
            )
        )

    @app.post("/projects/{project_id}/files/{file_id}/inspect")
    def inspect_project_file(
        request: Request,
        project_id: str,
        file_id: str,
        force: str = Form(""),
        views: list[str] = Form(default=[]),
    ):
        project = owned_project(request, project_id)
        found = projects.find_file(
            project.owner, project_id, file_id
        )
        if found is None:
            raise HTTPException(status_code=404, detail="资料不存在")
        _, item = found
        if item.kind != "drawing":
            return HTMLResponse(
                ui.project_files_page(
                    _user_payload(current_user(request)),
                    project,
                    error="只有设计图纸可以检查。",
                    inspections=new_quotes.list_inspections(project_id),
                    price_books=new_quotes.list_price_books(project_id),
                ),
                status_code=400,
            )
        existing = new_quotes.latest_inspection_for_file(
            project.id, item.id
        )
        if existing is not None and force != "1":
            engine = str(
                (existing.get("report") or {}).get("engineVersion") or ""
            )
            if engine == INSPECTION_ENGINE_VERSION:
                return RedirectResponse(
                    f"/projects/{project.id}/inspections/{existing['id']}"
                    "?notice=already-inspected",
                    status_code=303,
                )
            # 旧引擎结果可查看，但不静默当作本次检查；继续建立新记录。
        path = _project_file_path(data_root, project_id, item.stored_name)
        workdir = (
            data_root
            / "projects"
            / project_id
            / "inspection-work"
            / uuid4().hex
        )
        try:
            report = inspect_drawing(
                path,
                item.id,
                workdir,
                scope={"views": views} if views else None,
            )
            inspection = new_quotes.save_inspection(
                project.id, item.id, report
            )
        except CadReadError as exc:
            logger.exception("图纸检查失败")
            diagnostic_id = uuid4().hex[:8]
            failure_dir = (
                data_root / "projects" / project.id / "inspection-failures"
            )
            failure_dir.mkdir(parents=True, exist_ok=True)
            failure_path = failure_dir / f"{diagnostic_id}.json"
            failure_path.write_text(
                json.dumps(
                    {
                        "diagnosticId": diagnostic_id,
                        "fileId": item.id,
                        "fileName": item.original_name,
                        "sha256": item.sha256,
                        "code": exc.code,
                        "message": str(exc),
                        "detail": exc.detail,
                        "engineVersion": INSPECTION_ENGINE_VERSION,
                        "createdAt": _now_text(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            message = (
                f"{_cad_error_message(exc.code)}（诊断编号 {diagnostic_id}）"
            )
            return HTMLResponse(
                ui.project_files_page(
                    _user_payload(current_user(request)),
                    project,
                    error=message,
                    inspections=new_quotes.list_inspections(project_id),
                    price_books=new_quotes.list_price_books(project_id),
                ),
                status_code=400,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("图纸检查失败")
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else "图纸检查没有完成；请确认文件可读，或查看服务器日志。"
            )
            return HTMLResponse(
                ui.project_files_page(
                    _user_payload(current_user(request)),
                    project,
                    error=message,
                    inspections=new_quotes.list_inspections(project_id),
                    price_books=new_quotes.list_price_books(project_id),
                ),
                status_code=400,
            )
        log_action(
            request,
            "drawing_inspected",
            project_id=project.id,
            detail={
                "fileId": item.id,
                "inspectionId": inspection["id"],
                "equipmentGroups": report.get("counts", {}).get("equipmentGroups", 0),
                "routeGroups": report.get("counts", {}).get("routeGroups", 0),
            },
        )
        return RedirectResponse(
            f"/projects/{project.id}/inspections/{inspection['id']}",
            status_code=303,
        )

    @app.get(
        "/projects/{project_id}/files/{file_id}/template-map",
        response_class=HTMLResponse,
    )
    def template_mapping_get(
        request: Request, project_id: str, file_id: str
    ) -> str:
        project = owned_project(request, project_id)
        found = projects.find_file(
            project.owner, project_id, file_id
        )
        if found is None:
            raise HTTPException(status_code=404, detail="资料不存在")
        _, item = found
        if item.kind != "template":
            raise HTTPException(status_code=400, detail="只有报价模板可以导入价格")
        path = _project_file_path(data_root, project_id, item.stored_name)
        try:
            tables = inspect_workbook_tables(path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("读取模板工作表失败")
            return HTMLResponse(
                ui.project_files_page(
                    _user_payload(current_user(request)),
                    project,
                    error=f"读取模板工作表失败：{exc}",
                    inspections=new_quotes.list_inspections(project_id),
                    price_books=new_quotes.list_price_books(project_id),
                ),
                status_code=400,
            )
        return HTMLResponse(
            ui.template_mapping_page(
                _user_payload(current_user(request)),
                project,
                item,
                tables,
                profiles=mapping_profiles.list_for(
                    current_user(request).username, customer_key(project)
                ),
            )
        )

    @app.get(
        "/projects/{project_id}/template-mappings",
        response_class=HTMLResponse,
    )
    def template_mappings(request: Request, project_id: str, notice: str = "") -> str:
        project = owned_project(request, project_id)
        profiles = mapping_profiles.list_for(
            current_user(request).username, customer_key(project)
        )
        messages = {"deleted": "客户列映射已删除；下次导入会回退自动识别或需要重新手工选列。"}
        return HTMLResponse(
            ui.template_mapping_profiles_page(
                _user_payload(current_user(request)),
                project,
                profiles,
                notice=messages.get(notice, ""),
            )
        )

    @app.post("/projects/{project_id}/template-mappings/{profile_id}/delete")
    def template_mapping_delete(
        request: Request, project_id: str, profile_id: str
    ):
        project = owned_project(request, project_id)
        if not mapping_profiles.delete(
            current_user(request).username, customer_key(project), profile_id
        ):
            raise HTTPException(status_code=404, detail="客户列映射不存在")
        log_action(
            request,
            "template_mapping_deleted",
            project_id=project.id,
            detail={"profileId": profile_id},
        )
        return RedirectResponse(
            f"/projects/{project.id}/template-mappings?notice=deleted",
            status_code=303,
        )

    @app.post("/projects/{project_id}/files/{file_id}/import-template")
    def import_project_template(
        request: Request,
        project_id: str,
        file_id: str,
        sheet: str = Form(""),
        header_row: int = Form(0),
        name_col: str = Form(""),
        spec_col: str = Form(""),
        unit_col: str = Form(""),
        quantity_col: str = Form(""),
        material_col: str = Form(""),
        install_col: str = Form(""),
    ):
        project = owned_project(request, project_id)
        found = projects.find_file(
            project.owner, project_id, file_id
        )
        if found is None:
            raise HTTPException(status_code=404, detail="资料不存在")
        _, item = found
        if item.kind != "template":
            return HTMLResponse(
                ui.project_files_page(
                    _user_payload(current_user(request)),
                    project,
                    error="只有报价模板可以导入价格。",
                    inspections=new_quotes.list_inspections(project_id),
                    price_books=new_quotes.list_price_books(project_id),
                ),
                status_code=400,
            )
        existing_book = new_quotes.price_book_for_file(project.id, item.id)
        if existing_book is not None:
            return RedirectResponse(
                f"/projects/{project.id}/price-books/{existing_book['id']}"
                "?notice=already-imported",
                status_code=303,
            )
        path = _project_file_path(data_root, project_id, item.stored_name)
        profile_key = customer_key(project)
        try:
            if sheet:
                book = import_price_book_mapped(
                    path,
                    file_id=item.id,
                    source_sha256=item.sha256,
                    sheet_name=sheet,
                    header_row=header_row,
                    columns={
                        "name": name_col,
                        "spec": spec_col,
                        "unit": unit_col,
                        "quantity": quantity_col,
                        "material": material_col,
                        "installation": install_col,
                    },
                )
            else:
                saved_profiles = mapping_profiles.list_for(
                    current_user(request).username, profile_key
                )
                matched = find_matching_mapping(path, saved_profiles)
                book = None
                if matched is not None:
                    try:
                        book = import_price_book_mapped(
                            path,
                            file_id=item.id,
                            source_sha256=item.sha256,
                            sheet_name=matched["sheetName"],
                            header_row=matched["headerRow"],
                            columns=matched["columns"],
                        )
                        book["detectedBy"] = "profile"
                        book["mappingProfileId"] = matched["profileId"]
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "保存的模板列映射不再适用，回退自动识别",
                            exc_info=True,
                        )
                        book = None
                if book is None:
                    book = import_price_book_auto(
                        path, file_id=item.id, source_sha256=item.sha256
                    )
            price_book = new_quotes.save_price_book(
                project.id, item.id, item.original_name, book
            )
            if current_user(request).username == project.owner:
                try:
                    template_preferences.remember_if_absent(
                        owner=project.owner,
                        project_id=project.id,
                        file_name=item.original_name,
                        file_bytes=path.read_bytes(),
                        file_sha256=item.sha256,
                        price_book_name=item.original_name,
                        book=book,
                    )
                except (OSError, ValueError):
                    logger.exception("记住账号默认模板失败")
            headers = book.get("headerLabels") or {}
            signature = book.get("mappingSignature")
            if sheet and headers and signature:
                try:
                    mapping_profiles.upsert(
                        owner=current_user(request).username,
                        key=profile_key,
                        signature=str(signature),
                        sheet_name=str(book.get("sheetName") or sheet),
                        header_row=int(book.get("headerRow") or header_row),
                        columns=book.get("columns") or {},
                        headers=headers,
                        file_name=item.original_name,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("保存模板列映射失败")
        except Exception as exc:  # noqa: BLE001
            logger.exception("模板价格导入失败")
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else "模板价格导入没有完成；请确认文件是 XLSX，并查看服务器日志。"
            )
            return HTMLResponse(
                ui.project_files_page(
                    _user_payload(current_user(request)),
                    project,
                    error=message,
                    inspections=new_quotes.list_inspections(project_id),
                    price_books=new_quotes.list_price_books(project_id),
                ),
                status_code=400,
            )
        log_action(
            request,
            "template_imported",
            project_id=project.id,
            detail={
                "fileId": item.id,
                "priceBookId": price_book["id"],
                "rows": book.get("counts", {}).get("rows", 0),
            },
        )
        return RedirectResponse(
            f"/projects/{project.id}/price-books/{price_book['id']}",
            status_code=303,
        )

    def _batch_pending(project) -> list:
        pending = []
        for item in project.files:
            if item.kind == "drawing" and (
                new_quotes.latest_inspection_for_file(project.id, item.id) is None
            ):
                pending.append(item)
            elif item.kind == "template" and (
                new_quotes.price_book_for_file(project.id, item.id) is None
            ):
                pending.append(item)
        return pending

    def _batch_process_one(project, item, owner: str) -> tuple[str, str]:
        """串行处理单个待检查图纸/待导入模板；返回 (state, message)。"""
        path = _project_file_path(data_root, project.id, item.stored_name)
        if item.kind == "drawing":
            workdir = (
                data_root
                / "projects"
                / project.id
                / "inspection-work"
                / uuid4().hex
            )
            try:
                report = inspect_drawing(path, item.id, workdir)
            except CadReadError as exc:
                logger.exception("批量图纸检查失败: %s", item.original_name)
                return ("error", _cad_error_message(exc.code))
            except Exception as exc:  # noqa: BLE001
                logger.exception("批量图纸检查失败: %s", item.original_name)
                return (
                    "error",
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "图纸检查没有完成；请确认文件可读。",
                )
            inspection = new_quotes.save_inspection(project.id, item.id, report)
            audit.log(
                "drawing_inspected",
                username=owner,
                project_id=project.id,
                detail={
                    "fileId": item.id,
                    "inspectionId": inspection["id"],
                    "equipmentGroups": report.get("counts", {}).get(
                        "equipmentGroups", 0
                    ),
                    "routeGroups": report.get("counts", {}).get("routeGroups", 0),
                },
            )
            return "inspected", ""
        try:
            saved = mapping_profiles.list_for(owner, customer_key(project))
            matched = find_matching_mapping(path, saved)
            book = None
            if matched is not None:
                try:
                    book = import_price_book_mapped(
                        path,
                        file_id=item.id,
                        source_sha256=item.sha256,
                        sheet_name=matched["sheetName"],
                        header_row=matched["headerRow"],
                        columns=matched["columns"],
                    )
                    book["detectedBy"] = "profile"
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "批量导入：保存的列映射不适用，回退自动识别",
                        exc_info=True,
                    )
                    book = None
            if book is None:
                book = import_price_book_auto(
                    path, file_id=item.id, source_sha256=item.sha256
                )
            price_book = new_quotes.save_price_book(
                project.id, item.id, item.original_name, book
            )
            if owner == project.owner:
                try:
                    template_preferences.remember_if_absent(
                        owner=project.owner,
                        project_id=project.id,
                        file_name=item.original_name,
                        file_bytes=path.read_bytes(),
                        file_sha256=item.sha256,
                        price_book_name=item.original_name,
                        book=book,
                    )
                except (OSError, ValueError):
                    logger.exception("批量导入后记住账号默认模板失败")
        except Exception as exc:  # noqa: BLE001
            logger.exception("批量模板导入失败: %s", item.original_name)
            return (
                "error",
                str(exc)
                if isinstance(exc, ValueError)
                else "模板价格导入没有完成；请确认文件是 XLSX。",
            )
        audit.log(
            "template_imported",
            username=owner,
            project_id=project.id,
            detail={
                "fileId": item.id,
                "priceBookId": price_book["id"],
                "rows": book.get("counts", {}).get("rows", 0),
            },
        )
        return "imported", ""

    @app.post("/projects/{project_id}/files/batch-process")
    def files_batch_process(request: Request, project_id: str):
        project = owned_project(request, project_id)
        with batch_lock:
            job = batch_jobs.get(project_id)
            if job is not None and job.get("status") == "running":
                return RedirectResponse(
                    f"/projects/{project.id}/files?notice=batch-running",
                    status_code=303,
                )
        pending = _batch_pending(project)
        if not pending:
            return RedirectResponse(
                f"/projects/{project.id}/files?notice=batch-empty",
                status_code=303,
            )
        with batch_lock:
            job = batch_jobs.get(project_id)
            if job is not None and job.get("status") == "running":
                return RedirectResponse(
                    f"/projects/{project.id}/files?notice=batch-running",
                    status_code=303,
                )
            job = {
                "status": "running",
                "total": len(pending),
                "done": 0,
                "current": "",
                "startedAt": datetime.now()
                .astimezone()
                .isoformat(timespec="seconds"),
                "finishedAt": "",
                "results": [],
            }
            batch_jobs[project_id] = job
        owner = current_user(request).username

        def run_batch() -> None:
            try:
                for item in pending:
                    job["current"] = item.original_name
                    state, message = _batch_process_one(project, item, owner)
                    job["results"].append(
                        {
                            "fileName": item.original_name,
                            "kind": item.kind,
                            "state": state,
                            "message": message,
                        }
                    )
                    job["done"] += 1
            finally:
                job["status"] = "done"
                job["current"] = ""
                job["finishedAt"] = (
                    datetime.now().astimezone().isoformat(timespec="seconds")
                )
                audit.log(
                    "batch_run",
                    username=owner,
                    project_id=project.id,
                    detail={
                        "total": job["total"],
                        "done": job["done"],
                        "ok": sum(
                            1
                            for entry in job["results"]
                            if entry["state"] != "error"
                        ),
                        "failed": sum(
                            1
                            for entry in job["results"]
                            if entry["state"] == "error"
                        ),
                    },
                )

        threading.Thread(target=run_batch, daemon=True).start()
        return RedirectResponse(
            f"/projects/{project.id}/files?notice=batch-started",
            status_code=303,
        )

    @app.get(
        "/projects/{project_id}/inspections/{inspection_id}",
        response_class=HTMLResponse,
    )
    def inspection_detail(
        request: Request,
        project_id: str,
        inspection_id: str,
        notice: str = "",
    ) -> str:
        project = owned_project(request, project_id)
        inspection = new_quotes.get_inspection(project_id, inspection_id)
        if inspection is None:
            raise HTTPException(status_code=404, detail="图纸检查不存在")
        drafts = [
            item
            for item in new_quotes.list_drafts(project_id)
            if item.get("inspectionId") == inspection_id
            or inspection_id in (item.get("data") or {}).get("inspectionIds", [])
        ]
        config = VisionConfig.from_env()
        return HTMLResponse(
            ui.inspection_page(
                _user_payload(current_user(request)),
                project,
                inspection,
                new_quotes.list_price_books(project_id),
                notice=notice,
                vision_enabled=bool(
                    get_preferences(db, project.owner).get("visionEnabled")
                ),
                vision_config=config.safe_identity(),
                vision_candidates=candidate_snapshot(inspection),
                vision_drafts=drafts,
            )
        )

    @app.post(
        "/projects/{project_id}/inspections/{inspection_id}/vision-preview",
        response_class=HTMLResponse,
    )
    def vision_preview(
        request: Request,
        project_id: str,
        inspection_id: str,
        candidate_ids: list[str] = Form(default=[]),
        draft_id: str = Form(""),
    ):
        project = owned_project(request, project_id)
        if not get_preferences(db, project.owner).get("visionEnabled"):
            raise HTTPException(
                status_code=403,
                detail="视觉辅助未开启；请在账号设置或报价工作台开启后再使用。",
            )
        inspection = new_quotes.get_inspection(project_id, inspection_id)
        if inspection is None:
            raise HTTPException(status_code=404, detail="图纸检查不存在")
        draft = new_quotes.get_draft(project_id, draft_id) if draft_id else None
        if draft is None:
            raise HTTPException(status_code=400, detail="请先创建并选择目标草稿")
        if str(draft.get("inspectionId")) != inspection_id and inspection_id not in (
            draft.get("data") or {}
        ).get("inspectionIds", []):
            raise HTTPException(status_code=403, detail="草稿不属于这次图纸检查")
        found = projects.find_file(
            project.owner, project_id, inspection["fileId"]
        )
        if found is None:
            raise HTTPException(status_code=404, detail="原图不存在")
        drawing_path = _project_file_path(
            data_root, project_id, found[1].stored_name
        )
        config = VisionConfig.from_env()
        try:
            preview = vision_service.create_preview(
                owner=current_user(request).username,
                project_id=project_id,
                inspection=inspection,
                draft_id=draft_id,
                candidate_ids=[item for item in candidate_ids if item],
                drawing_path=drawing_path,
            )
        except VisionTaskConflict as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log_action(
            request,
            "vision_preview_created",
            project_id=project.id,
            detail={
                "inspectionId": inspection_id,
                "draftId": draft_id,
                "candidateIds": preview["candidateIds"],
                "cropId": preview["image"]["cropId"],
            },
        )
        return HTMLResponse(
            ui.vision_preview_page(
                _user_payload(current_user(request)),
                project,
                inspection,
                draft,
                preview,
                config.safe_identity(),
            )
        )

    @app.get(
        "/projects/{project_id}/vision-previews/{preview_id}/crop.png"
    )
    def vision_preview_image(
        request: Request, project_id: str, preview_id: str
    ):
        project = owned_project(request, project_id)
        preview = vision_service.get_preview(
            current_user(request).username, project_id, preview_id
        )
        if preview is None:
            raise HTTPException(status_code=404, detail="预览不存在")
        path = Path(preview["pngPath"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="预览图片不存在")
        return FileResponse(path, media_type="image/png")

    @app.get(
        "/projects/{project_id}/vision-tasks/{task_id}/crop.png"
    )
    def vision_task_image(
        request: Request, project_id: str, task_id: str
    ):
        project = owned_project(request, project_id)
        task = vision_service.get_task(
            current_user(request).username, project_id, task_id
        )
        if task is None:
            raise HTTPException(status_code=404, detail="视觉任务不存在")
        path = Path(task.crop_dir) / "crop.png"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="任务图片不存在")
        return FileResponse(path, media_type="image/png")

    @app.post("/projects/{project_id}/inspections/{inspection_id}/vision-tasks")
    async def vision_task_create(
        request: Request,
        project_id: str,
        inspection_id: str,
        preview_id: str = Form(""),
        draft_id: str = Form(""),
        idempotency_key: str = Form(""),
        opt_in: str = Form(""),
    ):
        project = owned_project(request, project_id)
        if not get_preferences(db, project.owner).get("visionEnabled"):
            raise HTTPException(status_code=403, detail="视觉辅助未开启")
        inspection = new_quotes.get_inspection(project_id, inspection_id)
        if inspection is None:
            raise HTTPException(status_code=404, detail="图纸检查不存在")
        payload: dict = {}
        if "application/json" in (
            request.headers.get("content-type") or ""
        ).lower():
            try:
                payload = await request.json()
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=400, detail="请求 JSON 无效"
                ) from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="请求 JSON 无效")
            preview_id = str(payload.get("previewId") or preview_id)
            draft_id = str(payload.get("draftId") or draft_id)
            idempotency_key = str(
                payload.get("idempotencyKey") or idempotency_key
            )
            opt_in = (
                "1"
                if payload.get("optIn") is True
                else str(payload.get("optIn") or opt_in)
            )
            expected_version = str(payload.get("inspectionVersion") or "")
            if expected_version and expected_version != inspection_version(
                inspection
            ):
                raise HTTPException(
                    status_code=409,
                    detail="检查版本已变化，请重新预览后再提交",
                )
        draft = new_quotes.get_draft(project_id, draft_id) if draft_id else None
        if draft is None:
            raise HTTPException(status_code=400, detail="请选择目标草稿")
        if not preview_id and payload.get("candidateIds"):
            found = projects.find_file(
                project.owner, project_id, inspection["fileId"]
            )
            if found is None:
                raise HTTPException(status_code=404, detail="原图不存在")
            try:
                preview_created = vision_service.create_preview(
                    owner=current_user(request).username,
                    project_id=project_id,
                    inspection=inspection,
                    draft_id=draft_id,
                    candidate_ids=[
                        str(value)
                        for value in payload.get("candidateIds") or []
                    ],
                    drawing_path=_project_file_path(
                        data_root, project_id, found[1].stored_name
                    ),
                )
            except VisionTaskConflict as exc:
                raise HTTPException(
                    status_code=400, detail=str(exc)
                ) from exc
            preview_id = str(preview_created["previewId"])
        preview = vision_service.get_preview(
            current_user(request).username, project_id, preview_id
        )
        if preview is None:
            raise HTTPException(status_code=404, detail="局部预览不存在或已过期")
        try:
            task = vision_service.create_task(
                owner=current_user(request).username,
                project_id=project_id,
                inspection=inspection,
                draft_id=draft_id,
                preview=preview,
                idempotency_key=idempotency_key,
                opt_in=str(opt_in).lower() in {"1", "true", "on", "yes"},
            )
        except VisionTaskConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if task.status == "queued":
            vision_executor.submit(run_task, vision_service, task.id)
        log_action(
            request,
            "vision_task_created",
            project_id=project.id,
            detail={
                "taskId": task.id,
                "inspectionId": inspection_id,
                "draftId": draft_id,
                "cropId": (task.images[0] if task.images else {}).get(
                    "cropId"
                ),
                "idempotencyKey": idempotency_key,
            },
        )
        if _wants_json(request) or payload:
            return JSONResponse(
                {"taskId": task.id, "status": task.status}, status_code=202
            )
        return RedirectResponse(
            f"/projects/{project.id}/vision-tasks/{task.id}",
            status_code=303,
        )

    @app.get(
        "/projects/{project_id}/vision-tasks/{task_id}",
        response_class=HTMLResponse,
    )
    def vision_task_get(
        request: Request,
        project_id: str,
        task_id: str,
        format: str = "",
    ):
        project = owned_project(request, project_id)
        task = vision_service.get_task(
            current_user(request).username, project_id, task_id
        )
        if task is None:
            raise HTTPException(status_code=404, detail="视觉任务不存在")
        payload = _vision_task_payload(task)
        if _wants_json(request) or format == "json":
            return JSONResponse(payload)
        draft = new_quotes.get_draft(project_id, task.draft_id)
        return HTMLResponse(
            ui.vision_task_page(
                _user_payload(current_user(request)),
                project,
                draft,
                payload,
                config=VisionConfig.from_env().safe_identity(),
            )
        )

    @app.post(
        "/projects/{project_id}/vision-tasks/{task_id}/decision",
    )
    async def vision_task_decision(
        request: Request,
        project_id: str,
        task_id: str,
        draft_id: str = Form(""),
        task_version: int = Form(0),
        suggestion_version: str = Form(""),
        expected_revision: int = Form(0),
    ):
        project = owned_project(request, project_id)
        task = vision_service.get_task(
            current_user(request).username, project_id, task_id
        )
        if task is None:
            raise HTTPException(status_code=404, detail="视觉任务不存在")
        payload: dict = {}
        if "application/json" in (
            request.headers.get("content-type") or ""
        ).lower():
            try:
                payload = await request.json()
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=400, detail="请求 JSON 无效"
                ) from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="请求 JSON 无效")
            draft_id = str(payload.get("draftId") or draft_id)
            task_version = int(payload.get("taskVersion") or 0)
            suggestion_version = str(
                payload.get("suggestionVersion") or suggestion_version
            )
            expected_revision = int(
                payload.get("expectedDraftRevision") or expected_revision
            )
        draft = new_quotes.get_draft(project_id, draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="目标草稿不存在")
        price_book = (
            new_quotes.get_price_book(project_id, draft["priceBookId"])
            if draft.get("priceBookId")
            else None
        )
        if price_book is None:
            raise HTTPException(status_code=409, detail="草稿缺少价格书")
        inspection = new_quotes.get_inspection(project_id, task.inspection_id)
        if inspection is None:
            raise HTTPException(status_code=404, detail="图纸检查不存在")
        if payload:
            decisions = payload.get("decisions")
            if not isinstance(decisions, list):
                raise HTTPException(
                    status_code=400, detail="decisions 必须是数组"
                )
        else:
            form = await request.form()
            items = suggestion_items(task.id, task.suggestion)
            decisions = []
            for item in items:
                item_id = item["suggestionItemId"]
                decision = str(
                    form.get(f"decision__{item_id}") or "pending"
                )
                human = {}
                for field in ("category", "medium", "role", "model", "spec"):
                    value = form.get(f"{field}__{item_id}")
                    if value not in (None, ""):
                        human[field] = str(value)
                decisions.append(
                    {
                        "suggestionItemId": item_id,
                        "decision": decision,
                        "humanEdits": human,
                    }
                )
        found = projects.find_file(
            project.owner, project_id, price_book["fileId"]
        )
        if found is None:
            raise HTTPException(status_code=404, detail="模板文件不存在")
        template_path = _project_file_path(
            data_root, project_id, found[1].stored_name
        )
        try:
            result = apply_decisions(
                vision_service,
                owner=current_user(request).username,
                project_id=project_id,
                project_name=project.name,
                task_id=task_id,
                draft=draft,
                inspection=inspection,
                price_book=price_book,
                template_path=template_path,
                task_version=int(task_version),
                suggestion_version=str(suggestion_version),
                expected_revision=int(expected_revision),
                decisions=decisions,
                actor=current_user(request).username,
            )
        except VisionTaskConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        log_action(
            request,
            "vision_adopted",
            project_id=project.id,
            detail={
                "taskId": task_id,
                "draftId": draft_id,
                "adoptionId": result["adoptionId"],
                "derivedHash": result["derivedHash"],
                "changed": len(result["changed"]),
            },
        )
        if payload:
            return JSONResponse(
                {
                    "adoptionId": result["adoptionId"],
                    "derivedHash": result["derivedHash"],
                    "draftRevision": result["draft"].get("revision"),
                    "requiresConfirmation": bool(
                        (result.get("preview") or {}).get(
                            "requiresConfirmation"
                        )
                    ),
                    "preview": result.get("preview") or {},
                    "changed": result.get("changed") or [],
                }
            )
        return RedirectResponse(
            f"/projects/{project.id}/drafts/{draft_id}?vision=applied",
            status_code=303,
        )

    @app.post("/projects/{project_id}/vision-tasks/{task_id}/cancel")
    def vision_task_cancel(
        request: Request, project_id: str, task_id: str
    ):
        project = owned_project(request, project_id)
        task = vision_service.cancel_task(
            current_user(request).username, project_id, task_id
        )
        if task is None:
            raise HTTPException(status_code=404, detail="视觉任务不存在")
        log_action(
            request,
            "vision_task_cancelled",
            project_id=project.id,
            detail={"taskId": task_id, "status": task.status},
        )
        return RedirectResponse(
            f"/projects/{project.id}/vision-tasks/{task_id}",
            status_code=303,
        )

    @app.post("/projects/{project_id}/vision-tasks/{task_id}/retry")
    def vision_task_retry(
        request: Request, project_id: str, task_id: str
    ):
        project = owned_project(request, project_id)
        if not get_preferences(db, project.owner).get("visionEnabled"):
            raise HTTPException(
                status_code=403,
                detail="视觉辅助已关闭；请先开启后再主动重试。",
            )
        try:
            task = vision_service.retry_task(
                current_user(request).username, project_id, task_id
            )
        except VisionTaskConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if task is None:
            raise HTTPException(status_code=404, detail="视觉任务不存在")
        if task.status == "queued":
            vision_executor.submit(run_task, vision_service, task.id)
        log_action(
            request,
            "vision_task_retried",
            project_id=project.id,
            detail={"taskId": task_id},
        )
        return RedirectResponse(
            f"/projects/{project.id}/vision-tasks/{task_id}",
            status_code=303,
        )

    @app.post(
        "/projects/{project_id}/drafts/{draft_id}/vision-adoptions/{adoption_id}/revoke"
    )
    def vision_adoption_revoke(
        request: Request,
        project_id: str,
        draft_id: str,
        adoption_id: str,
        expected_revision: int = Form(0),
    ):
        project = owned_project(request, project_id)
        adoption = new_quotes.get_vision_adoption(
            project_id, draft_id, adoption_id
        )
        if adoption is None:
            raise HTTPException(status_code=404, detail="采纳记录不存在")
        if adoption["status"] != "active":
            raise HTTPException(status_code=409, detail="这次采纳已经撤销")
        draft = new_quotes.get_draft(project_id, draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        current_revision = int(draft.get("revision") or 1)
        if int(expected_revision) != current_revision:
            raise HTTPException(
                status_code=409,
                detail="草稿版本已变化，请刷新后重试。",
            )
        price_book = (
            new_quotes.get_price_book(project_id, draft["priceBookId"])
            if draft and draft.get("priceBookId")
            else None
        )
        if draft is None or price_book is None:
            raise HTTPException(status_code=409, detail="草稿或价格书不存在")
        restored = copy.deepcopy(adoption["beforeData"])
        restored.setdefault("vision", {})
        restored["vision"] = {
            **dict(restored.get("vision") or {}),
            "lastRevokedAdoptionId": adoption_id,
            "lastRevokedAt": datetime.now().astimezone().isoformat(
                timespec="seconds"
            ),
        }
        try:
            new_quotes.revoke_vision_adoption(
                project_id,
                draft_id,
                adoption_id,
                actor=current_user(request).username,
                expected_revision=int(expected_revision),
                exceptions=draft_exceptions(restored, price_book),
            )
        except RevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        log_action(
            request,
            "vision_adoption_revoked",
            project_id=project.id,
            detail={
                "taskId": adoption["taskId"],
                "draftId": draft_id,
                "adoptionId": adoption_id,
            },
        )
        return RedirectResponse(
            f"/projects/{project.id}/drafts/{draft_id}?vision=revoked",
            status_code=303,
        )

    @app.get(
        "/projects/{project_id}/price-books/compare",
        response_class=HTMLResponse,
    )
    def price_books_compare(
        request: Request,
        project_id: str,
        left: str = "",
        right: str = "",
    ):
        project = owned_project(request, project_id)
        books = new_quotes.list_price_books(project_id)
        if len(books) < 2:
            return RedirectResponse(
                f"/projects/{project.id}/custom", status_code=303
            )
        left_book = new_quotes.get_price_book(project_id, left) if left else books[0]
        right_book = (
            new_quotes.get_price_book(project_id, right)
            if right
            else books[1]
        )
        if left_book is None or right_book is None:
            raise HTTPException(status_code=404, detail="价格书不存在")
        return HTMLResponse(
            ui.compare_price_books_page(
                _user_payload(current_user(request)),
                project,
                left_book,
                right_book,
            )
        )

    @app.get(
        "/projects/{project_id}/price-books/{price_book_id}",
        response_class=HTMLResponse,
    )
    def price_book_detail(
        request: Request,
        project_id: str,
        price_book_id: str,
        notice: str = "",
    ) -> str:
        project = owned_project(request, project_id)
        price_book = new_quotes.get_price_book(project_id, price_book_id)
        if price_book is None:
            raise HTTPException(status_code=404, detail="价格书不存在")
        notices = {
            "already-imported": "这份模板已经导入过价格，直接复用现有价格书。",
            "project-price-added": "项目价行已添加到这份价格书；可以在草稿的模板行里选择它。",
            "row-updated": "价格行已更新。",
            "row-deleted": "价格行已删除。",
        }
        preference = template_preferences.get(project.owner)
        return HTMLResponse(
            ui.price_book_page(
                _user_payload(current_user(request)),
                project,
                price_book,
                notice=notice,
                is_account_default=bool(
                    preference
                    and str(preference.get("sha256") or "")
                    == str(
                        (price_book.get("book") or {}).get("sourceSha256")
                        or ""
                    )
                ),
            )
        )

    @app.post(
        "/projects/{project_id}/price-books/{price_book_id}/project-price"
    )
    def add_project_price(
        request: Request,
        project_id: str,
        price_book_id: str,
        name: str = Form(...),
        spec: str = Form(""),
        unit: str = Form(""),
        material: str = Form(...),
        installation: str = Form(...),
        category: str = Form("equipment"),
        system: str = Form("general"),
        aliases: str = Form(""),
    ):
        project = owned_project(request, project_id)
        price_book = new_quotes.get_price_book(project_id, price_book_id)
        if price_book is None:
            raise HTTPException(status_code=404, detail="价格书不存在")
        name = name.strip()
        if not name:
            return HTMLResponse(
                ui.price_book_page(
                    _user_payload(current_user(request)),
                    project,
                    price_book,
                    error="项目价行需要填写名称。",
                ),
                status_code=400,
            )
        try:
            material_value = Decimal(material)
            installation_value = Decimal(installation)
        except (InvalidOperation, TypeError, ValueError):
            material_value = Decimal("-1")
            installation_value = Decimal("-1")
        if material_value < 0 or installation_value < 0:
            return HTMLResponse(
                ui.price_book_page(
                    _user_payload(current_user(request)),
                    project,
                    price_book,
                    error="项目价的材料单价和安装单价不能为负数。",
                ),
                status_code=400,
            )
        alias_list = [
            item.strip()
            for item in re.split(r"[,，;；\n]+", aliases)
            if item.strip()
        ]
        try:
            new_quotes.add_project_price_row(
                project_id,
                price_book_id,
                name=name,
                spec=spec.strip(),
                unit=unit.strip(),
                material=str(material_value),
                installation=str(installation_value),
                category=category,
                system=system,
                aliases=alias_list,
            )
        except ValueError as exc:
            return HTMLResponse(
                ui.price_book_page(
                    _user_payload(current_user(request)),
                    project,
                    price_book,
                    error=str(exc),
                ),
                status_code=400,
            )
        log_action(
            request,
            "project_price_added",
            project_id=project.id,
            detail={
                "priceBookId": price_book_id,
                "name": name,
                "material": str(material_value),
                "installation": str(installation_value),
                "aliases": alias_list,
            },
        )
        return RedirectResponse(
            f"/projects/{project.id}/price-books/{price_book_id}"
            "?notice=project-price-added",
            status_code=303,
        )

    @app.post(
        "/projects/{project_id}/price-books/{price_book_id}/rows/{row_number}"
    )
    def price_book_row_edit(
        request: Request,
        project_id: str,
        price_book_id: str,
        row_number: int,
        material: str = Form(""),
        installation: str = Form(""),
        unit: str = Form(""),
    ):
        project = owned_project(request, project_id)
        try:
            material_value = Decimal(material)
            installation_value = Decimal(installation)
        except (InvalidOperation, TypeError, ValueError):
            material_value = Decimal("-1")
            installation_value = Decimal("-1")
        price_book = new_quotes.get_price_book(project_id, price_book_id)
        if price_book is None:
            raise HTTPException(status_code=404, detail="价格书不存在")
        if material_value < 0 or installation_value < 0:
            return HTMLResponse(
                ui.price_book_page(
                    _user_payload(current_user(request)),
                    project,
                    price_book,
                    error="材料单价和安装单价需要是非负数字。",
                ),
                status_code=400,
            )
        try:
            new_quotes.update_price_book_row(
                project_id,
                price_book_id,
                row_number,
                material=str(material_value),
                installation=str(installation_value),
                unit=unit,
            )
        except ValueError as exc:
            return HTMLResponse(
                ui.price_book_page(
                    _user_payload(current_user(request)),
                    project,
                    price_book,
                    error=str(exc),
                ),
                status_code=400,
            )
        log_action(
            request,
            "price_book_row_updated",
            project_id=project.id,
            detail={
                "priceBookId": price_book_id,
                "rowNumber": row_number,
                "material": str(material_value),
                "installation": str(installation_value),
            },
        )
        return RedirectResponse(
            f"/projects/{project.id}/price-books/{price_book_id}"
            "?notice=row-updated",
            status_code=303,
        )

    @app.post(
        "/projects/{project_id}/price-books/{price_book_id}"
        "/rows/{row_number}/delete"
    )
    def price_book_row_delete(
        request: Request,
        project_id: str,
        price_book_id: str,
        row_number: int,
    ):
        project = owned_project(request, project_id)
        price_book = new_quotes.get_price_book(project_id, price_book_id)
        if price_book is None:
            raise HTTPException(status_code=404, detail="价格书不存在")
        try:
            new_quotes.delete_price_book_row(project_id, price_book_id, row_number)
        except ValueError as exc:
            return HTMLResponse(
                ui.price_book_page(
                    _user_payload(current_user(request)),
                    project,
                    price_book,
                    error=str(exc),
                ),
                status_code=400,
            )
        log_action(
            request,
            "price_book_row_deleted",
            project_id=project.id,
            detail={"priceBookId": price_book_id, "rowNumber": row_number},
        )
        return RedirectResponse(
            f"/projects/{project.id}/price-books/{price_book_id}"
            "?notice=row-deleted",
            status_code=303,
        )

    @app.post(
        "/projects/{project_id}/price-books/{price_book_id}/delete"
    )
    def price_book_delete(request: Request, project_id: str, price_book_id: str):
        project = owned_project(request, project_id)
        price_book = new_quotes.get_price_book(project_id, price_book_id)
        if price_book is None:
            raise HTTPException(status_code=404, detail="价格书不存在")
        try:
            new_quotes.delete_price_book(project_id, price_book_id)
        except ValueError as exc:
            return HTMLResponse(
                ui.price_book_page(
                    _user_payload(current_user(request)),
                    project,
                    price_book,
                    error=str(exc),
                ),
                status_code=400,
            )
        log_action(
            request,
            "price_book_deleted",
            project_id=project.id,
            detail={
                "priceBookId": price_book_id,
                "name": price_book.get("name") or "",
                "fileName": price_book.get("fileName") or "",
            },
        )
        return RedirectResponse(
            f"/projects/{project.id}/custom", status_code=303
        )

    @app.post("/projects/{project_id}/custom/default-template")
    def use_default_template(request: Request, project_id: str):
        project = owned_project(request, project_id)
        source = default_template_path(data_root)
        if source is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "系统默认模板不存在。请上传客户模板，或把 default-template.xlsx "
                    "放到数据目录，或设置 HVAC_DEFAULT_TEMPLATE。"
                ),
            )
        source_bytes = source.read_bytes()
        source_sha = hashlib.sha256(source_bytes).hexdigest()
        existing = next(
            (
                item
                for item in project.files
                if item.kind == "template" and item.sha256 == source_sha
            ),
            None,
        )
        files_dir = _project_files_dir(data_root, project_id)
        files_dir.mkdir(parents=True, exist_ok=True)
        if existing is None:
            stored_name = f"file-{uuid4().hex}.xlsx"
            target = files_dir / stored_name
            target.write_bytes(source_bytes)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
            existing = projects.add_file(
                project.owner,
                project.id,
                original_name="系统默认模板.xlsx",
                stored_name=stored_name,
                kind="template",
                size=len(source_bytes),
                sha256=source_sha,
            )
        existing_book = new_quotes.price_book_for_file(project.id, existing.id)
        if existing_book is not None:
            return RedirectResponse(
                f"/projects/{project.id}/price-books/{existing_book['id']}"
                "?notice=already-imported",
                status_code=303,
            )
        path = _project_file_path(data_root, project_id, existing.stored_name)
        try:
            book = import_price_book_auto(
                path, file_id=existing.id, source_sha256=source_sha
            )
            price_book = new_quotes.save_price_book(
                project.id, existing.id, "系统默认模板", book
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("默认模板价格导入失败")
            return HTMLResponse(
                ui.custom_workspace_page(
                    _user_payload(current_user(request)),
                    project,
                    new_quotes.list_inspections(project_id),
                    new_quotes.list_price_books(project_id),
                    new_quotes.list_drafts(project_id),
                    default_template_available=default_template_path(data_root)
                    is not None,
                ),
                status_code=400,
            )
        log_action(
            request,
            "template_imported",
            project_id=project.id,
            detail={
                "fileId": existing.id,
                "priceBookId": price_book["id"],
                "rows": book.get("counts", {}).get("rows", 0),
                "default": True,
            },
        )
        return RedirectResponse(
            f"/projects/{project.id}/price-books/{price_book['id']}",
            status_code=303,
        )

    @app.get(
        "/projects/{project_id}/copper-prices",
        response_class=HTMLResponse,
    )
    def copper_price_page(request: Request, project_id: str) -> str:
        project = owned_project(request, project_id)
        return HTMLResponse(
            ui.copper_price_page(
                _user_payload(current_user(request)),
                project,
                new_quotes.list_price_books(project_id),
                copper_prices.list_for_project(project_id),
            )
        )

    @app.post("/projects/{project_id}/copper-prices")
    async def copper_price_submit(request: Request, project_id: str):
        project = owned_project(request, project_id)
        user = current_user(request)
        form = await request.form()
        action = str(form.get("action") or "add")
        record_id = str(form.get("record_id") or "").strip()

        def render_error(message: str) -> HTMLResponse:
            return HTMLResponse(
                ui.copper_price_page(
                    _user_payload(user),
                    project,
                    new_quotes.list_price_books(project_id),
                    copper_prices.list_for_project(project_id),
                    error=message,
                ),
                status_code=400,
            )

        if action == "confirm":
            record = copper_prices.confirm(project_id, record_id, user.username)
            if record is None:
                return render_error("参考价记录不存在。")
            log_action(
                request,
                "copper_price_confirmed",
                project_id=project.id,
                detail={
                    "recordId": record_id,
                    "version": record["version"],
                },
            )
            return RedirectResponse(
                f"/projects/{project.id}/copper-prices", status_code=303
            )
        if action == "delete":
            if not copper_prices.delete_draft(project_id, record_id):
                return render_error("只能删除草稿状态的参考价记录。")
            log_action(
                request,
                "copper_price_deleted",
                project_id=project.id,
                detail={"recordId": record_id},
            )
            return RedirectResponse(
                f"/projects/{project.id}/copper-prices", status_code=303
            )
        if action != "add":
            return render_error("参考价操作无效。")

        template_ref = str(form.get("template_ref") or "").strip()
        price_book_id = None
        template_row = None
        template_material_price = None
        if template_ref:
            try:
                price_book_id, row_text = template_ref.split("|", 1)
                template_row = int(row_text)
            except (ValueError, TypeError):
                return render_error("模板材料价选择无效。")
            price_book = new_quotes.get_price_book(project_id, price_book_id)
            if price_book is None:
                return render_error("所选模板价格书不存在。")
            row = next(
                (
                    item
                    for item in (price_book.get("book") or {}).get("rows", [])
                    if int(item.get("rowNumber") or 0) == template_row
                ),
                None,
            )
            if (
                row is None
                or row.get("priceState") != "priced"
                or row.get("category") != "pipe"
                or not any(
                    marker in f"{row.get('name', '')}{row.get('spec', '')}"
                    for marker in ("铜管", "冷媒", "液管", "气管")
                )
            ):
                return render_error("所选模板行不是可计价的铜管行。")
            template_material_price = str(row.get("material"))
        try:
            record = copper_prices.create(
                owner=project.owner,
                project_id=project.id,
                record_date=str(form.get("record_date") or "").strip(),
                region=str(form.get("region") or "").strip(),
                source_name=str(form.get("source_name") or "").strip(),
                source_url=str(form.get("source_url") or "").strip(),
                grade=str(form.get("grade") or "TP2").strip(),
                outer_diameter_mm=form.get("outer_diameter_mm"),
                wall_thickness_mm=form.get("wall_thickness_mm"),
                copper_price_per_ton=form.get("copper_price_per_ton"),
                processing_fee_per_ton=form.get("processing_fee_per_ton"),
                price_book_id=price_book_id,
                template_row=template_row,
                template_material_price=template_material_price,
                note=str(form.get("note") or "").strip(),
                created_by=user.username,
            )
        except ValueError as exc:
            return render_error(str(exc))
        log_action(
            request,
            "copper_price_recorded",
            project_id=project.id,
            detail={
                "recordId": record["id"],
                "grade": record["grade"],
                "pricePerM": record["pricePerM"],
                "status": record["status"],
            },
        )
        return RedirectResponse(
            f"/projects/{project.id}/copper-prices", status_code=303
        )

    @app.get("/projects/{project_id}/custom", response_class=HTMLResponse)
    def custom_workspace(request: Request, project_id: str) -> str:
        project = owned_project(request, project_id)
        try:
            template_preferences.ensure_project(
                owner=project.owner,
                project_id=project.id,
                projects=projects,
                new_quotes=new_quotes,
            )
        except (OSError, ValueError):
            logger.exception("自动准备账号默认模板失败")
        preference = template_preferences.get(project.owner)
        return HTMLResponse(
            ui.custom_workspace_page(
                _user_payload(current_user(request)),
                project,
                new_quotes.list_inspections(project_id),
                new_quotes.list_price_books(project_id),
                new_quotes.list_drafts(project_id),
                default_template_available=default_template_path(data_root)
                is not None,
                account_default=preference,
            )
        )

    @app.post("/projects/{project_id}/custom/drafts")
    async def create_custom_draft(
        request: Request,
        project_id: str,
        inspection_id: str = Form(""),
        inspection_ids: list[str] = Form(default=[]),
        price_book_id: str = Form(""),
        title: str = Form(""),
        scope: str = Form(""),
        use_account_default: str = Form(""),
    ):
        project = owned_project(request, project_id)
        if use_account_default or price_book_id == "__account_default__":
            try:
                prepared = template_preferences.ensure_project(
                    owner=project.owner,
                    project_id=project.id,
                    projects=projects,
                    new_quotes=new_quotes,
                )
            except (OSError, ValueError) as exc:
                raise HTTPException(
                    status_code=400, detail=str(exc)
                ) from exc
            if prepared is None:
                raise HTTPException(
                    status_code=400,
                    detail="账号还没有默认模板，请先上传模板并导入价格。",
                )
            price_book_id = str(prepared["id"])
        requested_ids = [item for item in [inspection_id, *(inspection_ids or [])] if item]
        requested_ids = list(dict.fromkeys(requested_ids))
        if not requested_ids:
            raise HTTPException(status_code=400, detail="请选择至少一份图纸检查")
        inspections = []
        for item in requested_ids:
            inspection = new_quotes.get_inspection(project_id, item)
            if inspection is None:
                raise HTTPException(status_code=404, detail="图纸检查不存在")
            inspections.append(inspection)
        price_book = new_quotes.get_price_book(project_id, price_book_id)
        if price_book is None:
            raise HTTPException(status_code=404, detail="价格书不存在")
        rules = new_quotes.get_mapping_rules(project_id)
        if len(inspections) == 1:
            data = build_draft_data(
                inspections[0],
                price_book,
                title=title.strip(),
                view=scope.strip(),
                rules=rules,
            )
        else:
            data = build_multi_draft_data(
                inspections,
                price_book,
                title=title.strip(),
                rules=rules,
            )
        form = await request.form()
        multipliers = {}
        for inspection in inspections:
            raw = str(form.get(f"multiplier_{inspection['id']}") or "1").strip()
            try:
                multiplier = Decimal(raw or "1")
            except (InvalidOperation, ValueError):
                multiplier = Decimal("1")
            if multiplier < 1 or multiplier > 20:
                multiplier = Decimal("1")
            multipliers[str(inspection["id"])] = multiplier
        primary = inspections[0]
        apply_source_multipliers(
            data, multipliers, default_source_id=str(primary["id"])
        )
        draft = new_quotes.create_draft(
            project_id, str(primary["id"]), price_book_id, data
        )
        exceptions = draft_exceptions(data, price_book)
        new_quotes.replace_exceptions(project_id, draft["id"], exceptions)
        log_action(
            request,
            "draft_created",
            project_id=project.id,
            detail={
                "draftId": draft["id"],
                "inspectionId": primary["id"],
                "inspectionIds": requested_ids,
                "priceBookId": price_book_id,
                "scope": scope,
                "multipliers": {key: str(value) for key, value in multipliers.items()},
            },
        )
        return RedirectResponse(
            f"/projects/{project.id}/drafts/{draft['id']}", status_code=303
        )

    @app.post("/projects/{project_id}/drafts/{draft_id}/copy")
    def draft_copy(request: Request, project_id: str, draft_id: str):
        project = owned_project(request, project_id)
        if new_quotes.get_draft(project_id, draft_id) is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        try:
            copied = new_quotes.copy_draft(project_id, draft_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        log_action(
            request,
            "draft_copied",
            project_id=project.id,
            detail={"draftId": draft_id, "newDraftId": copied["id"]},
        )
        return RedirectResponse(
            f"/projects/{project.id}/drafts/{copied['id']}", status_code=303
        )

    @app.post("/projects/{project_id}/drafts/{draft_id}/delete")
    def draft_delete(
        request: Request,
        project_id: str,
        draft_id: str,
        expected_revision: str = Form(""),
    ):
        project = owned_project(request, project_id)
        if new_quotes.get_draft(project_id, draft_id) is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        try:
            revision = int(str(expected_revision).strip())
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=409,
                detail="缺少草稿版本，请刷新后重试。",
            ) from None
        try:
            new_quotes.delete_draft(
                project_id, draft_id, expected_revision=revision
            )
        except RevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        log_action(
            request,
            "draft_deleted",
            project_id=project.id,
            detail={"draftId": draft_id},
        )
        return RedirectResponse(
            f"/projects/{project.id}/custom", status_code=303
        )

    @app.get(
        "/projects/{project_id}/drafts/{draft_id}",
        response_class=HTMLResponse,
    )
    def draft_detail(
        request: Request, project_id: str, draft_id: str
    ) -> str:
        project = owned_project(request, project_id)
        draft = new_quotes.get_draft(project_id, draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        price_book = (
            new_quotes.get_price_book(project_id, draft["priceBookId"])
            if draft.get("priceBookId")
            else None
        )
        if price_book is None:
            raise HTTPException(status_code=409, detail="草稿缺少模板价格书")
        tasks = vision_service.list_tasks(
            current_user(request).username, project_id, draft_id=draft_id
        )
        return HTMLResponse(
            ui.draft_page(
                _user_payload(current_user(request)),
                project,
                draft,
                price_book,
                new_quotes.list_exceptions(project_id, draft_id),
                price_books=new_quotes.list_price_books(project_id),
                publication_key=_draft_publication_key(
                    new_quotes,
                    owner=project.owner,
                    project_id=project_id,
                    draft=draft,
                ),
                vision={
                    "enabled": bool(
                        get_preferences(db, project.owner).get(
                            "visionEnabled"
                        )
                    ),
                    "config": VisionConfig.from_env().safe_identity(),
                    "tasks": [
                        {
                            "id": item.id,
                            "status": item.status,
                            "createdAt": item.created_at,
                        }
                        for item in tasks
                    ],
                    "adoptions": list(
                        new_quotes.list_vision_adoptions(
                            project_id, draft_id
                        )
                    ),
                },
            )
        )

    @app.get("/projects/{project_id}/drafts/{draft_id}/missing-prices.xlsx")
    def draft_missing_prices(request: Request, project_id: str, draft_id: str):
        project = owned_project(request, project_id)
        draft = new_quotes.get_draft(project_id, draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        price_book = (
            new_quotes.get_price_book(project_id, draft["priceBookId"])
            if draft.get("priceBookId")
            else None
        )
        if price_book is None:
            raise HTTPException(status_code=409, detail="草稿缺少模板价格书")
        content = build_missing_price_workbook(draft["data"], price_book)
        log_action(
            request,
            "download",
            project_id=project.id,
            detail={"draftId": draft_id, "kind": "missing-prices"},
        )
        return Response(
            content=content,
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''"
                    + quote("缺价清单.xlsx")
                )
            },
        )

    @app.get("/projects/{project_id}/drafts/{draft_id}/supplement-template.xlsx")
    def draft_supplement_template(
        request: Request, project_id: str, draft_id: str
    ) -> Response:
        project = owned_project(request, project_id)
        draft = new_quotes.get_draft(project_id, draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        price_book = (
            new_quotes.get_price_book(project_id, draft["priceBookId"])
            if draft.get("priceBookId")
            else None
        )
        if price_book is None:
            raise HTTPException(status_code=409, detail="草稿缺少模板价格书")
        content = build_supplement_workbook(price_book)
        return Response(
            content=content,
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition": (
                    "attachment; filename=supplement-template.xlsx"
                )
            },
        )

    @app.post(
        "/projects/{project_id}/drafts/{draft_id}/bulk-supplement",
        response_class=HTMLResponse,
    )
    async def draft_bulk_supplement(
        request: Request,
        project_id: str,
        draft_id: str,
        upload: UploadFile = File(...),
        expected_revision: str = Form(""),
    ):
        project = owned_project(request, project_id)
        draft = new_quotes.get_draft(project_id, draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        price_book = (
            new_quotes.get_price_book(project_id, draft["priceBookId"])
            if draft.get("priceBookId")
            else None
        )
        if price_book is None:
            raise HTTPException(status_code=409, detail="草稿缺少模板价格书")
        try:
            bulk_revision = int(str(expected_revision).strip())
        except (TypeError, ValueError):
            bulk_revision = -1
        if bulk_revision != int(draft.get("revision") or 1):
            await upload.close()
            return HTMLResponse(
                ui.draft_page(
                    _user_payload(current_user(request)),
                    project,
                    draft,
                    price_book,
                    new_quotes.list_exceptions(project_id, draft_id),
                    price_books=new_quotes.list_price_books(project_id),
                    error="草稿已在其他页面或窗口修改，请刷新后重新上传补充表。",
                ),
                status_code=409,
            )
        name = str(upload.filename or "")
        if not name.lower().endswith(".xlsx"):
            await upload.close()
            return HTMLResponse(
                ui.draft_page(
                    _user_payload(current_user(request)),
                    project,
                    draft,
                    price_book,
                    new_quotes.list_exceptions(project_id, draft_id),
                    price_books=new_quotes.list_price_books(project_id),
                    error="批量补充只支持 XLSX；请使用系统下载的补充表。",
                ),
                status_code=400,
            )
        content = await upload.read()
        await upload.close()
        if len(content) > 10 * 1024 * 1024:
            return HTMLResponse(
                ui.draft_page(
                    _user_payload(current_user(request)),
                    project,
                    draft,
                    price_book,
                    new_quotes.list_exceptions(project_id, draft_id),
                    price_books=new_quotes.list_price_books(project_id),
                    error="批量补充表不能超过 10 MB。",
                ),
                status_code=400,
            )
        parsed, errors = parse_supplement(content, price_book)
        if errors:
            message = "批量补充未写入：" + "；".join(errors[:5])
            if len(errors) > 5:
                message += f"；另有 {len(errors) - 5} 处错误"
            return HTMLResponse(
                ui.draft_page(
                    _user_payload(current_user(request)),
                    project,
                    draft,
                    price_book,
                    new_quotes.list_exceptions(project_id, draft_id),
                    price_books=new_quotes.list_price_books(project_id),
                    error=message,
                ),
                status_code=400,
            )
        counts = apply_supplement(
            draft.get("data") or {},
            parsed,
            rows=(price_book.get("book") or {}).get("rows") or [],
        )
        if not any(counts.values()):
            return HTMLResponse(
                ui.draft_page(
                    _user_payload(current_user(request)),
                    project,
                    draft,
                    price_book,
                    new_quotes.list_exceptions(project_id, draft_id),
                    price_books=new_quotes.list_price_books(project_id),
                    error="补充表没有可导入的行；请填写设备、管线或安装项。",
                ),
                status_code=400,
            )
        try:
            _sync_manual_route_records(
                draft.get("data") or {}, project.id
            )
            refreshed = new_quotes.update_draft(
                project_id,
                draft_id,
                data=draft.get("data") or {},
                expected_revision=bulk_revision,
                exceptions=draft_exceptions(
                    (draft.get("data") or {}), price_book
                ),
            ) or draft
        except RevisionConflict:
            return HTMLResponse(
                ui.draft_page(
                    _user_payload(current_user(request)),
                    project,
                    draft,
                    price_book,
                    new_quotes.list_exceptions(project_id, draft_id),
                    price_books=new_quotes.list_price_books(project_id),
                    error="草稿已被其他操作修改，本次补充未写入；请刷新后重试。",
                ),
                status_code=409,
            )
        log_action(
            request,
            "draft_bulk_supplement",
            project_id=project.id,
            detail={"draftId": draft_id, **counts},
        )
        return RedirectResponse(
            f"/projects/{project.id}/drafts/{draft_id}", status_code=303
        )

    @app.post(
        "/projects/{project_id}/drafts/{draft_id}",
        response_class=HTMLResponse,
    )
    async def draft_submit(
        request: Request, project_id: str, draft_id: str
    ) -> HTMLResponse:
        project = owned_project(request, project_id)
        draft = new_quotes.get_draft(project_id, draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="草稿不存在")
        price_book = (
            new_quotes.get_price_book(project_id, draft["priceBookId"])
            if draft.get("priceBookId")
            else None
        )
        if price_book is None:
            raise HTTPException(status_code=409, detail="草稿缺少模板价格书")

        def publication_error(
            message: str,
            *,
            status_code: int = 500,
            keep_key: str = "",
        ) -> HTMLResponse:
            refreshed = draft
            exceptions_now: list[dict] = []
            books_now: list[dict] = []
            try:
                refreshed = (
                    new_quotes.get_draft(project_id, draft_id) or draft
                )
                exceptions_now = new_quotes.list_exceptions(
                    project_id, draft_id
                )
                books_now = new_quotes.list_price_books(project_id)
            except Exception:  # noqa: BLE001 - the notice must still render
                logger.exception("发布失败后读取草稿失败")
            return HTMLResponse(
                ui.draft_page(
                    _user_payload(current_user(request)),
                    project,
                    refreshed,
                    price_book,
                    exceptions_now,
                    price_books=books_now,
                    error=message,
                    publication_key=keep_key,
                ),
                status_code=status_code,
            )

        form = await request.form()
        data = draft.get("data") or {}
        previous_data = copy.deepcopy(data)
        username = current_user(request).username
        publication_key = str(form.get("publication_key") or "").strip()
        request_digest = _publication_request_digest(form)
        if publication_key:
            try:
                prior_intent = new_quotes.get_publication_intent(
                    publication_key
                )
            except Exception:  # noqa: BLE001
                logger.exception("读取生成请求幂等记录失败")
                return publication_error(
                    "生成请求的幂等记录暂时无法读取；本次未生成，"
                    "请稍后刷新重试。"
                )
            if prior_intent is not None:
                if (
                    prior_intent["owner"] != username
                    or prior_intent["projectId"] != project_id
                    or prior_intent["draftId"] != draft_id
                ):
                    return publication_error(
                        "这个生成请求标识不属于当前草稿，已拒绝。",
                        status_code=409,
                    )
                stored_digest = str(
                    prior_intent.get("requestDigest") or ""
                )
                if (
                    stored_digest
                    and prior_intent["status"] in {"running", "ready"}
                    and stored_digest != request_digest
                ):
                    return publication_error(
                        "同一个生成请求标识的内容已经变化。"
                        "请刷新草稿页，重新核对后开始一次新的生成。",
                        status_code=409,
                    )
                if prior_intent["status"] == "ready":
                    replay_run_id = str(prior_intent.get("runId") or "")
                    try:
                        replay_exists = bool(
                            replay_run_id
                            and new_quotes.quote_exists(
                                project_id, replay_run_id
                            )
                        )
                    except Exception:  # noqa: BLE001
                        replay_exists = False
                    if replay_exists:
                        return RedirectResponse(
                            f"/projects/{project.id}/quotes/"
                            f"{replay_run_id}",
                            status_code=303,
                        )
                    return publication_error(
                        "这条生成请求已有成功记录，但报价登记暂时无法读取；"
                        "正式文件不会被删除，请稍后刷新核对。",
                        status_code=500,
                    )
                if prior_intent["status"] == "running":
                    return publication_error(
                        "同样的生成请求正在处理中，请勿重复提交；"
                        "稍后刷新页面查看结果。",
                        status_code=409,
                    )
                if prior_intent["status"] == "uncertain":
                    return publication_error(
                        "上一次生成请求的提交结果仍待核对。"
                        "正式文件已保留，请先刷新报价记录；"
                        "不要重复生成。",
                        status_code=409,
                    )
        expected_raw = str(form.get("expected_revision") or "").strip()
        try:
            expected_revision = int(expected_raw)
        except (TypeError, ValueError):
            expected_revision = -1
        if expected_revision != int(draft.get("revision") or 1):
            return HTMLResponse(
                ui.draft_page(
                    _user_payload(current_user(request)),
                    project,
                    draft,
                    price_book,
                    new_quotes.list_exceptions(project_id, draft_id),
                    price_books=new_quotes.list_price_books(project_id),
                    error="草稿已在其他页面或窗口修改，请刷新后重新核对本页内容。",
                ),
                status_code=409,
            )
        early_action = str(form.get("action") or "save")
        vision_route_overrides = new_quotes.active_vision_route_overrides(
            project_id, draft_id
        )
        pending_vision = (data.get("visionPreview") or {}).get(
            "requiresConfirmation"
        )
        if early_action == "generate" and pending_vision and str(
            form.get("vision_confirm") or ""
        ) not in {"1", "on", "true"}:
            return HTMLResponse(
                ui.draft_page(
                    _user_payload(current_user(request)),
                    project,
                    draft,
                    price_book,
                    new_quotes.list_exceptions(project_id, draft_id),
                    price_books=new_quotes.list_price_books(project_id),
                    error=(
                        "视觉采纳的差异还没有确认。请核对下方量价变化，"
                        "勾选“确认视觉差异并生成报价”后再生成。"
                    ),
                    publication_key=publication_key,
                ),
                status_code=409,
            )
        default_source_id = str(draft.get("inspectionId") or "")
        scale_text = str(form.get("scale") or "").strip()
        scale_updates: dict[str, str] = {}
        scale_error = ""
        if data.get("scaleEditable", True):
            if scale_text:
                try:
                    scale_value = Decimal(scale_text)
                except (InvalidOperation, TypeError, ValueError):
                    scale_value = None
                if (
                    scale_value is None
                    or not scale_value.is_finite()
                    or scale_value <= 0
                ):
                    scale_error = "比例要填大于 0 的数字（米/图面单位）。"
                elif scale_text != str(data.get("scale") or ""):
                    scale_updates[default_source_id] = scale_text
            data["scale"] = scale_text or None
        else:
            stored_scales = dict(data.get("sourceScales") or {})
            sources = data.get("sourceFiles") or [
                {"inspectionId": default_source_id, "fileName": "本图纸"}
            ]
            for source in sources:
                source_id = str(source.get("inspectionId") or "")
                raw_scale = form.get(f"source_scale_{source_id}")
                if raw_scale is None:
                    continue
                raw_scale = str(raw_scale).strip()
                if not raw_scale:
                    continue
                try:
                    scale_value = Decimal(raw_scale)
                except (InvalidOperation, TypeError, ValueError):
                    scale_value = None
                if (
                    scale_value is None
                    or not scale_value.is_finite()
                    or scale_value <= 0
                ):
                    scale_error = (
                        f"{source.get('fileName') or '图纸'}的比例要填大于 0 的数字。"
                    )
                    break
                if raw_scale != str(stored_scales.get(source_id) or ""):
                    scale_updates[source_id] = raw_scale
                    stored_scales[source_id] = raw_scale
            data["sourceScales"] = stored_scales
        for index, group in enumerate(data.get("equipment", [])):
            include = f"equipment_include_{index}" in form
            manual_component = f"equipment_component_{index}" in form
            set_confirmed = f"equipment_set_confirm_{index}" in form
            if group.get("isComponent"):
                group["include"] = False
            elif manual_component:
                group["isComponentManual"] = True
                group["include"] = False
            else:
                group["isComponentManual"] = False
                group["include"] = include
            if group.get("setIncomplete") is not None:
                group["setConfirmed"] = set_confirmed
            quantity = str(form.get(f"equipment_quantity_{index}") or "").strip()
            if quantity:
                group["quantity"] = quantity
            price_row = str(
                form.get(f"equipment_price_{index}") or ""
            ).strip()
            group["priceRow"] = int(price_row) if price_row else None
            group["manualMaterial"] = (
                str(form.get(f"equipment_manual_material_{index}") or "").strip()
                or None
            )
            group["manualInstallation"] = (
                str(form.get(f"equipment_manual_installation_{index}") or "").strip()
                or None
            )
            group["manualUnit"] = (
                str(form.get(f"equipment_manual_unit_{index}") or "").strip()
                or None
            )
            group["manualLabel"] = (
                str(form.get(f"equipment_manual_label_{index}") or "").strip()
                or None
            )
            if (
                group.get("setIssueRow")
                and group.get("priceRow") != group.get("setIssueRow")
            ):
                group["setIncomplete"] = False
                group["issues"] = [
                    issue
                    for issue in group.get("issues", [])
                    if "整套计价" not in str(issue)
                ]
        review_requests: dict[int, bool] = {}
        for index, group in enumerate(data.get("routes", [])):
            group["include"] = f"route_include_{index}" in form
            review_requests[index] = (
                f"route_review_confirm_{index}" in form
            )
            previous_spec = str(group.get("manualSpec") or "")
            length = str(form.get(f"route_length_{index}") or "").strip()
            if length:
                group["lengthM"] = length
            price_row = str(form.get(f"route_price_{index}") or "").strip()
            group["priceRow"] = int(price_row) if price_row else None
            group["manualMaterial"] = (
                str(form.get(f"route_manual_material_{index}") or "").strip()
                or None
            )
            group["manualInstallation"] = (
                str(form.get(f"route_manual_installation_{index}") or "").strip()
                or None
            )
            group["manualUnit"] = (
                str(form.get(f"route_manual_unit_{index}") or "").strip()
                or None
            )
            manual_spec = (
                str(form.get(f"route_manual_spec_{index}") or "").strip() or None
            )
            group["manualSpec"] = manual_spec
            spec_codes = {
                "PIPE_SPEC_UNRESOLVED",
                "PIPE_SPEC_AMBIGUOUS",
                "PIPE_SPEC_INFERRED",
            }
            if manual_spec:
                if group.get("specSource") != "customer":
                    group["specSourceBeforeConfirm"] = group.get("specSource") or ""
                    group["specIssuesBeforeConfirm"] = [
                        str(issue)
                        for issue in group.get("issues") or []
                        if str(issue) in spec_codes
                    ]
                group["issues"] = [
                    issue
                    for issue in group.get("issues") or []
                    if str(issue) not in spec_codes
                ]
                group["specSource"] = "customer"
                group["specConfirmed"] = True
            else:
                if group.get("specSource") == "customer":
                    restore = group.get("specIssuesBeforeConfirm") or []
                    existing = [str(issue) for issue in group.get("issues") or []]
                    group["issues"] = list(group.get("issues") or []) + [
                        issue for issue in restore if issue not in existing
                    ]
                    group["specSource"] = group.get("specSourceBeforeConfirm") or ""
                group["specConfirmed"] = False
            if manual_spec != previous_spec:
                # Keep this request's checkbox unapplied until the proposal
                # key comparison below; a changed spec must be re-confirmed.
                group.pop("routeReview", None)
        for index, item in enumerate(data.get("issues") or []):
            if f"ack_issue_{index}" in form:
                item["acknowledged"] = True
            else:
                item.pop("acknowledged", None)
        data.setdefault("globalCoefficient", "1")
        data.setdefault("equipmentCoefficient", "")
        data.setdefault("pipeCoefficient", "")
        data.setdefault("taxRate", "0")
        for field in (
            "globalCoefficient",
            "equipmentCoefficient",
            "pipeCoefficient",
            "taxRate",
        ):
            value = str(form.get(field) or "").strip()
            if value:
                data[field] = value
        tax_basis = str(
            form.get("taxBasis") or data.get("taxBasis") or "subtotal"
        ).strip()
        if tax_basis not in {"subtotal", "installation_and_auxiliary"}:
            tax_basis = "subtotal"
        data["taxBasis"] = tax_basis
        data["secondPriceBookId"] = (
            str(form.get("secondPriceBookId") or "").strip() or None
        )
        # 成套关系按当前数量重新推导；不能沿用上次保存的布尔值。
        data = reconcile_equipment_sets(
            data,
            {
                int(row["rowNumber"]): row
                for row in (price_book.get("book") or {}).get("rows", [])
            },
        )
        _sync_manual_route_records(data, project.id)
        legacy_recovered = _restore_legacy_multiplied_routes(
            new_quotes, project_id, data
        )

        def early_error(message: str) -> HTMLResponse:
            return HTMLResponse(
                ui.draft_page(
                    _user_payload(current_user(request)),
                    project,
                    draft,
                    price_book,
                    new_quotes.list_exceptions(project_id, draft_id),
                    price_books=new_quotes.list_price_books(project_id),
                    error=message,
                ),
                status_code=400,
            )

        def revision_conflict() -> HTMLResponse:
            refreshed = new_quotes.get_draft(project_id, draft_id) or draft
            return HTMLResponse(
                ui.draft_page(
                    _user_payload(current_user(request)),
                    project,
                    refreshed,
                    price_book,
                    new_quotes.list_exceptions(project_id, draft_id),
                    price_books=new_quotes.list_price_books(project_id),
                    error=(
                        "草稿已在其他页面或窗口修改，本次操作未写入；"
                        "请刷新后重新核对本页内容。"
                    ),
                ),
                status_code=409,
            )

        if scale_error:
            return early_error(scale_error)
        if scale_updates:
            apply_source_scales(
                data, scale_updates, default_source_id=default_source_id
            )
            for item in data.get("issues") or []:
                code = str(item.get("code") or "").upper()
                source = str(
                    item.get("sourceInspectionId") or default_source_id or ""
                )
                if source in scale_updates and (
                    "SCALE" in code or "UNIT" in code
                ):
                    item["acknowledged"] = True
                    item["acknowledgedBy"] = "scale"

        custom_action = str(form.get("custom_action") or "").strip()
        if custom_action:
            items = list(data.get("customItems") or [])
            if custom_action.startswith("remove:"):
                try:
                    remove_index = int(custom_action.split(":", 1)[1])
                except (IndexError, ValueError):
                    return early_error("要移除的补充项目无效。")
                if remove_index < 0 or remove_index >= len(items):
                    return early_error("要移除的补充项目不存在。")
                items.pop(remove_index)
                data["customItems"] = items
            elif custom_action == "add":
                name = str(form.get("custom_name") or "").strip()
                if not name:
                    return early_error("补充项目需要填写名称。")
                try:
                    quantity_value = Decimal(
                        str(form.get("custom_quantity") or "0")
                    )
                    material_value = Decimal(
                        str(form.get("custom_material") or "0")
                    )
                    installation_value = Decimal(
                        str(form.get("custom_installation") or "0")
                    )
                except (InvalidOperation, TypeError, ValueError):
                    return early_error("补充项目的数量或价格无效。")
                if (
                    quantity_value <= 0
                    or material_value < 0
                    or installation_value < 0
                ):
                    return early_error(
                        "补充项目数量必须大于 0，材料价和安装价不能为负数。"
                    )
                items.append(
                    {
                        "name": name,
                        "spec": str(form.get("custom_spec") or "").strip(),
                        "unit": str(form.get("custom_unit") or "").strip()
                        or (
                            "台"
                            if str(form.get("custom_category")) == "equipment"
                            else "米"
                        ),
                        "category": (
                            "equipment"
                            if str(form.get("custom_category")) == "equipment"
                            else "pipe"
                        ),
                        "floor": str(form.get("custom_floor") or "补充项目").strip(),
                        "quantity": str(quantity_value),
                        "material": str(material_value),
                        "installation": str(installation_value),
                    }
                )
                data["customItems"] = items
            else:
                return early_error("补充项目操作无效。")
            _sync_manual_route_records(data, project.id)
            try:
                new_quotes.update_draft(
                    project_id,
                    draft_id,
                    data=data,
                    expected_revision=expected_revision,
                    exceptions=draft_exceptions(data, price_book),
                )
            except RevisionConflict:
                return revision_conflict()
            log_action(
                request,
                "draft_custom_items",
                project_id=project.id,
                detail={"draftId": draft_id, "action": custom_action},
            )
            return RedirectResponse(
                f"/projects/{project.id}/drafts/{draft_id}", status_code=303
            )

        add_kind = str(form.get("add_kind") or "")
        if add_kind:
            add_label = str(form.get("add_label") or "").strip()
            add_price = str(form.get("add_price_row") or "").strip()
            add_floor = str(form.get("add_floor") or "人工补录").strip()
            if not add_label or not add_price:
                return early_error("补录需要填写名称并选择模板行。")
            manual_key = f"manual-{uuid4().hex[:8]}"
            if add_kind == "equipment":
                raw_quantity = str(form.get("add_quantity") or "").strip()
                try:
                    quantity = Decimal(raw_quantity or "0")
                except InvalidOperation:
                    return early_error("补录设备数量无效。")
                if quantity <= 0:
                    return early_error("补录设备数量必须大于 0。")
                data.setdefault("equipment", []).append(
                    {
                        "key": manual_key,
                        "ruleKey": manual_key,
                        "label": add_label,
                        "model": add_label,
                        "layer": "人工补录",
                        "blockName": "",
                        "view": "manual",
                        "floorLabel": add_floor,
                        "quantity": str(quantity),
                        "unit": "台",
                        "priceRow": int(add_price),
                        "priceLabel": "",
                        "include": True,
                        "confidence": "manual",
                        "sourceHandles": [],
                        "textHints": [],
                    }
                )
            elif add_kind == "route":
                raw_length = str(form.get("add_length") or "").strip()
                try:
                    length = Decimal(raw_length or "0")
                except InvalidOperation:
                    return early_error("补录管线长度无效。")
                if length <= 0:
                    return early_error("补录管线长度必须大于 0。")
                manual_group = {
                    "key": manual_key,
                    "ruleKey": manual_key,
                    "label": add_label,
                    "layer": "人工补录",
                    "origin": "customer_input",
                    "manualRecordId": manual_key,
                    "category": str(form.get("add_category") or "管线").strip(),
                    "view": "manual",
                    "floorLabel": add_floor,
                    "lengthRaw": None,
                    "lengthM": str(length),
                    "unit": "米",
                    "priceRow": int(add_price),
                    "priceLabel": "",
                    "include": True,
                    "confidence": "manual",
                    "sourceHandles": [],
                    "textHints": [],
                    "issues": [],
                }
                rows_for_check = (price_book.get("book") or {}).get("rows") or []
                linked_group = link_route_material(manual_group, rows_for_check)
                check_row = next(
                    (
                        row
                        for row in rows_for_check
                        if int(row.get("rowNumber") or 0) == int(add_price)
                    ),
                    None,
                )
                incompatibility = route_row_error(linked_group, check_row)
                if incompatibility:
                    return early_error(f"补录管线：{incompatibility}")
                data.setdefault("routes", []).append(linked_group)
            else:
                return early_error("补录类型无效。")

        rules: dict[tuple[str, str], int] = {}
        for group in data.get("equipment", []):
            if group.get("priceRow"):
                rules[("equipment", str(group.get("ruleKey") or group.get("key")))] = int(
                    group["priceRow"]
                )
        for group in data.get("routes", []):
            if group.get("priceRow"):
                rules[("route", str(group.get("ruleKey") or group.get("key")))] = int(
                    group["priceRow"]
                )
        action = str(form.get("action") or "save")
        needs_reconfirm = _finalize_route_proposals(
            data, previous_data, review_requests
        )
        if needs_reconfirm and action == "generate":
            return HTMLResponse(
                ui.draft_page(
                    _user_payload(current_user(request)),
                    project,
                    draft,
                    price_book,
                    new_quotes.list_exceptions(project_id, draft_id),
                    price_books=new_quotes.list_price_books(project_id),
                    error=(
                        "本轮修改了管线长度、规格或范围，原确认不能继续用于新值。"
                        "请先保存，在页面上按图核对新的计量提案后再确认生成。"
                    ),
                ),
                status_code=409,
            )
        evidence_conflict = _route_evidence_conflict(
            new_quotes,
            project_id,
            data,
            derived_routes=vision_route_overrides,
        )
        if evidence_conflict:
            return early_error(evidence_conflict)
        exceptions = draft_exceptions(data, price_book)

        def render_error(message: str) -> HTMLResponse:
            _sync_manual_route_records(data, project.id)
            try:
                refreshed = new_quotes.update_draft(
                    project_id,
                    draft_id,
                    data=data,
                    expected_revision=expected_revision,
                    exceptions=exceptions,
                )
            except RevisionConflict:
                return revision_conflict()
            return HTMLResponse(
                ui.draft_page(
                    _user_payload(current_user(request)),
                    project,
                    refreshed,
                    price_book,
                    new_quotes.list_exceptions(project_id, draft_id),
                    price_books=new_quotes.list_price_books(project_id),
                    error=message,
                    publication_key=_draft_publication_key(
                        new_quotes,
                        owner=project.owner,
                        project_id=project_id,
                        draft=refreshed,
                    ),
                ),
                status_code=400,
            )

        if action == "multipliers":
            multiplier_sources = data.get("sourceFiles") or [
                {"inspectionId": draft.get("inspectionId")}
            ]
            multipliers = {}
            for source in multiplier_sources:
                source_id = str(source.get("inspectionId") or "")
                raw = str(form.get(f"source_multiplier_{source_id}") or "1").strip()
                try:
                    value = Decimal(raw or "1")
                except (InvalidOperation, ValueError):
                    value = Decimal("1")
                if value < 1 or value > 20:
                    value = Decimal("1")
                if source_id:
                    multipliers[source_id] = value
            apply_source_multipliers(
                data,
                multipliers,
                default_source_id=str(draft.get("inspectionId") or ""),
            )
            _finalize_route_proposals(data, previous_data, {})
            _sync_manual_route_records(data, project.id)
            try:
                new_quotes.update_draft(
                    project_id,
                    draft_id,
                    data=data,
                    expected_revision=expected_revision,
                    exceptions=draft_exceptions(data, price_book),
                    mapping_rules=rules,
                )
            except RevisionConflict:
                return revision_conflict()
            log_action(
                request,
                "draft_multipliers",
                project_id=project.id,
                detail={
                    "draftId": draft_id,
                    "legacyMultiplierRecovered": legacy_recovered,
                    "multipliers": {
                        key: str(value) for key, value in multipliers.items()
                    },
                },
            )
            return RedirectResponse(
                f"/projects/{project.id}/drafts/{draft_id}",
                status_code=303,
            )
        if action != "generate":
            if (data.get("visionPreview") or {}).get(
                "requiresConfirmation"
            ):
                data["visionPreview"] = {
                    **dict(data.get("visionPreview") or {}),
                    "requiresConfirmation": False,
                    "invalidatedBySave": True,
                }
            _sync_manual_route_records(data, project.id)
            try:
                new_quotes.update_draft(
                    project_id,
                    draft_id,
                    data=data,
                    expected_revision=expected_revision,
                    exceptions=exceptions,
                    mapping_rules=rules,
                )
            except RevisionConflict:
                return revision_conflict()
            log_action(
                request,
                "draft_saved",
                project_id=project.id,
                detail={
                    "draftId": draft_id,
                    "exceptions": len(exceptions),
                    "legacyMultiplierRecovered": legacy_recovered,
                },
            )
            return RedirectResponse(
                f"/projects/{project.id}/drafts/{draft_id}", status_code=303
            )
        blocking = blocking_exceptions(exceptions)
        if blocking:
            detail = "；".join(
                str(item.get("message") or "") for item in blocking[:2]
            )
            return render_error(f"还有阻塞异常：{detail}")
        _sync_manual_route_records(data, project.id)
        derived_key, publication_fingerprint = _publication_identity(
            owner=username,
            project_id=project_id,
            draft_id=draft_id,
            expected_revision=expected_revision,
            price_book_id=str(price_book.get("id") or ""),
            data=data,
        )
        publication_key = publication_key or derived_key
        try:
            claim = new_quotes.claim_publication_intent(
                key=publication_key,
                owner=username,
                project_id=project_id,
                draft_id=draft_id,
                fingerprint=publication_fingerprint,
                request_digest=request_digest,
            )
        except PublicationConflict as exc:
            return publication_error(str(exc), status_code=409)
        if claim["state"] == "ready":
            replay_run_id = str(claim.get("runId") or "")
            if replay_run_id and new_quotes.quote_exists(
                project_id, replay_run_id
            ):
                return RedirectResponse(
                    f"/projects/{project.id}/quotes/{replay_run_id}",
                    status_code=303,
                )
            return publication_error(
                "这条生成请求已有成功记录，但报价登记暂时无法读取；"
                "正式文件不会被删除，请稍后刷新核对，必要时联系维护人员。",
                status_code=500,
            )
        if claim["state"] == "running":
            return publication_error(
                "同样的生成请求正在处理中，请勿重复提交；"
                "稍后刷新页面查看结果。",
                status_code=409,
            )
        run_id = _new_run_id()
        staging_dir = runs_root / ".staging" / run_id
        try:
            template_found = projects.find_file(
                project.owner, project_id, price_book["fileId"]
            )
            if template_found is None:
                raise ValueError("草稿绑定的模板文件不存在")
            template_path = _project_file_path(
                data_root, project_id, template_found[1].stored_name
            )
            out_dir = staging_dir
            second_book = None
            second_overrides: dict[int, tuple[Decimal, Decimal]] = {}
            second_changed_rows: list[int] = []
            second_id = str(data.get("secondPriceBookId") or "").strip()
            if second_id:
                if second_id == price_book.get("id"):
                    raise ValueError("第二套价格书不能与第一套相同")
                second_book = new_quotes.get_price_book(project_id, second_id)
                if second_book is None:
                    raise ValueError("第二套价格书不存在")
                second_overrides, second_changed_rows = _second_price_overrides(
                    price_book, second_book
                )
                if not second_overrides:
                    raise ValueError(
                        "第二套价格书没有可替换的管线/保温价格；"
                        "请确认它和第一套模板行能对应，且辅材价格确实不同。"
                    )
            authoritative_routes = _authoritative_routes(
                new_quotes,
                project_id,
                data,
                derived_routes=vision_route_overrides,
            )
            manual_route_records = _sync_manual_route_records(
                data, project.id
            )
            result = generate_generic_quote(
                project_name=project.name,
                draft={**draft, "data": data},
                template_path=template_path,
                out_dir=out_dir,
                price_book=price_book,
                authoritative_routes=authoritative_routes,
                manual_route_records=manual_route_records,
                project_id=project.id,
            )
            if second_book is not None:
                second_dir = out_dir / "second"
                second_result = generate_generic_quote(
                    project_name=project.name,
                    draft={**draft, "data": data},
                    template_path=template_path,
                    out_dir=second_dir,
                    price_book=price_book,
                    price_overrides=second_overrides,
                    authoritative_routes=authoritative_routes,
                    manual_route_records=manual_route_records,
                    project_id=project.id,
                )
                shutil.copyfile(
                    second_result.excel_path, out_dir / "quote-second.xlsx"
                )
                shutil.copyfile(
                    second_result.pdf_path, out_dir / "quote-second.pdf"
                )
                shutil.copyfile(
                    second_result.quote_json, out_dir / "quote-second.json"
                )
                second_payload = json.loads(
                    second_result.quote_json.read_text(encoding="utf-8")
                )
                second_payload.setdefault("source", {})["priceBookId"] = second_id
                second_payload["source"]["priceBookName"] = second_book.get(
                    "name", ""
                )
                (out_dir / "quote-second.json").write_text(
                    json.dumps(second_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                second_meta = {
                    "priceBookId": second_id,
                    "priceBookName": second_book.get("name", ""),
                    "subtotal": str(second_result.pricing.subtotal),
                    "tax": str(second_result.pricing.tax),
                    "total": str(second_result.pricing.total),
                    "overriddenRows": second_changed_rows,
                    "excel": "quote-second.xlsx",
                    "pdf": "quote-second.pdf",
                }
                data["secondScheme"] = second_meta
                payload = json.loads(
                    result.quote_json.read_text(encoding="utf-8")
                )
                payload["secondScheme"] = second_meta
                result.quote_json.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except Exception as exc:  # noqa: BLE001
            _remove_staging_dir(staging_dir)
            data.pop("runId", None)
            new_quotes.fail_publication_intent(
                publication_key, f"生成失败：{exc}"
            )
            logger.exception("新图纸报价生成失败")
            return render_error(
                _friendly_error(exc, action="生成新图纸报价")
            )
        total = str(result.pricing.total)
        if pending_vision and str(form.get("vision_confirm") or "") in {
            "1",
            "on",
            "true",
        }:
            data["visionPreview"] = {
                **dict(data.get("visionPreview") or {}),
                "requiresConfirmation": False,
                "confirmedAt": datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
                "confirmedRevision": int(draft.get("revision") or 1),
            }
        data["runId"] = run_id
        final_dir = runs_root / run_id
        published = False
        quote_registration = {
            "run_id": run_id,
            "slice_key": "custom",
            "title": str(data.get("title") or "新图纸报价"),
            "status": "ready",
            "total": total,
            "supplemented": False,
        }
        try:
            staging_dir.rename(final_dir)
            published = True
            _remove_staging_dir(staging_dir)
            new_quotes.update_draft(
                project_id,
                draft_id,
                data=data,
                status="ready",
                expected_revision=expected_revision,
                exceptions=exceptions,
                mapping_rules=rules,
                quote_registration=quote_registration,
                publication_intent={
                    "key": publication_key,
                    "run_id": run_id,
                },
            )
        except RevisionConflict:
            new_quotes.fail_publication_intent(
                publication_key, "草稿版本冲突，未提交"
            )
            if published:
                shutil.rmtree(final_dir, ignore_errors=True)
            _remove_staging_dir(staging_dir)
            data.pop("runId", None)
            return revision_conflict()
        except Exception as exc:  # noqa: BLE001
            logger.exception("新图纸报价发布事务失败")
            registered = False
            lookup_failed = False
            try:
                registered = new_quotes.quote_exists(project_id, run_id)
            except Exception:  # noqa: BLE001
                lookup_failed = True
                logger.exception("提交结果不确定时按 runId 核对登记失败")
            if registered:
                # The commit landed; the file set is the published result and
                # must survive response/log failures.  A later retry of the
                # same intent returns this run instead of generating again.
                new_quotes.mark_publication_intent_ready(
                    publication_key, run_id
                )
                try:
                    log_action(
                        request,
                        "quote_completed",
                        project_id=project.id,
                        run_id=run_id,
                        detail={
                            "slice": "custom",
                            "total": total,
                            "draftId": draft_id,
                            "postCommitNotice": str(exc)[:200],
                        },
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("报价已提交，但完成日志写入失败")
                return RedirectResponse(
                    f"/projects/{project.id}/quotes/{run_id}",
                    status_code=303,
                )
            if lookup_failed:
                # Cannot prove the transaction failed; keep the files and the
                # runId for reconciliation instead of blindly deleting them.
                new_quotes.mark_publication_intent_uncertain(
                    publication_key, str(exc)
                )
                return publication_error(
                    "报价提交结果暂时无法核对，正式文件已保留，未执行删除。"
                    "请刷新本页核对报价记录；不要重复生成。",
                    status_code=500,
                    keep_key=publication_key,
                )
            new_quotes.fail_publication_intent(publication_key, str(exc))
            if published:
                shutil.rmtree(final_dir, ignore_errors=True)
            _remove_staging_dir(staging_dir)
            data.pop("runId", None)
            return publication_error(
                "本次报价未提交成功，草稿、映射和已有报价保持不变。"
                "未登记新报价、未保留本次生成文件，可以直接重试。",
                keep_key=publication_key,
            )
        try:
            log_action(
                request,
                "quote_completed",
                project_id=project.id,
                run_id=run_id,
                detail={
                    "slice": "custom",
                    "total": total,
                    "draftId": draft_id,
                    "secondTotal": (data.get("secondScheme") or {}).get(
                        "total"
                    ),
                },
            )
        except Exception:  # noqa: BLE001 - the committed quote stands
            logger.exception("报价已提交，但完成日志写入失败")
        return RedirectResponse(
            f"/projects/{project.id}/quotes/{run_id}", status_code=303
        )

    @app.get("/projects/{project_id}/delete", response_class=HTMLResponse)
    def delete_project_get(request: Request, project_id: str) -> str:
        project = owned_project(request, project_id)
        return ui.delete_project_page(
            _user_payload(current_user(request)), project
        )

    @app.post("/projects/{project_id}/delete")
    def delete_project_post(
        request: Request,
        project_id: str,
        confirm: str = Form(""),
    ):
        project = owned_project(request, project_id)
        if current_user(request).username != project.owner:
            raise HTTPException(
                status_code=403, detail="只有项目所有者可以删除项目"
            )
        if confirm != "yes":
            return HTMLResponse(
                ui.delete_project_page(
                    _user_payload(current_user(request)), project
                ),
                status_code=400,
            )
        log_action(
            request,
            "project_deleted",
            project_id=project.id,
            detail={
                "name": project.name,
                "quotes": len(project.quotes),
                "files": len(project.files),
            },
        )
        run_ids = projects.delete(project.owner, project_id)
        remaining = projects.all_run_ids()
        for run_id in run_ids:
            if run_id in remaining:
                continue
            try:
                run_dir = _run_dir(runs_root, run_id)
            except HTTPException:
                continue
            shutil.rmtree(run_dir, ignore_errors=True)
        project_data_dir = (
            data_root / "projects" / project.id
        ).resolve()
        if project_data_dir.is_relative_to(data_root):
            shutil.rmtree(project_data_dir, ignore_errors=True)
        try:
            db.vacuum()
        except sqlite3.Error:
            logger.exception("数据库清理失败")
        return RedirectResponse("/projects", status_code=303)

    @app.get("/projects/{project_id}/new", response_class=HTMLResponse)
    def new_quote_get(request: Request, project_id: str):
        project = owned_project(request, project_id)
        # 客户项目只有一个报价入口：新图纸报价。旧链接保留兼容。
        return RedirectResponse(
            f"/projects/{project.id}/custom", status_code=303
        )






    @app.post("/projects/{project_id}/quotes/{run_id}/delete")
    def quote_delete(request: Request, project_id: str, run_id: str):
        project, quote = owned_quote(request, project_id, run_id)
        log_action(
            request,
            "quote_deleted",
            project_id=project.id,
            run_id=run_id,
            detail={
                "title": quote.title,
                "total": quote.total,
                "status": quote.status,
            },
        )
        projects.delete_quote(project.owner, project.id, run_id)
        remaining = projects.all_run_ids()
        if run_id not in remaining:
            try:
                run_dir = _run_dir(runs_root, run_id)
            except HTTPException:
                run_dir = None
            if run_dir is not None:
                shutil.rmtree(run_dir, ignore_errors=True)
        return RedirectResponse(f"/projects/{project.id}", status_code=303)

    @app.post("/projects/{project_id}/quotes/{run_id}/include")
    def quote_include(
        request: Request,
        project_id: str,
        run_id: str,
        included: str = Form("1"),
    ):
        project, quote = owned_quote(request, project_id, run_id)
        if quote.status != "ready":
            raise HTTPException(status_code=409, detail="待补充报价不能计入汇总")
        projects.set_quote_include(
            project.owner, project.id, run_id, included == "1"
        )
        log_action(
            request,
            "quote_include_changed",
            project_id=project.id,
            run_id=run_id,
            detail={"included": included == "1"},
        )
        return RedirectResponse(
            f"/projects/{project.id}", status_code=303
        )

    @app.get(
        "/projects/{project_id}/quotes/{run_id}",
        response_class=HTMLResponse,
    )
    def quote_result(
        request: Request, project_id: str, run_id: str
    ) -> str:
        project, quote = owned_quote(request, project_id, run_id)
        if quote.slice_key != "custom":
            raise HTTPException(status_code=404, detail="报价不存在")
        run_dir = _run_dir(runs_root, run_id)
        payload = _read_quote_payload(run_dir)
        return HTMLResponse(
            ui.quote_result_page(
                _user_payload(current_user(request)),
                project,
                quote,
                payload,
                labels=_labels_for(payload),
                excluded=("未确认的设备与管线不计入报价", "图纸未表达的竖向段、预留和损耗需另行核对"),
            )
        )

    @app.get(
        "/projects/{project_id}/quotes/{run_id}/download/{filename}"
    )
    def quote_download(
        request: Request, project_id: str, run_id: str, filename: str
    ) -> FileResponse:
        project, quote = owned_quote(request, project_id, run_id)
        if filename not in CUSTOMER_DOWNLOADS:
            raise HTTPException(status_code=404, detail="文件不存在")
        run_dir = _run_dir(runs_root, run_id)
        path = _quote_file(run_dir, filename)
        if path is None:
            raise HTTPException(status_code=404, detail="文件不存在")
        log_action(
            request,
            "download",
            project_id=project_id,
            run_id=quote.run_id,
            detail={"filename": filename, "slice": quote.slice_key},
        )
        return FileResponse(
            path,
            filename=_quote_download_name(
                project.name, quote.title, quote.created_at, filename
            ),
        )





    return app
