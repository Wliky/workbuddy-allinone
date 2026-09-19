"""账号管理：状态、手动停用/启用/复活、凭证文件、扫码加号。"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from .. import db, oauth, settings, upstream
from ..supervisor import supervisor
from .deps import require_admin

log = logging.getLogger("workbuddy.accounts")

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _normalize(info: dict) -> dict:
    """把上游 /status 的账号条目整理成界面直接可用的形状。

    双状态位是上游的设计（issue #138/#118）：disabled 是系统判定坏了，
    manual_disabled 是运维主动摘的——两者独立、可叠加，操作也不同
    （前者该 revive，后者该 enable）。界面必须分开呈现，不能合并成一盏灯。
    """
    return {
        "uid": info.get("uid") or "",
        "realm": info.get("realm") or "cn",
        "nickname": info.get("nickname") or "",
        "credits": info.get("credits") or 0,
        "cooling": bool(info.get("cooling")),
        "cool_kind": info.get("cool_kind") or "",
        "cool_remaining_sec": info.get("cool_remaining_sec") or 0,
        "until": info.get("until") or None,
        "reason": info.get("reason") or "",
        "soft_streak": info.get("soft_streak") or 0,
        "rate_limited_models": info.get("rate_limited_models") or [],
        "model_costs": info.get("model_costs") or [],
        "disabled": bool(info.get("disabled")),
        "disabled_reason": info.get("disabled_reason") or "",
        "manual_disabled": bool(info.get("manual_disabled")),
        "manual_reason": info.get("manual_reason") or "",
        "success_count": info.get("success_count") or 0,
        "err_total": info.get("err_total") or 0,
        "last_success": info.get("last_success") or None,
        "last_err": info.get("last_err") or None,
        "consecutive_fails": info.get("consecutive_fails") or 0,
        "degrade_until": info.get("degrade_until") or None,
        "in_flight": info.get("in_flight") or 0,
        "breaker_fails": info.get("breaker_fails") or 0,
        "breaker_until": info.get("breaker_until") or None,
        # 派生状态灯。一个账号可能同时处于多种状态（例如运维手动停用 + token 正在冷却），
        # 所以这里给的是**主导原因**，优先级按「运维需要先处理哪个」排：
        #   disabled（系统判定坏了，可 revive） >
        #   manual（运维自己摘的，该 enable） >
        #   cooling（等它自己恢复） >
        #   available
        # 被折叠掉的事实不会丢：cooling / manual_disabled / disabled 三个原始字段
        # 都在同一个对象里，界面要同时展示两个徽标时直接用原始字段即可。
        "state": (
            "disabled" if info.get("disabled")
            else "manual" if info.get("manual_disabled")
            else "cooling" if info.get("cooling")
            else "available"
        ),
    }


@router.get("")
async def list_accounts(_: dict = Depends(require_admin)):
    try:
        raw = await upstream.status()
    except upstream.UpstreamError as exc:
        # 上游没起来不算致命：面板要能显示凭证文件，便于用户排查
        return {
            "upstream_error": str(exc),
            "upstream_running": supervisor.running,
            "accounts": [],
            "total": 0, "healthy": 0, "cooling": 0, "disabled": 0, "in_flight_full": 0,
            "realm_totals": {}, "sticky_sessions": 0, "redis_mode": "noop",
            "files": upstream.auth_files(),
            "admin_enabled": upstream.admin_enabled(),
            "pending_logins": oauth.pending(),
            "login_mode": settings.LOGIN_MODE,
        }
    accounts = [_normalize(a) for a in (raw.get("accounts") or [])]
    by_uid = {a["uid"]: a for a in accounts}
    files = upstream.auth_files()
    for f in files:
        f["known"] = f["uid"] in by_uid
    # 上游有账号但本地没有对应文件 → 一般是权限/属主问题，值得显式提示
    orphan = [a["uid"] for a in accounts if a["uid"] not in {f["uid"] for f in files}]
    return {
        "accounts": accounts,
        "total": raw.get("total") or 0,
        "healthy": raw.get("healthy") or 0,
        "cooling": raw.get("cooling") or 0,
        "disabled": raw.get("disabled") or 0,
        "in_flight_full": raw.get("in_flight_full") or 0,
        "realm_totals": raw.get("realm_totals") or {},
        "sticky_sessions": raw.get("sticky_sessions") or 0,
        "redis_mode": raw.get("redis_mode") or "noop",
        "cost_explore": raw.get("cost_explore") or {},
        "files": files,
        "orphan_uids": orphan,
        "admin_enabled": upstream.admin_enabled(),
        "upstream_running": supervisor.running,
        # 未完成的加号会话：页面刷新或面板重启后前端据此续上轮询。
        "pending_logins": oauth.pending(),
        "login_mode": settings.LOGIN_MODE,
    }


# 注意：下面这个通配路由**必须**注册在 /login/* 之后。
# FastAPI 按注册顺序匹配，放前面会把 POST /api/accounts/login/start
# 当成 uid=login、action=start 吃掉（实测踩过）。


@router.delete("/{uid}")
async def remove_account(uid: str, user: dict = Depends(require_admin)):
    """删除本地凭证文件。上游内存里的账号要等重启才消失，所以顺手重启一次。"""
    if not upstream.valid_uid(uid):
        raise HTTPException(400, "uid 非法")
    if not upstream.delete_auth_file(uid):
        raise HTTPException(404, "未找到该账号的凭证文件")
    db.audit(user["username"], "account.delete_file", f"uid={uid}")
    ok, detail = await supervisor.restart(reason=f"remove-account:{uid}")
    return {"ok": True, "restarted": ok, "detail": detail}


# ── 扫码加号 ───────────────────────────────────────────────────────
# 会话由 oauth 模块按「上游签发的 state」键控：同一 realm 可并发开多个加号，
# 面板重启后未完成的会话也从 DB 恢复（不必重新取链接）。
@router.get("/login/pending")
async def pending(_: dict = Depends(require_admin)):
    return {
        "pending": oauth.pending(),
        "mode": settings.LOGIN_MODE,
        "ttl_sec": settings.LOGIN_TTL,
    }


@router.get("/login/regions")
async def login_regions(_: dict = Depends(require_admin)):
    """国际版注册可选地区。

    前端只读展示用；真正提交时会拿上游返回的完整地区对象（含数字码），
    所以这里的静态表即使与上游有出入也不会导致提交错地区。
    """
    return {
        "regions": [
            {"code": c, "name": oauth.GLOBAL_REGION_NAMES.get(c, c)}
            for c in oauth.GLOBAL_REGION_WHITELIST
        ]
    }


@router.post("/login/start")
async def login_start(body: dict, user: dict = Depends(require_admin)):
    realm = "global" if str(body.get("realm") or "").strip().lower() == "global" else "cn"
    region = str(body.get("region") or "").strip().upper()
    try:
        if settings.LOGIN_MODE == "binary":
            result = await oauth.start_binary(realm)
        else:
            result = await oauth.start(realm, region=region)
    except upstream.UpstreamError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except oauth.OAuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except oauth.OAuthPending as exc:
        raise HTTPException(502, f"上游未返回可用授权链接：{exc}") from exc
    return result


@router.get("/login/poll")
async def login_poll(state: str, _: dict = Depends(require_admin)):
    """轮询一次授权结果。前端每 3 秒调一次，直到 done=true。

    完成时顺带把上游重启一把，让新凭证进池 —— 重启放在后台做，不占住这个响应，
    否则用户会盯着一个转 20 秒的请求。
    """
    try:
        result = await oauth.poll(state)
    except oauth.OAuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    if result.get("done"):
        result["restart_pending"] = True
        asyncio.create_task(_reload_pool_after_login(str(result.get("uid") or "")))
    return result


async def _reload_pool_after_login(uid: str) -> None:
    """后台重启上游把新凭证读进池。结果只落日志与审计，不回给前端。"""
    try:
        ok, detail = await supervisor.restart(reason=f"new-account:{uid or '?'}")
        db.audit("system", "account.login_reload", f"uid={uid} ok={ok} detail={detail}")
        if not ok:
            log.warning("加号后重启上游失败：%s（可在账号页点「重新加载账号池」重试）", detail)
    except Exception as exc:  # noqa: BLE001
        log.warning("加号后重启上游异常：%s", exc)


@router.post("/login/cancel")
async def login_cancel(body: dict | None = None, user: dict = Depends(require_admin)):
    state = str((body or {}).get("state") or "").strip()
    if not state:
        raise HTTPException(400, "缺少 state")
    gone = await oauth.forget(state)
    db.audit(user["username"], "account.login_cancel", f"state={state[:12]}")
    return {"ok": True, "found": gone}


@router.post("/{uid}/{action}")
async def act(uid: str, action: str, body: dict | None = None, user: dict = Depends(require_admin)):
    # 双保险：即便将来有人把这个路由挪到 /login/* 前面，也不会把加号子路由吞掉。
    if uid in ("login", "reload", "refresh-region"):
        raise HTTPException(404, "未找到该接口")
    reason = (body or {}).get("reason") or ""
    try:
        result = await upstream.account_action(uid, action, reason)
    except upstream.UpstreamError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    db.audit(user["username"], f"account.{action}", f"uid={uid} reason={reason}")
    return {"ok": True, "result": result}


@router.post("/reload")
async def reload_pool(user: dict = Depends(require_admin)):
    """重启上游以重新对齐 auths/ 目录。

    上游在启动时用 auths/ 对齐账号池，手工增删凭证文件后需要它重新加载。
    （加号/删号流程内部已经自动调用了这一步，这里只是给「我手工放了文件」留的入口。）
    """
    ok, detail = await supervisor.restart(reason="reload-pool")
    db.audit(user["username"], "upstream.reload", detail)
    return {"ok": ok, "detail": detail}


@router.post("/refresh-region")
async def refresh_region(body: dict, user: dict = Depends(require_admin)):
    """给已存在的国际版账号补做一次注册激活（幂等）。

    场景：加号时上游正好抽风、或当初没走集成面板而是手放的凭证文件 ——
    这类账号 region 没补上，chat 会一直报 14017 trial not activated。
    """
    uid = str(body.get("uid") or "").strip()
    if not upstream.valid_uid(uid):
        raise HTTPException(400, "uid 非法")
    try:
        message = await oauth.refresh_global_region(uid)
    except oauth.OAuthError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except oauth.OAuthPending as exc:
        raise HTTPException(502, f"上游未就绪：{exc}") from exc
    db.audit(user["username"], "account.refresh_region", f"uid={uid} {message}")
    return {"ok": True, "message": message or "已激活"}
