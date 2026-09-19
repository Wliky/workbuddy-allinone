"""系统设置与运行时状态：config.json 编辑、上游进程控制、日志、审计、环境信息。"""
from __future__ import annotations

import platform
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from .. import db, settings, upstream
from ..supervisor import disk_usage, supervisor
from .deps import require_admin

router = APIRouter(prefix="/api", tags=["system"])


def _meminfo() -> dict:
    """玩客云上「还剩多少内存」是最常问的问题，直接读 /proc。"""
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            if k in ("MemTotal", "MemAvailable", "SwapTotal"):
                out[k.lower()] = int(v.strip().split()[0]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return out


@router.get("/system/about")
async def about(_: dict = Depends(require_admin)):
    return {
        "manager_version": settings.VERSION,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": __import__("os").cpu_count(),
        "memory_mb": _meminfo(),
        "disk": disk_usage(),
        "upstream_repo": settings.UPSTREAM_REPO,
        "upstream_ref": settings.UPSTREAM_REF,
        "upstream_commit": settings.UPSTREAM_COMMIT,
        "paths": {
            "data": str(settings.DATA_DIR),
            "upstream": str(settings.UPSTREAM_DIR),
            "auths": str(settings.AUTH_DIR),
            "config": str(settings.UPSTREAM_CONFIG),
        },
        "uptime_sec": int(time.time() - _START_TS),
        "install_mode": "all-in-one",
    }


_START_TS = time.time()


@router.get("/system/health")
async def health(_: dict = Depends(require_admin)):
    up = await supervisor.health()
    try:
        up_status = await upstream.status()
        accounts = {
            "total": up_status.get("total") or 0,
            "healthy": up_status.get("healthy") or 0,
            "cooling": up_status.get("cooling") or 0,
            "disabled": up_status.get("disabled") or 0,
        }
    except upstream.UpstreamError:
        accounts = {"total": 0, "healthy": 0, "cooling": 0, "disabled": 0}
    return {"upstream": up, "accounts": accounts, "admin_enabled": upstream.admin_enabled()}


@router.get("/system/upstream/logs")
async def upstream_logs(limit: int = 200, source: str = "memory", _: dict = Depends(require_admin)):
    limit = max(1, min(limit, settings.UPSTREAM_LOG_LINES))
    lines = supervisor.tail_logs(limit) if source == "memory" else supervisor.log_file_tail(limit)
    return {"lines": lines, "source": source, "status": supervisor.status()}


@router.post("/system/upstream/{action}")
async def control(action: str, _: dict = Depends(require_admin)):
    if action == "start":
        ok, detail = await supervisor.start(reason="panel")
    elif action == "stop":
        ok, detail = await supervisor.stop(reason="panel")
    elif action == "restart":
        ok, detail = await supervisor.restart(reason="panel")
    else:
        raise HTTPException(400, f"不支持的操作：{action}")
    return {"ok": ok, "detail": detail, "status": supervisor.status()}


@router.get("/system/audit")
async def audit_log(limit: int = 100, _: dict = Depends(require_admin)):
    rows = db.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))
    return {"audit": db.rows_to_dicts(rows)}


# ── 上游 config.json ───────────────────────────────────────────────
@router.get("/settings")
async def get_settings(_: dict = Depends(require_admin)):
    view = upstream.config_view()
    view["admin_enabled"] = upstream.admin_enabled()
    view["upstream"] = supervisor.status()
    return view


@router.put("/settings")
async def put_settings(body: dict, user: dict = Depends(require_admin)):
    """只写用户提交的键（前端做「输入即校验、只提交改动项」）。

    patch 里出现 "__SET__" 表示「这一项没改」，直接跳过 —— 否则界面会把
    打码后的占位符当成新密钥写回去，把真密钥抹掉。
    """
    patch = body.get("patch") or {}
    restart = bool(body.get("restart", True))
    if not isinstance(patch, dict) or not patch:
        raise HTTPException(400, "没有需要保存的改动")

    clean = _strip_placeholders(patch)
    if not clean:
        return {"ok": True, "changed": [], "note": "没有实际改动（敏感项保持原值）"}

    if "admin" in clean and isinstance(clean["admin"], dict):
        clean["admin"]["enabled"] = bool(clean["admin"].get("enabled"))

    cfg = upstream.merge_config(clean)
    changed = list(clean.keys())
    db.audit(user["username"], "settings.save", ",".join(changed))

    result = {"ok": True, "changed": changed}
    if restart:
        ok, detail = await supervisor.restart(reason="settings-changed")
        result["restarted"] = ok
        result["restart_detail"] = detail
    result["admin_enabled"] = upstream.admin_enabled()
    if not upstream.admin_enabled():
        result["hint"] = "admin.enabled 为 false 时，账号的停用/启用/复活按钮不可用。"
    return result


def _strip_placeholders(node):
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if v == "__SET__":
                continue
            out[k] = _strip_placeholders(v)
        return out
    return node


@router.get("/settings/schedule-hint")
async def schedule_hint(_: dict = Depends(require_admin)):
    """把 schedule 里的「小时数组」翻译成人话，省得用户对着 [9,21] 猜。"""
    cfg = upstream.load_config()
    sch = cfg.get("schedule") or {}
    tasks = {
        "checkin": "签到",
        "travel": "旅行",
        "activity": "活跃",
        "keepalive": "保活",
        "school": "校园",
        "cat": "养猫",
    }
    rows = []
    for key, label in tasks.items():
        hours = sch.get(f"{key}_hours") or []
        rows.append({
            "key": key,
            "label": label,
            "enabled": bool(sch.get(f"{key}_enabled")),
            "hours": hours,
            "text": ("关闭" if not sch.get(f"{key}_enabled")
                     else ("、".join(f"{h:02d}:00" for h in hours) if hours else "未设置时刻")),
        })
    return {"tasks": rows}
