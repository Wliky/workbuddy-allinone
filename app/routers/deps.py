"""共享依赖：登录态校验、管理员校验。"""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from .. import db, security, settings

COOKIE_NAME = "wb_session"


def current_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    session = security.read_session(token)
    if not session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效，请重新登录")
    row = db.one("SELECT id, username, role FROM users WHERE username=?", (session["username"],))
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号不存在")
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "issued": session["issued"]}


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if user["role"] != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return user


def cookie_secure(request: Request) -> bool:
    mode = settings.SECURE_COOKIE
    if mode in ("1", "true"):
        return True
    if mode in ("0", "false"):
        return False
    # auto：跟随反代透传的协议；localStorage 场景下也允许
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
