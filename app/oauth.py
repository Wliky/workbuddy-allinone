"""WorkBuddy 账号加号（OAuth 设备授权）—— 进程内纯 HTTP 实现。

与「调用上游 `login` 二进制」的旧路径相比，这条路把三步 HTTP 直接跑在面板进程里：

    1. POST {base}/v2/plugin/auth/state?platform=CLI   → {state, authUrl}
    2. GET  {base}/v2/plugin/auth/token?state=<state>  → pending 时业务码非 0
                                                       完成时 {accessToken, refreshToken, expiresIn, domain}
    3. GET  {base}/v2/plugin/login/account?state=<state>（带 Bearer）→ {uid, enterpriseId, nickname}

线路协议（端点、请求头、信封语义）取自上游 `cmd/login/main.go` 的实测实现，
逐字对齐，没有自行推断的部分。收益是实打实的三条：

  · 不再依赖 `login` 二进制（少一个必须打进镜像的产物，也少一处 fork 失败面）；
  · 轮询在面板里做，所以能做到「每 3 秒自动检测授权完成」，而不是让用户手点确认；
  · 能拿到 state，于是待授权会话可持久化、可超时、可并发（同一 realm 可同时开多个号）。

realm 语义：cn → copilot.tencent.com（Origin: codebuddy.cn）；
global → workbuddy.ai（base 与 Origin 同域）。缺省/非法一律 cn。

落盘后的收尾动作按 realm 分叉（与上游 login.sh 一致）：
  · cn     → 每日签到 POST codebuddy.cn/v2/billing/meter/daily-checkin（幂等）
  · global → 注册激活（缺地区则自动补香港）+ trial 加油包（幂等码 14051）

所有收尾动作都是「尽力而为」：失败只体现在返回文案里，不影响账号已登录的事实。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from . import db, settings, upstream

# ── 协议常量（与上游 cmd/login 逐字对齐）────────────────────────────
CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2"
# 国际版注册链路走 web 指纹（不是 CLI 指纹）—— 逆自 web 注册完善页。
GLOBAL_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

STATE_PATH = "/v2/plugin/auth/state?platform=CLI"
TOKEN_PATH = "/v2/plugin/auth/token?state="
ACCOUNT_PATH = "/v2/plugin/login/account?state="

# 国际版注册链路（与 global base 同域）
REGISTER_PATH = "/auth/realms/copilot/overseas/user/register?userId="
SUBMIT_REGION_PATH = "/console/login/account"
COUNTRY_CODE_PATH = "/billing/area/get-country-code"
TRIAL_PATH = "/billing/ide/trial"

# 国际版注册可选地区。顺序即默认优先级（首个 = 不指定时自动补的地区）。
# 对齐国际版 web 的展示集，也与参考实现保持一致。
GLOBAL_REGION_WHITELIST = ("HK", "MO", "SG", "TH", "PH", "MY", "ID")
GLOBAL_REGION_NAMES = {
    "HK": "Hong Kong", "MO": "Macao", "SG": "Singapore", "TH": "Thailand",
    "PH": "Philippines", "MY": "Malaysia", "ID": "Indonesia",
}

# state / uid 都是「上游返回、又被我们用来拼 URL 或文件名」的值。
# 上游实测给的是 UUID 形态，但这里不做信任：只放行安全字符集。
_STATE_RE = re.compile(r"^[A-Za-z0-9._~:/+=%$-]{4,300}$")
UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 待授权会话在 DB 里的键（面板重启后仍能看到并继续轮询）。
_SESSION_DB_KEY = "oauth_login_sessions"


class OAuthError(RuntimeError):
    """终止性错误：会话失效 / 上游拒绝 / 返回结构异常。"""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class OAuthPending(RuntimeError):
    """「还没好」—— 轮询期正常现象，前端应继续等。"""

    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


@dataclass
class LoginSession:
    state: str
    realm: str
    url: str
    created: float
    region: str = ""
    polls: int = 0
    last_message: str = ""
    # "http" = 本模块直连上游；"binary" = 走内置 login 工具（见 settings.LOGIN_MODE）。
    mode: str = "http"

    def to_row(self) -> dict:
        return {
            "state": self.state, "realm": self.realm, "url": self.url,
            "created": self.created, "region": self.region,
            "polls": self.polls, "last_message": self.last_message,
            "mode": self.mode,
        }

    @classmethod
    def from_row(cls, row: dict) -> "LoginSession":
        mode = str(row.get("mode") or "http")
        return cls(
            state=str(row.get("state") or ""),
            realm=str(row.get("realm") or "cn"),
            url=str(row.get("url") or ""),
            created=float(row.get("created") or 0),
            region=str(row.get("region") or ""),
            polls=int(row.get("polls") or 0),
            last_message=str(row.get("last_message") or ""),
            mode="binary" if mode == "binary" else "http",
        )


_sessions: dict[str, LoginSession] = {}
_lock = asyncio.Lock()
_loaded = False


# ──────────────────────────────────────────────────────────────────
# 会话存取
# ──────────────────────────────────────────────────────────────────
def _prune_locked(now: float) -> None:
    """清掉超时会话（调用方持锁）。"""
    for state, sess in list(_sessions.items()):
        if now - sess.created > settings.LOGIN_TTL:
            _sessions.pop(state, None)


def _persist() -> None:
    with contextlib.suppress(Exception):
        db.meta_set_json(_SESSION_DB_KEY, [s.to_row() for s in _sessions.values()])


async def load_sessions() -> None:
    """从 DB 恢复未完成的授权会话（面板重启后用户不必重新取链接）。"""
    global _loaded
    async with _lock:
        if _loaded:
            return
        _loaded = True
        rows = db.meta_get_json(_SESSION_DB_KEY, []) or []
        now = time.time()
        for row in rows:
            if not isinstance(row, dict):
                continue
            sess = LoginSession.from_row(row)
            if sess.state and now - sess.created <= settings.LOGIN_TTL:
                _sessions[sess.state] = sess
        _prune_locked(now)
    _persist()


def pending() -> list[dict]:
    now = time.time()
    out = []
    for sess in _sessions.values():
        if now - sess.created > settings.LOGIN_TTL:
            continue
        out.append({
            "state": sess.state, "realm": sess.realm, "url": sess.url,
            "created": int(sess.created), "age_sec": int(now - sess.created),
            "region": sess.region, "last_message": sess.last_message,
        })
    return sorted(out, key=lambda x: x["created"], reverse=True)


async def forget(state: str) -> bool:
    async with _lock:
        gone = _sessions.pop(state, None) is not None
    if gone:
        _persist()
    return gone


# ──────────────────────────────────────────────────────────────────
# HTTP 底座
# ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Endpoints:
    base: str
    origin: str


def endpoints(realm: str) -> Endpoints:
    if realm == "global":
        return Endpoints(settings.OAUTH_GLOBAL_BASE, settings.OAUTH_GLOBAL_ORIGIN)
    return Endpoints(settings.OAUTH_CN_BASE, settings.OAUTH_CN_ORIGIN)


_shared_client: httpx.AsyncClient | None = None


def _new_client() -> httpx.AsyncClient:
    # trust_env=False：容器里若设了 HTTP_PROXY，不该把 tencent.com 的登录请求劫走。
    # follow_redirects=True：与 Go http.Client 的默认行为一致（上游 CLI 也跟随跳转）。
    return httpx.AsyncClient(
        timeout=settings.OAUTH_TIMEOUT, trust_env=False, follow_redirects=True
    )


@contextlib.asynccontextmanager
async def _client():
    """默认每次开一个短连接；只有显式开启 cookie jar 时才复用长连接。"""
    global _shared_client
    if not settings.OAUTH_COOKIE_JAR:
        client = _new_client()
        try:
            yield client
        finally:
            await client.aclose()
        return
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = _new_client()
    yield _shared_client


async def aclose() -> None:
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None


def _headers(origin: str, bearer: str = "", ua: str = CLIENT_UA,
             extra: dict | None = None) -> dict:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": origin + "/",
        "User-Agent": ua,
    }
    if bearer:
        h["Authorization"] = "Bearer " + bearer
    if extra:
        h.update(extra)
    return h


async def _raw(method: str, url: str, headers: dict, json_body: Any = None) -> httpx.Response:
    try:
        async with _client() as client:
            return await client.request(method, url, headers=headers, json=json_body)
    except httpx.HTTPError as exc:
        raise OAuthPending(f"网络错误：{exc}") from exc


async def _data(method: str, url: str, *, origin: str, bearer: str = "",
                json_body: Any = None, ua: str = CLIENT_UA,
                extra: dict | None = None) -> Any:
    """取信封里的 data。业务码非 0 一律视为「还没好」（轮询期的主路径）。

    上游 `auth/token` 在用户还没授权时就是这么返回的：HTTP 200 + code 非 0 + msg≈"login ing"。
    """
    resp = await _raw(method, url, _headers(origin, bearer, ua, extra), json_body)
    if resp.status_code >= 300:
        raise OAuthPending(f"上游 HTTP {resp.status_code}")
    try:
        env = resp.json()
    except ValueError as exc:
        raise OAuthPending("上游返回的不是 JSON") from exc
    if not isinstance(env, dict):
        raise OAuthPending("上游返回结构异常")
    code = env.get("code")
    if code not in (0, None):
        raise OAuthPending(str(env.get("msg") or f"code={code}"), code=int(code or 0))
    return env.get("data")


async def _envelope(method: str, url: str, *, origin: str, bearer: str = "",
                    json_body: Any = None, ua: str = GLOBAL_WEB_UA,
                    extra: dict | None = None) -> tuple[int, str, Any]:
    """注册链路专用：原样返回 (code, msg, data)。

    注意这里和 _data 的信封语义**不同**：注册接口用 code=200 表示成功、code=0 表示成功
    只在计费类接口成立，所以不能套用 _data 的判定。
    """
    resp = await _raw(method, url, _headers(origin, bearer, ua, extra), json_body)
    try:
        env = resp.json()
    except ValueError as exc:
        raise OAuthError(f"上游返回的不是 JSON（HTTP {resp.status_code}）", resp.status_code) from exc
    if not isinstance(env, dict):
        raise OAuthError("上游返回结构异常", resp.status_code)
    return int(env.get("code") or 0), str(env.get("msg") or ""), env.get("data")


# ──────────────────────────────────────────────────────────────────
# 第 1 步：发起授权
# ──────────────────────────────────────────────────────────────────
async def start(realm: str = "cn", region: str = "") -> dict:
    realm = "global" if realm == "global" else "cn"
    region = region.strip().upper()
    if region not in GLOBAL_REGION_WHITELIST:
        region = ""
    ep = endpoints(realm)

    data = await _data("POST", ep.base + STATE_PATH, origin=ep.origin, json_body={})
    if not isinstance(data, dict):
        raise OAuthError("上游 auth/state 返回结构异常（缺 data 对象）")
    # state 与 authUrl 必须都在：少一个就没法继续，且不能"猜"。
    state = str(data.get("state") or "").strip()
    url = str(data.get("authUrl") or "").strip()
    if not state or not url:
        raise OAuthError("上游未返回 state 或 authUrl")
    if not _STATE_RE.match(state):
        raise OAuthError("上游返回的 state 含意外字符，已拒绝使用")
    if not url.lower().startswith(("http://", "https://")):
        raise OAuthError("上游返回的授权链接不是 http(s) 地址")

    sess = LoginSession(state=state, realm=realm, url=url, created=time.time(), region=region)
    async with _lock:
        _prune_locked(time.time())
        _sessions[state] = sess
    _persist()
    db.audit("admin", "account.login_start", f"realm={realm} region={region or '-'}")
    return {"realm": realm, "state": state, "url": url, "region": region, "mode": "http"}


async def start_binary(realm: str = "cn") -> dict:
    """兼容路径：调内置 `login` 工具取授权链接（settings.LOGIN_MODE=binary）。

    这条路的中间态由那个工具自己按 realm 落在 cwd 里，所以同一个 realm 无法并发
    开两个加号 —— state 在这里只是个会话句柄，不参与上游交互。
    """
    realm = "global" if realm == "global" else "cn"
    result = await upstream.login_url(realm)
    state = f"bin-{realm}-{secrets.token_hex(8)}"
    sess = LoginSession(state=state, realm=realm, url=result["url"],
                        created=time.time(), mode="binary")
    async with _lock:
        _prune_locked(time.time())
        _sessions[state] = sess
    _persist()
    db.audit("admin", "account.login_start", f"realm={realm} mode=binary")
    return {"realm": realm, "state": state, "url": result["url"], "region": "", "mode": "binary"}


async def _poll_binary(sess: LoginSession) -> dict:
    """兼容路径的轮询。`login poll` 是「一次定生死」的，没授权完就直接失败，
    所以这里把任何失败都当成"还没好"，让前端继续等（TTL 兜底）。"""
    sess.polls += 1
    try:
        saved = await upstream.login_poll(sess.realm)
    except upstream.UpstreamError as exc:
        sess.last_message = str(exc)
        _persist()
        return {"done": False, "realm": sess.realm, "message": str(exc),
                "polls": sess.polls, "mode": "binary"}
    await forget(sess.state)
    checkin = saved.get("checkin") or {}
    return {
        "done": True,
        "uid": saved["uid"],
        "nickname": saved.get("nickname") or "",
        "realm": saved.get("realm") or sess.realm,
        "updated": bool(saved.get("updated")),
        "file": saved.get("file") or "",
        "expires_at": None,
        "message": checkin.get("message") or "登录完成",
        "mode": "binary",
    }


# ──────────────────────────────────────────────────────────────────
# 第 2 步：轮询授权结果
# ──────────────────────────────────────────────────────────────────
async def poll(state: str) -> dict:
    """轮询一次。未完成 → {"done": False, "message": ...}；完成 → 落盘并返回账号信息。

    「未完成」与「出错」在这里刻意合并成同一个 done=False 分支：
    上游在待授权、state 失效、网络抖动这几种情况下返回的东西长得差不多，
    而用户能做的动作完全一样（继续等或重新发起）。
    真正兜底的是会话 TTL —— 见下面 OAuthError(410)。
    """
    sess = _sessions.get(state)
    if sess is None:
        # 面板刚重启过：DB 里可能还有，补加载一次再判。
        await load_sessions()
        sess = _sessions.get(state)
    if sess is None:
        raise OAuthError("未知或已失效的登录会话，请重新发起添加账号", 404)

    if time.time() - sess.created > settings.LOGIN_TTL:
        await forget(state)
        raise OAuthError(
            f"登录会话已超时（{settings.LOGIN_TTL // 60} 分钟内未完成授权），请重新发起", 410
        )

    if sess.mode == "binary":
        return await _poll_binary(sess)

    ep = endpoints(sess.realm)
    sess.polls += 1

    try:
        data = await _data("GET", ep.base + TOKEN_PATH + sess.state, origin=ep.origin)
    except OAuthPending as exc:
        sess.last_message = str(exc)
        _persist()
        return {"done": False, "realm": sess.realm, "message": str(exc),
                "polls": sess.polls, "mode": "http"}

    tok = data if isinstance(data, dict) else {}
    access = str(tok.get("accessToken") or "")
    if not access:
        # code=0 但没给 token：按「还没好」处理（与上游 CLI 的判定一致）。
        sess.last_message = "waiting for login"
        _persist()
        return {"done": False, "realm": sess.realm, "message": "waiting for login",
                "polls": sess.polls, "mode": "http"}

    # 账号信息拿失败不阻断：缺的只是展示名，token 已经到手了。
    acct: dict = {}
    with contextlib.suppress(OAuthError, OAuthPending):
        adata = await _data("GET", ep.base + ACCOUNT_PATH + sess.state,
                            origin=ep.origin, bearer=access)
        if isinstance(adata, dict):
            acct = adata

    uid = str(acct.get("uid") or "").strip()
    if not uid:
        # 不清理会话：账号信息接口是"可能抽风"的，用户授权已经完成，
        # 再轮一次很可能就拿到了，所以让前端继续重试（TTL 兜底）。
        raise OAuthError(
            "已拿到 token，但账号信息接口没返回 uid，无法落盘。稍后会自动重试。", 502
        )

    expires_in = int(tok.get("expiresIn") or 0)
    record = {
        "account": {
            "uid": uid,
            "enterpriseId": acct.get("enterpriseId") or "",
            "nickname": acct.get("nickname") or "",
        },
        "auth": {
            "accessToken": access,
            "refreshToken": tok.get("refreshToken") or "",
            "expiresAt": int(time.time()) + expires_in if expires_in else 0,
            "domain": tok.get("domain") or "",
            "realm": sess.realm,
        },
    }
    try:
        saved = upstream.write_auth_file(record)
    except ValueError as exc:
        # uid 不合法这类问题是终局性的：重试多少次都一样，直接把会话收掉，
        # 免得前端每 3 秒撞一次同一个错误直到 TTL。
        await forget(state)
        raise OAuthError(f"凭证落盘失败：{exc}", 502) from exc

    notes = await _post_login_actions(sess.realm, record, region=sess.region)
    await forget(state)
    db.audit("admin", "account.login_done",
             f"uid={uid} realm={sess.realm} region={sess.region or '-'} "
             f"updated={saved['updated']} notes={notes}")

    return {
        "done": True,
        "uid": uid,
        "nickname": record["account"]["nickname"],
        "realm": sess.realm,
        "updated": saved["updated"],
        "file": saved["file"],
        "expires_at": record["auth"]["expiresAt"],
        "message": notes or "登录完成",
        "mode": "http",
    }


async def _post_login_actions(realm: str, record: dict, region: str = "") -> str:
    """落盘后的收尾：CN 签到 / global 注册激活+trial。全程尽力而为，失败只回文案。"""
    auth = record["auth"]
    account = record["account"]
    parts: list[str] = []
    try:
        if realm == "global":
            msg = await activate_global(
                auth["accessToken"], account["uid"], region=region
            )
            if msg:
                parts.append(msg)
        else:
            result = await upstream.daily_checkin(
                auth["accessToken"], account["uid"],
                account["enterpriseId"], auth["domain"],
            )
            if result.get("message"):
                parts.append(str(result["message"]))
    except (OAuthError, OAuthPending, httpx.HTTPError) as exc:
        parts.append(f"收尾步骤未完成（不影响登录）：{exc}")
    return "；".join(p for p in parts if p)


# ──────────────────────────────────────────────────────────────────
# 国际版注册激活 + trial
# ──────────────────────────────────────────────────────────────────
async def _register_status(ep: Endpoints, access: str, uid: str) -> tuple[bool, bool, str]:
    """(已激活, 缺地区, 原始消息)。端点与判定取自上游 cmd/signin 的实测实现。"""
    code, msg, _ = await _envelope(
        "GET", ep.base + REGISTER_PATH + quote(uid, safe=""),
        origin=ep.base, bearer=access, extra={"X-User-Id": uid},
    )
    if code == 200:
        return True, False, msg
    if code == 500 or "region required" in msg.lower():
        return False, True, msg
    return False, False, msg


async def _fetch_countries(ep: Endpoints, access: str) -> list[dict]:
    _, _, data = await _envelope(
        "POST", ep.base + COUNTRY_CODE_PATH, origin=ep.base, bearer=access,
        json_body={"filterForbidden": 1},
    )
    # data 可能是对象，也可能是**被序列化成字符串的 JSON**（双层信封）——
    # 上游这个接口实测就是后者，不二次解析会直接解析失败。
    if isinstance(data, str):
        with contextlib.suppress(ValueError):
            data = json.loads(data)
    if not isinstance(data, dict):
        return []
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    items = inner.get("list") if isinstance(inner, dict) else None
    if not isinstance(items, list):
        return []

    by_code: dict[str, dict] = {}
    for item in items:
        if isinstance(item, dict) and item.get("IOS2"):
            by_code[str(item["IOS2"]).upper()] = item
    # 按白名单顺序输出，保证"不指定时取首个"稳定等于 HK。
    return [by_code[c] for c in GLOBAL_REGION_WHITELIST if c in by_code]


async def _submit_region(ep: Endpoints, access: str, country: dict) -> None:
    attrs = {
        "countryCode": [str(country.get("Code") or "")],
        "countryFullName": [str(country.get("EnName") or "")],
        "countryName": [str(country.get("IOS2") or "")],
    }
    code, msg, _ = await _envelope(
        "POST", ep.base + SUBMIT_REGION_PATH, origin=ep.base, bearer=access,
        json_body={"attributes": attrs},
    )
    if code != 0:
        raise OAuthError(f"提交注册地区失败：{msg or f'code={code}'}")


async def _claim_trial(ep: Endpoints, access: str) -> str:
    code, msg, data = await _envelope(
        "POST", ep.base + TRIAL_PATH, origin=ep.base, bearer=access, json_body={}
    )
    # 14051 = 已经领过。它是幂等成功，不是失败。
    if code == 0 or "14051" in json.dumps(data, ensure_ascii=False) or "14051" in msg:
        return "trial 加油包已领取"
    return f"trial 未领取：{msg or f'code={code}'}"


async def activate_global(access: str, uid: str, region: str = "") -> str:
    """国际版新号：注册激活（缺地区自动补）+ trial。幂等，返回给用户看的说明。"""
    if not access or not uid:
        return ""
    ep = endpoints("global")
    activated, needs_region, msg = await _register_status(ep, access, uid)
    if activated:
        return "注册已激活；" + await _claim_trial(ep, access)
    if not needs_region:
        return f"注册未通过：{msg or '上游未说明原因'}（登录已生效，可在账号页重试）"

    countries = await _fetch_countries(ep, access)
    if not countries:
        return "注册需要补充地区，但未能取到可用地区列表"
    pick = next((c for c in countries if str(c.get("IOS2", "")).upper() == region), None)
    pick = pick or countries[0]
    await _submit_region(ep, access, pick)
    activated, _, msg = await _register_status(ep, access, uid)
    if not activated:
        return f"已提交地区 {pick.get('IOS2')}，但激活仍未通过：{msg or '上游未说明原因'}"
    iso2 = pick.get("IOS2") or ""
    return f"已激活（地区 {iso2}）；" + await _claim_trial(ep, access)


async def refresh_global_region(uid: str, realm: str = "global") -> str:
    """给已存在的 global 账号补做一次注册激活（账号页的手动入口）。"""
    files = {f["uid"]: f for f in upstream.auth_files()}
    target = files.get(uid)
    if not target:
        raise OAuthError("未找到该账号的凭证文件", 404)
    token, _, _ = upstream.read_auth_tokens(uid)
    if not token:
        raise OAuthError("凭证文件里没有 accessToken", 400)
    return await activate_global(token, uid)
