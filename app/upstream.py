"""上游网关的客户端、config.json 读写、账号凭证落盘，以及加号登录的旧路径。

加号登录有两条路径：
  · **默认（http）** —— `app/oauth.py`，在面板进程内直连上游设备授权端点，
    不依赖任何外部二进制。轮询、超时、并发都由面板自己掌握。
  · **兼容（binary）** —— 本文件下面的 `login_url` / `login_poll`，调用内置的
    `login` 命令行工具。上游那个工具本身也是非交互两步式（`url` / `poll` 两个
    子命令），login.sh 只是给它套了个 `read -p`。留这条路是因为它和上游 CLI
    的行为逐字一致，出问题时是一个可对照的基准。

两条路都写同样格式的 auth 文件（见 write_auth_file），所以下游完全无感。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

import httpx

from . import db, settings

# 会被界面打码的敏感字段（路径用点号表示嵌套）。global.chat_base /
# global.billing_base 是自建端点地址、不是密钥，照实回显。
SECRET_KEYS = {"api_key", "upstash.token", "upstash.url", "upstream.device_token"}

_realm_re = re.compile(r"^[a-z0-9_-]{2,16}$")

# uid 会被用来拼文件名（auths/workbuddy-<uid>.json）与 URL 路径段。
# 它来自上游响应 / 用户传参，属于不可信输入：只放行 [A-Za-z0-9_-]。
# 不校验的话 `uid=../x` 这类值会让写入逃出 auths/ 目录。
# （腾讯侧 uid 实测为 UUID 形态，这个字符集足够宽。）
UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def valid_uid(uid: str) -> bool:
    return bool(UID_RE.match(uid or ""))


# ──────────────────────────────────────────────────────────────────
# config.json
# ──────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if not settings.UPSTREAM_CONFIG.exists():
        if settings.UPSTREAM_CONFIG_EXAMPLE.exists():
            return json.loads(settings.UPSTREAM_CONFIG_EXAMPLE.read_text(encoding="utf-8"))
        return {}
    try:
        return json.loads(settings.UPSTREAM_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    """原子写：tmp + os.replace，避免上游读到一个半截文件。"""
    p = settings.UPSTREAM_CONFIG
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def ensure_config() -> str:
    """首启补齐 config.json：example 落盘 + 生成内部密钥。返回一行说明。"""
    notes = []
    if not settings.UPSTREAM_CONFIG.exists():
        if settings.UPSTREAM_CONFIG_EXAMPLE.exists():
            save_config(json.loads(settings.UPSTREAM_CONFIG_EXAMPLE.read_text(encoding="utf-8")))
            notes.append("已从 config.example.json 生成 config.json")
        else:
            save_config({"listen": ":7863", "api_key": "", "auth_dir": "./auths",
                         "state_file": "./data/state.json", "admin": {"enabled": False}})
            notes.append("config.example.json 缺失，已写入最小可用配置")

    cfg = load_config()
    if not (cfg.get("api_key") or "").strip():
        # 上游端口只监听容器回环，但仍给它一把随机钥匙：万一将来误暴露端口，
        # 至少不是「空密钥 = 不鉴权」。
        cfg["api_key"] = secrets.token_urlsafe(32)
        save_config(cfg)
        notes.append("已为上游生成内部 API 密钥并写回 config.json")
    return "；".join(notes)


def internal_key() -> str:
    return (load_config().get("api_key") or "").strip()


def auth_headers() -> dict:
    key = internal_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def admin_enabled() -> bool:
    return bool((load_config().get("admin") or {}).get("enabled"))


def config_view() -> dict:
    """给界面看的安全视图：敏感值打码，但仍告知「已设置」。"""
    cfg = load_config()
    out = json.loads(json.dumps(cfg))
    for path in SECRET_KEYS:
        parts = path.split(".")
        node = out
        for part in parts[:-1]:
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if isinstance(node, dict) and parts[-1] in node:
            val = node[parts[-1]]
            node[parts[-1]] = "__SET__" if val else ""
    return {
        "config": out,
        "raw_keys": sorted(_flatten_keys(cfg)),
        "admin_enabled": admin_enabled(),
        "path": str(settings.UPSTREAM_CONFIG),
    }


def _flatten_keys(obj: Any, prefix: str = "") -> list[str]:
    keys: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.append(f"{prefix}{k}")
            keys.extend(_flatten_keys(v, f"{prefix}{k}."))
    return keys


def merge_config(patch: dict) -> dict:
    """浅合并（一层深）。只覆盖调用方给出的键，其余原样保留。"""
    cfg = load_config()
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    save_config(cfg)
    return cfg


# ──────────────────────────────────────────────────────────────────
# 上游 HTTP 客户端
# ──────────────────────────────────────────────────────────────────
def _client(timeout: float | None = None) -> httpx.AsyncClient:
    # trust_env=False：容器里若设了 HTTP_PROXY，127.0.0.1 的请求不该被代理劫持
    return httpx.AsyncClient(timeout=timeout or 20.0, trust_env=False)


class UpstreamError(RuntimeError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


async def _request(method: str, path: str, **kw) -> httpx.Response:
    url = f"{settings.UPSTREAM_BASE}{path}"
    headers = dict(auth_headers())
    headers.update(kw.pop("headers", {}) or {})
    try:
        async with _client(kw.pop("timeout", None)) as client:
            return await client.request(method, url, headers=headers, **kw)
    except httpx.ConnectError as exc:
        raise UpstreamError("上游网关未运行或未就绪（连接被拒绝）", 503) from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(f"访问上游失败：{exc}", 502) from exc


async def healthz() -> dict:
    r = await _request("GET", "/healthz", timeout=5.0)
    try:
        return {"status": r.status_code, **(r.json() or {})}
    except ValueError:
        return {"status": r.status_code}


async def status() -> dict:
    r = await _request("GET", "/status", timeout=15.0)
    if r.status_code != 200:
        raise UpstreamError(_err_text(r), r.status_code)
    return r.json() or {}


async def models() -> dict:
    r = await _request("GET", "/v1/models", timeout=30.0)
    if r.status_code != 200:
        raise UpstreamError(_err_text(r), r.status_code)
    return r.json() or {}


async def stats() -> dict:
    r = await _request("GET", "/v1/stats", timeout=15.0)
    if r.status_code != 200:
        raise UpstreamError(_err_text(r), r.status_code)
    return r.json() or {}


async def stats_reset() -> dict:
    r = await _request("POST", "/v1/stats/reset", timeout=15.0)
    return r.json() if r.status_code == 200 else {"error": _err_text(r)}


async def account_action(uid: str, action: str, reason: str = "") -> dict:
    if action not in ("disable", "enable", "revive"):
        raise UpstreamError(f"不支持的操作：{action}", 400)
    if not valid_uid(uid):
        raise UpstreamError("uid 非法", 400)
    if not admin_enabled() and action in ("disable", "enable", "revive"):
        raise UpstreamError(
            "上游 admin 接口未开启：请在「系统设置」里把 admin.enabled 设为 true "
            "并保存（会自动重启上游）。",
            409,
        )
    body = {"reason": reason} if action == "disable" and reason else {}
    r = await _request("POST", f"/admin/accounts/{uid}/{action}", json=body, timeout=20.0)
    if r.status_code not in (200, 201):
        raise UpstreamError(_err_text(r), r.status_code)
    try:
        return r.json() or {}
    except ValueError:
        return {"ok": True}


def _err_text(r: httpx.Response) -> str:
    try:
        body = r.json()
        if isinstance(body, dict):
            msg = body.get("error") or body.get("message") or body.get("msg")
            if msg:
                return str(msg)
        return json.dumps(body, ensure_ascii=False)[:300]
    except ValueError:
        return (r.text or "").strip()[:300] or f"HTTP {r.status_code}"


# ──────────────────────────────────────────────────────────────────
# 账号凭证（auths/）
# ──────────────────────────────────────────────────────────────────
def auth_files() -> list[dict]:
    out = []
    if not settings.AUTH_DIR.exists():
        return out
    for p in sorted(settings.AUTH_DIR.glob("workbuddy-*.json")):
        item = {"file": p.name, "uid": p.stem.replace("workbuddy-", ""), "size": p.stat().st_size,
                "mtime": int(p.stat().st_mtime), "realm": "", "nickname": "", "expires_at": None}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            item["uid"] = str((data.get("account") or {}).get("uid") or item["uid"])
            item["nickname"] = (data.get("account") or {}).get("nickname") or ""
            item["realm"] = (data.get("auth") or {}).get("realm") or ""
            item["expires_at"] = (data.get("auth") or {}).get("expiresAt")
        except (OSError, ValueError):
            item["broken"] = True
        out.append(item)
    return out


def write_auth_file(payload: dict) -> dict:
    """按上游 internal/auth 的格式落盘（嵌套形，与 login.sh / 面板参考实现一致）。

    uid 只接受 [A-Za-z0-9_-]：拼文件名前必须先校验，否则上游返回一个带 `/` 的 uid
    就能把文件写到 auths/ 之外。
    """
    account = payload.get("account") or {}
    auth = payload.get("auth") or {}
    uid = str(account.get("uid") or "").strip()
    if not uid:
        raise ValueError("OAuth 结果缺少 uid，无法落盘")
    if not valid_uid(uid):
        raise ValueError(f"上游返回的 uid 含非法字符（{uid[:40]}），拒绝落盘以防路径穿越")

    realm = str(auth.get("realm") or "").strip().lower()
    if realm not in ("cn", "global"):
        realm = "cn"

    settings.AUTH_DIR.mkdir(parents=True, exist_ok=True)
    target = settings.AUTH_DIR / f"workbuddy-{uid}.json"
    existed = target.exists()

    record = {
        "account": {
            "uid": uid,
            "enterpriseId": account.get("enterpriseId") or "",
            "nickname": account.get("nickname") or "",
        },
        "auth": {
            "accessToken": auth.get("accessToken") or "",
            "refreshToken": auth.get("refreshToken") or "",
            "expiresAt": int(auth.get("expiresAt") or 0),
            "domain": auth.get("domain") or "",
            "realm": realm,
        },
    }

    # device_token 只对设备有效、跟账号无关，重新登录同一账号时不该把它抹掉。
    # 调用方没给就沿用文件里已有的那一份。
    device_token = str(payload.get("device_token") or "").strip()
    if not device_token and existed:
        with contextlib.suppress(OSError, ValueError):
            old = json.loads(target.read_text(encoding="utf-8"))
            device_token = str(old.get("device_token") or "").strip()
    if device_token:
        record["device_token"] = device_token

    tmp = settings.AUTH_DIR / f".tmp-{secrets.token_hex(6)}.json"
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, target)
    return {"uid": uid, "file": target.name, "updated": existed}


def read_auth_tokens(uid: str) -> tuple[str, str, str]:
    """读回某账号的 (accessToken, refreshToken, domain)；文件不存在或坏了返回空串。"""
    if not valid_uid(uid):
        return "", "", ""
    p = settings.AUTH_DIR / f"workbuddy-{uid}.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", "", ""
    if isinstance(data.get("auth"), dict):
        a = data["auth"]
    else:
        a = data if isinstance(data, dict) else {}
    return (str(a.get("accessToken") or ""), str(a.get("refreshToken") or ""),
            str(a.get("domain") or ""))


def delete_auth_file(uid: str) -> bool:
    if not valid_uid(uid):
        return False
    p = settings.AUTH_DIR / f"workbuddy-{uid}.json"
    if p.exists():
        p.unlink()
        return True
    return False


# ──────────────────────────────────────────────────────────────────
# OAuth 加号（复用上游 login 二进制，非交互两步式）
# ──────────────────────────────────────────────────────────────────
async def _run_login(subcmd: str, realm: str, timeout: float) -> tuple[int, str, str]:
    if not settings.LOGIN_BIN.exists():
        raise UpstreamError(f"缺少登录工具 {settings.LOGIN_BIN}（镜像构建时未内置 login 二进制）", 500)
    if not _realm_re.match(realm):
        raise UpstreamError("realm 取值非法", 400)
    proc = await asyncio.create_subprocess_exec(
        str(settings.LOGIN_BIN), f"--realm={realm}", subcmd,
        cwd=str(settings.UPSTREAM_DIR),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise UpstreamError(f"登录工具 {subcmd} 超时（{timeout:.0f}s）", 504) from None
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def login_url(realm: str = "cn") -> dict:
    code, out, err = await _run_login("url", realm, 30.0)
    url = ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("http://") or line.startswith("https://"):
            url = line
            break
    if code != 0 or not url:
        raise UpstreamError(f"获取授权链接失败：{(err or out).strip()[:300]}", 502)
    return {"realm": realm, "url": url}


async def login_poll(realm: str = "cn") -> dict:
    code, out, err = await _run_login("poll", realm, 90.0)
    if code != 0:
        raise UpstreamError(
            "轮询登录结果失败——通常是在授权完成前就发起了轮询。"
            f"请先在浏览器完成登录再重试。原始输出：{(err or out).strip()[:200]}",
            502,
        )
    payload = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    if not payload:
        raise UpstreamError(f"登录工具未返回可解析结果：{out.strip()[:300]}", 502)

    expires_in = int(payload.get("expires_in") or 0)
    record = {
        "account": {
            "uid": payload.get("uid") or "",
            "enterpriseId": payload.get("enterprise_id") or "",
            "nickname": payload.get("nickname") or "",
        },
        "auth": {
            "accessToken": payload.get("access_token") or "",
            "refreshToken": payload.get("refresh_token") or "",
            "expiresAt": int(time.time()) + expires_in if expires_in else 0,
            "domain": payload.get("domain") or "",
            "realm": payload.get("realm") or realm,
        },
    }
    try:
        saved = write_auth_file(record)
    except ValueError as exc:
        raise UpstreamError(str(exc), 502) from exc
    db.audit("admin", "account.login_done", f"uid={saved['uid']} realm={realm}")

    # 上游的 login.sh 在落盘后会对国内版做一次首次签到。面板直接调 login 二进制
    # 就跳过了那一步，这里补回来（幂等，已签到会返回业务错误，不影响加号结果）。
    # 国际版不走签到，走注册激活 + trial —— 与 http 路径保持同一套收尾语义。
    checkin = None
    if realm == "global":
        from . import oauth  # 延迟导入：oauth 依赖本模块，顶层导入会成环
        checkin = {"message": await oauth.activate_global(
            record["auth"]["accessToken"], record["account"]["uid"])}
    elif record["auth"]["accessToken"]:
        checkin = await daily_checkin(
            record["auth"]["accessToken"],
            record["account"]["uid"],
            record["account"]["enterpriseId"],
            record["auth"]["domain"],
        )
    else:
        checkin = {"ok": False, "message": "未拿到 accessToken，跳过签到"}

    return {**saved, "nickname": record["account"]["nickname"], "realm": realm, "checkin": checkin}


async def daily_checkin(token: str, uid: str, enterprise_id: str = "", domain: str = "") -> dict:
    """国内版每日签到。端点与请求头取自上游 login.sh 的实测实现。

    注意：国际版（global）不走这里 —— 国际版没有 CN 这套签到体系，
    它的收尾是「注册激活 + trial」，见 oauth.activate_global。
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-User-Id": uid,
    }
    if enterprise_id:
        headers["X-Enterprise-Id"] = enterprise_id
        headers["X-Tenant-Id"] = enterprise_id
    if domain:
        headers["X-Domain"] = domain

    try:
        async with _client(20.0) as client:
            r = await client.post(
                f"{settings.OAUTH_CN_ORIGIN}/v2/billing/meter/daily-checkin",
                json={},
                headers=headers,
            )
        if r.status_code == 200:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if body.get("code") == 0:
                return {"ok": True, "message": "签到成功", "data": body.get("data")}
            return {"ok": True, "message": body.get("msg") or "签到已处理"}
        # 已签到等业务错误也走 4xx（上游实测 code=10001 "今天已签到"）
        try:
            body = r.json()
            return {"ok": True, "message": body.get("msg") or f"http {r.status_code}"}
        except ValueError:
            return {"ok": True, "message": f"http {r.status_code}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "message": f"签到请求失败：{exc}"}


def pending_logins() -> list[dict]:
    """待授权的加号会话。

    实现已迁到 oauth.py（会话按 state 键控、可并发、可持久化到 DB），
    这里只保留一个转发入口，避免调用方到处 import oauth。
    """
    from . import oauth  # 延迟导入：避免 oauth ↔ upstream 顶层循环导入
    return oauth.pending()
