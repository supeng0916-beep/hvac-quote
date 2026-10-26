"""使用日志：只记录操作事实，不记录口令、文件内容或客户图纸正文。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .db import Database, decode_json


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class AuditEntry:
    id: int
    created_at: str
    username: str | None
    action: str
    project_id: str | None
    run_id: str | None
    detail: dict
    client_ip: str | None


ACTION_LABELS = {
    "setup": "创建管理员",
    "login_success": "登录成功",
    "login_failed": "登录失败",
    "logout": "退出登录",
    "password_changed": "修改密码",
    "password_reset": "重置密码",
    "user_created": "新增账号",
    "project_created": "创建项目",
    "project_member_added": "添加项目成员",
    "project_member_removed": "移除项目成员",
    "customer_created": "创建客户",
    "customer_contact_added": "新增客户联系人",
    "contract_created": "创建合同",
    "contract_updated": "修改合同",
    "contract_deleted": "删除合同草稿",
    "project_deleted": "删除项目",
    "file_uploaded": "上传资料",
    "file_deleted": "删除资料",
    "quote_started": "开始报价",
    "quote_completed": "生成报价",
    "supplement_submitted": "提交补充",
    "download": "下载文件",
    "drawing_inspected": "图纸检查",
    "template_imported": "模板导入",
    "batch_run": "批量检查/导入",
    "project_price_added": "添加项目价行",
    "price_book_row_updated": "修改价格行",
    "price_book_row_deleted": "删除价格行",
    "price_book_deleted": "删除价格书",
    "draft_copied": "复制草稿",
    "draft_deleted": "删除草稿",
    "quote_deleted": "删除报价",
    "template_mapping_deleted": "删除客户模板映射",
    "draft_created": "创建报价草稿",
    "draft_saved": "保存报价草稿",
    "draft_multipliers": "调整代表层数",
    "quote_include_changed": "修改项目汇总",
    "copper_price_recorded": "记录铜管参考价",
    "copper_price_confirmed": "确认铜管参考价",
    "copper_price_deleted": "删除铜管参考价",
}


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def log(
        self,
        action: str,
        *,
        username: str | None = None,
        project_id: str | None = None,
        run_id: str | None = None,
        detail: dict[str, Any] | None = None,
        client_ip: str | None = None,
    ) -> None:
        safe_detail = detail or {}
        self.db.execute(
            "INSERT INTO audit_logs(created_at, username, action, project_id, "
            "run_id, detail_json, client_ip) VALUES(?,?,?,?,?,?,?)",
            (
                _now_text(),
                username,
                action,
                project_id,
                run_id,
                json.dumps(safe_detail, ensure_ascii=False, separators=(",", ":"))
                if safe_detail
                else None,
                client_ip,
            ),
        )

    def list_recent(
        self,
        *,
        limit: int = 200,
        project_id: str | None = None,
        username: str | None = None,
    ) -> tuple[AuditEntry, ...]:
        where = []
        params: list[Any] = []
        if project_id:
            where.append("project_id = ?")
            params.append(project_id)
        if username:
            where.append("username = ?")
            params.append(username)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(max(1, min(int(limit), 1000)))
        rows = self.db.query(
            f"SELECT * FROM audit_logs {clause} ORDER BY id DESC LIMIT ?",
            tuple(params),
        )
        return tuple(
            AuditEntry(
                id=int(row["id"]),
                created_at=str(row["created_at"]),
                username=row["username"],
                action=str(row["action"]),
                project_id=row["project_id"],
                run_id=row["run_id"],
                detail=decode_json(row["detail_json"], {}),
                client_ip=row["client_ip"],
            )
            for row in rows
        )
