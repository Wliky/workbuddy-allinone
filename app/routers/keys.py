"""API 密钥分发：多密钥、独立配额、模型白名单、IP 白名单。

密钥只存 SHA-256 散列，明文仅在创建响应里出现一次（之后任何接口都取不回）。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException

from .. import db, security
from .deps import require_admin

router = APIRouter(prefix="/api/keys", tags=["keys"])


def _view(row) -> dict:
    now = int(time.time())
    expires_at = row["expires_at"]
    quota = row["quota_credits"] or 0
    used = row["used_credits"] or 0
    if row["disabled"]:
        state = "disabled"
    elif expires_at and expires_at < now:
        state = "expired"
    elif quota > 0 and used >= quota:
        state = "exhausted"
    else:
        state = "active"
    return {
        "id": row["id"],
        "name": row["name"],
        "prefix": row["prefix"],
        "created_at": row["created_at"],
        "expires_at": expires_at,
        "disabled": bool(row["disabled"]),
        "models": row["models"],
        "ip_allow": row["ip_allow"],
        "quota_credits": quota,
        "used_credits": round(used, 4),
        "request_count": row["request_count"],
        "last_used": row["last_used"],
        "state": state,
        # 剩余额度百分比，界面进度条直接用；0 = 不限量
        "quota_left_pct": None if quota <= 0 else max(0.0, round((quota - used) / quota * 100, 1)),
    }


@router.get("")
async def list_keys(_: dict = Depends(require_admin)):
    rows = db.query("SELECT * FROM api_keys ORDER BY id DESC")
    return {"keys": [_view(r) for r in rows]}


@router.post("")
async def create_key(body: dict, user: dict = Depends(require_admin)):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "请填写密钥名称")

    days = body.get("expires_in_days")
    expires_at = int(time.time()) + int(days) * 86400 if days else None
    models = ",".join(m.strip() for m in (body.get("models") or []) if m.strip())
    ip_allow = (body.get("ip_allow") or "").strip()
    quota = float(body.get("quota_credits") or 0)

    plain = security.new_api_key()
    kid = db.execute(
        """INSERT INTO api_keys(name, key_hash, prefix, created_at, expires_at,
                                models, ip_allow, quota_credits)
           VALUES (?,?,?,?,?,?,?,?)""",
        (name, security.key_hash(plain), security.KEY_PREFIX, int(time.time()),
         expires_at, models, ip_allow, quota),
    )
    db.audit(user["username"], "key.create", f"id={kid} name={name}")
    return {
        "ok": True,
        "id": kid,
        # 明文只在这里出现一次
        "api_key": plain,
        "warning": "这是唯一一次展示明文，请立即复制保存；服务端只存散列，无法再取回。",
    }


@router.patch("/{key_id}")
async def update_key(key_id: int, body: dict, user: dict = Depends(require_admin)):
    row = db.one("SELECT * FROM api_keys WHERE id=?", (key_id,))
    if not row:
        raise HTTPException(404, "密钥不存在")

    fields, args = [], []
    if "name" in body:
        fields.append("name=?")
        args.append((body.get("name") or "").strip() or row["name"])
    if "disabled" in body:
        fields.append("disabled=?")
        args.append(1 if body.get("disabled") else 0)
    if "models" in body:
        fields.append("models=?")
        args.append(",".join(m.strip() for m in (body.get("models") or []) if m.strip()))
    if "ip_allow" in body:
        fields.append("ip_allow=?")
        args.append((body.get("ip_allow") or "").strip())
    if "quota_credits" in body:
        fields.append("quota_credits=?")
        args.append(float(body.get("quota_credits") or 0))
    if "reset_usage" in body and body.get("reset_usage"):
        fields.append("used_credits=?")
        args.append(0.0)
    if "expires_in_days" in body:
        days = body.get("expires_in_days")
        fields.append("expires_at=?")
        args.append(int(time.time()) + int(days) * 86400 if days else None)

    if not fields:
        raise HTTPException(400, "没有需要更新的字段")
    args.append(key_id)
    db.execute(f"UPDATE api_keys SET {', '.join(fields)} WHERE id=?", args)
    db.audit(user["username"], "key.update", f"id={key_id} fields={','.join(fields)}")
    return {"ok": True, "key": _view(db.one("SELECT * FROM api_keys WHERE id=?", (key_id,)))}


@router.delete("/{key_id}")
async def delete_key(key_id: int, user: dict = Depends(require_admin)):
    if not db.one("SELECT id FROM api_keys WHERE id=?", (key_id,)):
        raise HTTPException(404, "密钥不存在")
    db.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
    db.audit(user["username"], "key.delete", f"id={key_id}")
    return {"ok": True}
