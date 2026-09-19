"""登录、登出、改密、会话续签。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .. import db, security, settings
from .deps import COOKIE_NAME, cookie_secure, current_user

router = APIRouter(prefix="/api", tags=["auth"])

# 同一 IP 连续失败锁定（内存态即可，重启清零不算问题）
_fails: dict[str, list[float]] = {}
MAX_FAILS = 8
WINDOW = 900


def _throttled(ip: str) -> bool:
    import time

    now = time.time()
    hits = [t for t in _fails.get(ip, []) if now - t < WINDOW]
    _fails[ip] = hits
    return len(hits) >= MAX_FAILS


def _note_fail(ip: str) -> None:
    import time

    _fails.setdefault(ip, []).append(time.time())


@router.post("/login")
async def login(request: Request, response: Response):
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    ip = request.client.host if request.client else "?"

    if _throttled(ip):
        raise HTTPException(429, "失败次数过多，请 15 分钟后再试")

    row = db.one("SELECT * FROM users WHERE username=?", (username,))
    if not row or not security.verify_password(password, row["pw_hash"], row["salt"]):
        _note_fail(ip)
        db.audit(username or "?", "login.failed", f"ip={ip}")
        raise HTTPException(401, "用户名或密码错误")

    token = security.make_session(row["username"], row["role"])
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=cookie_secure(request),
        path="/",
    )
    db.audit(row["username"], "login.ok", f"ip={ip}")
    return {"ok": True, "username": row["username"], "role": row["role"]}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    return {
        "username": user["username"],
        "role": user["role"],
        "version": settings.VERSION,
        "upstream_repo": settings.UPSTREAM_REPO,
    }


@router.post("/session/renew")
async def renew(request: Request, response: Response, user: dict = Depends(current_user)):
    """活跃时静默续签，实现「常用的人不被打扰、放着不动的会话自己过期」。"""
    import time

    if time.time() - user["issued"] > 3600:
        token = security.make_session(user["username"], user["role"])
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=settings.SESSION_DAYS * 86400,
            httponly=True,
            samesite="lax",
            secure=cookie_secure(request),
            path="/",
        )
        return {"renewed": True}
    return {"renewed": False}


@router.post("/password")
async def change_password(request: Request, user: dict = Depends(current_user)):
    body = await request.json()
    old = body.get("old_password") or ""
    new = body.get("new_password") or ""
    if len(new) < 8:
        raise HTTPException(400, "新密码至少 8 位")

    row = db.one("SELECT * FROM users WHERE username=?", (user["username"],))
    if not row or not security.verify_password(old, row["pw_hash"], row["salt"]):
        raise HTTPException(400, "原密码不正确")

    pw_hash, salt = security.hash_password(new)
    db.execute("UPDATE users SET pw_hash=?, salt=? WHERE id=?", (pw_hash, salt, row["id"]))
    db.audit(user["username"], "password.changed", "")
    return {"ok": True}
