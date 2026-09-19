"""口令散列、会话 Cookie 签名、API 密钥生成与校验。

只用标准库：armv7 这类小设备上少一个依赖就少一份装不上的风险（armv7 轮子稀缺）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import time

from . import settings

KEY_PREFIX = "wbk-"


# ── 口令 ───────────────────────────────────────────────────────────
def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), settings.PBKDF2_ITERATIONS
    )
    return dk.hex(), salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    got, _ = hash_password(password, salt)
    return hmac.compare_digest(got, stored_hash)


# ── 会话 ───────────────────────────────────────────────────────────
def _sign(payload: str) -> str:
    return hmac.new(
        settings.session_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def make_session(username: str, role: str) -> str:
    exp = int(time.time()) + settings.SESSION_DAYS * 86400
    payload = f"{username}|{role}|{exp}|{int(time.time())}"
    raw = f"{payload}|{_sign(payload)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def read_session(token: str) -> dict | None:
    """返回 {username, role, exp, issued} 或 None。签名不对/过期都算无效。"""
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except Exception:  # noqa: BLE001
        return None
    parts = raw.split("|")
    if len(parts) != 5:
        return None
    username, role, exp_s, issued_s, sig = parts
    payload = "|".join(parts[:4])
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        exp, issued = int(exp_s), int(issued_s)
    except ValueError:
        return None
    now = int(time.time())
    if now > exp:
        return None
    # 空闲判定用 cookie 自身的签发时刻：客户端每次带 cookie 就是「还在用」，
    # 前端会在活跃时静默续签（见 /api/session/renew），因此这里只需保证
    # 超过空闲窗口的旧 cookie 立刻失效。
    idle = settings.SESSION_IDLE_HOURS * 3600
    if idle > 0 and now - issued > idle:
        return None
    return {"username": username, "role": role, "exp": exp, "issued": issued}


# ── API 密钥 ───────────────────────────────────────────────────────
def new_api_key() -> str:
    return KEY_PREFIX + secrets.token_hex(24)


def key_hash(api_key: str) -> str:
    """密钥只存散列；明文仅在创建时返回一次。"""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def key_display(key_id: int, api_key: str) -> str:
    return f"{KEY_PREFIX}{key_id}-…{api_key[-4:]}"


# ── 入站 IP 管控 ───────────────────────────────────────────────────
def ip_allowed(ip: str, allow_list: str) -> bool:
    """allow_list 为逗号分隔的 IP / CIDR，空串 = 不限制。"""
    allow_list = (allow_list or "").strip()
    if not allow_list:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in allow_list.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            if addr in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            continue
    return False


def client_ip(request) -> str:
    if settings.TRUST_PROXY:
        for header in ("x-real-ip", "x-forwarded-for"):
            v = request.headers.get(header)
            if v:
                return v.split(",")[0].strip()
    return request.client.host if request.client else ""
