"""端到端冒烟测试：用假上游打真实代码路径。

不 mock 任何本项目代码 —— 起一个真的 HTTP 假上游（实现 /healthz /status
/v1/models /v1/chat/completions /admin/accounts/* /v1/stats），真的起 uvicorn，
再用真的 HTTP 请求把面板与网关的路径打一遍。

用法：
    python tests/smoke_test.py
    python tests/smoke_test.py --python /path/to/venv/bin/python   # 指定解释器

上游 Go 子进程托管无法在 Windows 上验证，所以走 WB_UPSTREAM_EXTERNAL=1
分支（顺带验证该分支本身）。
"""
from __future__ import annotations

import argparse
import contextlib
import http.cookiejar
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJ = Path(__file__).resolve().parents[1]
UP_PORT = 17863
APP_PORT = 17864
OAUTH_PORT = 17865          # 假设备授权服务（cn）；global 用 +1

REQUIRED = ("fastapi", "uvicorn", "httpx")
results: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


# ── 假上游 ────────────────────────────────────────────────────────
class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 别把测试输出淹掉
        pass

    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth_ok(self):
        return self.headers.get("Authorization") == "Bearer internal-test-key"

    def do_GET(self):
        if self.path == "/healthz":
            return self._json({"healthy": 1, "total": 2, "service": "workbuddy2api",
                               "realm_servable": {"cn": True, "global": True}})
        if not self._auth_ok():
            return self._json({"error": "unauthorized"}, 401)
        if self.path == "/status":
            return self._json({
                "accounts": [
                    {"uid": "u1", "realm": "cn", "nickname": "甲", "credits": 1200,
                     "cooling": False, "disabled": False, "manual_disabled": False,
                     "success_count": 42, "err_total": 1, "consecutive_fails": 0, "in_flight": 0},
                    # u2 刻意同时「手动停用」+「冷却」，用来验证多重状态的折叠规则
                    {"uid": "u2", "realm": "global", "nickname": "乙", "credits": 30,
                     "cooling": True, "cool_remaining_sec": 300, "cool_kind": "soft",
                     "disabled": False, "manual_disabled": True, "manual_reason": "观察中",
                     "success_count": 3, "err_total": 5, "consecutive_fails": 2, "in_flight": 1},
                ],
                "total": 2, "healthy": 1, "cooling": 1, "disabled": 0, "in_flight_full": 0,
                "realm_totals": {
                    "cn": {"total": 1, "healthy": 1, "cooling": 0, "disabled": 0, "in_flight_full": 0},
                    "global": {"total": 1, "healthy": 0, "cooling": 1, "disabled": 0, "in_flight_full": 0},
                },
                "sticky_sessions": 3, "redis_mode": "noop",
            })
        if self.path == "/v1/models":
            return self._json({"object": "list", "data": [
                {"id": "deepseek-v4-flash", "name": "Flash", "credits": "x0.05 credits",
                 "vendor": "tencent", "tags": ["fast"]},
                {"id": "glm-5", "name": "GLM-5", "credits": "x0.20 credits", "tags": []},
            ]})
        if self.path == "/v1/stats":
            return self._json({"requests": 99, "tokens": 12345})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        if self.path.startswith("/admin/accounts/"):
            if not self._auth_ok():
                return self._json({"error": "unauthorized"}, 401)
            parts = self.path.split("/")
            return self._json({"uid": parts[3], "action": parts[4], "changed": True})
        if self.path == "/v1/stats/reset":
            return self._json({"ok": True})
        if self.path == "/v1/chat/completions":
            if not self._auth_ok():
                return self._json({"error": "unauthorized"}, 401)
            payload = json.loads(body or b"{}")
            if payload.get("stream"):
                # 末尾那条带 usage 的 chunk 是关键：验证面板能从字节流尾部把它捞出来
                chunks = [
                    {"id": "c1", "choices": [{"delta": {"content": "你"}}]},
                    {"id": "c1", "choices": [{"delta": {"content": "好"}}]},
                    {"id": "c1", "choices": [{"delta": {}}],
                     "usage": {"prompt_tokens": 7, "completion_tokens": 2, "credits": 0.015}},
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for c in chunks:
                    data = f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode()
                    self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()
                done = b"data: [DONE]\n\n"
                self.wfile.write(f"{len(done):X}\r\n".encode() + done + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                return
            return self._json({
                "id": "c1", "model": payload.get("model"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "你好，我是假上游。"}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 6, "credits": 0.02},
            })
        return self._json({"error": "not found"}, 404)


# ── 假设备授权服务 ─────────────────────────────────────────────────
# 刻意复刻真实上游的两个「坑点」，否则这段测试等于没测：
#   · token 接口在用户授权前返回 HTTP 200 + code 非 0（不是 4xx）；
#   · 地区列表的 data 是被序列化成字符串的 JSON（双层信封）。
COUNTRIES = [
    {"IOS2": "SG", "Code": "65", "EnName": "Singapore"},
    {"IOS2": "HK", "Code": "852", "EnName": "Hong Kong"},
    {"IOS2": "MO", "Code": "853", "EnName": "Macao"},
]
OAUTH = {r: {"seq": 0, "authorized": set(), "regions": {}, "hits": {}}
         for r in ("cn", "global")}


def oauth_handler(realm: str):
    """生成某个 realm 的假设备授权服务端。state/uid 一一对应：st-<realm>-N → u-<realm>-N。"""
    box = OAUTH[realm]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            raw = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _p(self):
            return urlparse(self.path).path

        def _q(self):
            return parse_qs(urlparse(self.path).query)

        def _hit(self, p):
            box["hits"][p] = box["hits"].get(p, 0) + 1

        def _uid_of_state(self, s):
            return f"u-{realm}-{s.rsplit('-', 1)[-1]}"

        def _bearer_uid(self):
            h = self.headers.get("Authorization") or ""
            return h[10:].strip() if h.lower().startswith("bearer at-") else ""

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            p = self._p()
            self._hit(p)
            if p == "/v2/plugin/auth/state":
                box["seq"] += 1
                s = f"st-{realm}-{box['seq']}"
                return self._json({"code": 0, "data": {
                    "state": s, "authUrl": f"https://example.test/auth/{s}"}})
            if p == "/v2/billing/meter/daily-checkin":
                return self._json({"code": 0, "msg": "签到成功", "data": {"credits": 10}})
            if p == "/billing/area/get-country-code":
                return self._json({"code": 0, "data": json.dumps(
                    {"data": {"list": COUNTRIES}}, ensure_ascii=False)})
            if p == "/console/login/account":
                uid = self._bearer_uid()
                iso = ""
                with contextlib.suppress(ValueError):
                    attrs = json.loads(raw or b"{}").get("attributes") or {}
                    iso = (attrs.get("countryName") or [""])[0]
                box["regions"][uid] = iso
                return self._json({"code": 0, "msg": "ok"})
            if p == "/billing/ide/trial":
                return self._json({"code": 0, "msg": "ok", "data": {"amount": 100}})
            return self._json({"code": 404, "msg": "not found"})

        def do_GET(self):
            p = self._p()
            q = self._q()
            self._hit(p)
            s = (q.get("state") or [""])[0]
            if p == "/v2/plugin/auth/token":
                if s not in box["authorized"]:
                    # 关键：HTTP 200 + code 非 0，不是 4xx
                    return self._json({"code": 1, "msg": "login ing"})
                uid = self._uid_of_state(s)
                return self._json({"code": 0, "data": {
                    "accessToken": f"at-{uid}", "refreshToken": "rt-x",
                    "expiresIn": 3600, "domain": "d.example"}})
            if p == "/v2/plugin/login/account":
                uid = self._uid_of_state(s)
                return self._json({"code": 0, "data": {
                    "uid": uid, "enterpriseId": "ent-1", "nickname": f"账号{uid}"}})
            if p == "/auth/realms/copilot/overseas/user/register":
                uid = (q.get("userId") or [""])[0]
                if uid in box["regions"]:
                    return self._json({"code": 200, "msg": "activated"})
                return self._json({"code": 500, "msg": "region required"})
            return self._json({"code": 404, "msg": "not found"})

    return Handler


class Client:
    """带 cookie 罐的极简 HTTP 客户端。"""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None, headers=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{APP_PORT}{path}", data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self.opener.open(req, timeout=40) as r:
                payload = r.read()
                return r.status, (payload if raw else _maybe_json(payload)), dict(r.headers)
        except urllib.error.HTTPError as e:
            payload = e.read()
            return e.code, (payload if raw else _maybe_json(payload)), dict(e.headers)


def _maybe_json(payload):
    try:
        return json.loads(payload)
    except Exception:
        return payload.decode("utf-8", "replace")


def _as_text(payload) -> str:
    return payload if isinstance(payload, str) else payload.decode("utf-8", "replace")


def run(python: Path) -> int:
    missing = [m for m in REQUIRED if subprocess.run(
        [str(python), "-c", f"import {m}"], capture_output=True).returncode != 0]
    if missing:
        print(f"当前解释器缺少依赖：{', '.join(missing)}")
        print(f"请先安装：{python} -m pip install -r {PROJ / 'app' / 'requirements.txt'}")
        return 2

    srv = ThreadingHTTPServer(("127.0.0.1", UP_PORT), Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 假设备授权服务：cn / global 各起一个（realm 不同 → 端点与收尾动作不同）
    oauth_srvs = []
    for realm, port in (("cn", OAUTH_PORT), ("global", OAUTH_PORT + 1)):
        s = ThreadingHTTPServer(("127.0.0.1", port), oauth_handler(realm))
        threading.Thread(target=s.serve_forever, daemon=True).start()
        oauth_srvs.append(s)

    srv_dir = Path(tempfile.mkdtemp(prefix="wb-smoke-"))
    for sub in ("data", "upstream/auths", "upstream/data", "bin"):
        (srv_dir / sub).mkdir(parents=True, exist_ok=True)
    (srv_dir / "upstream" / "config.json").write_text(json.dumps({
        "listen": ":7863", "api_key": "internal-test-key", "auth_dir": "./auths",
        "state_file": "./data/state.json", "admin": {"enabled": True},
        "schedule": {"checkin_hours": [9, 21], "checkin_enabled": True,
                     "travel_hours": [9], "travel_enabled": False},
        "pool": {"max_in_flight": 3, "breaker_threshold": 3},
        "session_sticky": {"enabled": True}, "cooldown": {"soft_rate": "600s"},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (srv_dir / "upstream" / "auths" / "workbuddy-u1.json").write_text(json.dumps({
        "account": {"uid": "u1", "enterpriseId": "", "nickname": "甲"},
        "auth": {"accessToken": "a", "refreshToken": "r",
                 "expiresAt": int(time.time()) + 86400, "domain": "", "realm": "cn"},
    }), encoding="utf-8")

    env = dict(os.environ)
    env.update({
        "WB_SRV_DIR": str(srv_dir),
        "WB_DATA_DIR": str(srv_dir / "data"),
        "WB_STATIC_DIR": str(PROJ / "web"),
        "WB_UPSTREAM_DIR": str(srv_dir / "upstream"),
        "WB_BIN_DIR": str(srv_dir / "bin"),
        "WB_UPSTREAM_BIN": str(srv_dir / "bin" / "wb2api"),
        "WB_LOGIN_BIN": str(srv_dir / "bin" / "login"),
        "WB_ADMIN_PASSWORD": "test12345678",
        "WB_UPSTREAM_EXTERNAL": "1",
        "WB_UPSTREAM_ADDR": f"127.0.0.1:{UP_PORT}",
        "WB_MANAGER_PORT": str(APP_PORT),
        "WB_AUTO_START_UPSTREAM": "1",
        # 把加号流量指到假授权服务（默认 http 模式，不依赖 login 二进制）
        "WB_LOGIN_MODE": "http",
        "WB_OAUTH_CN_BASE": f"http://127.0.0.1:{OAUTH_PORT}",
        "WB_OAUTH_CN_ORIGIN": f"http://127.0.0.1:{OAUTH_PORT}",
        "WB_OAUTH_GLOBAL_BASE": f"http://127.0.0.1:{OAUTH_PORT + 1}",
        "WB_OAUTH_GLOBAL_ORIGIN": f"http://127.0.0.1:{OAUTH_PORT + 1}",
        "WB_PBKDF2_ITERS": "1000",   # 测试里不必为弱口令散列付时间
        "PYTHONUNBUFFERED": "1",
    })

    log_path = srv_dir / "app.log"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(APP_PORT), "--log-level", "warning"],
        cwd=str(PROJ), env=env, stdout=log, stderr=subprocess.STDOUT,
    )

    try:
        c = Client()
        ready = False
        for _ in range(60):
            if proc.poll() is not None:
                break
            try:
                st, _b, _h = c.call("GET", "/api/healthz")
                if st == 200:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not ready:
            log.close()
            print("服务未就绪，日志尾部：")
            print(log_path.read_text(encoding="utf-8", errors="replace")[-3000:])
            return 1

        print("\n--- 面板接口 ---")
        st, body, _ = c.call("GET", "/api/healthz")
        check("GET /api/healthz 200", st == 200, st)
        check("健康检查报告上游状态", isinstance(body, dict) and body.get("upstream") in ("ok", "degraded"),
              str(body)[:120])
        st, _b, _ = c.call("POST", "/api/login", {"username": "admin", "password": "wrong-pw"})
        check("错误密码被拒", st == 401, st)
        st, body, _ = c.call("POST", "/api/login", {"username": "admin", "password": "test12345678"})
        check("正确密码登录成功", st == 200, f"{st} {body}")
        st, body, _ = c.call("GET", "/api/me")
        check("GET /api/me 返回身份", st == 200 and body.get("username") == "admin", str(body)[:80])

        st, body, _ = c.call("GET", "/api/accounts")
        check("账号列表 200", st == 200, st)
        check("账号数正确", body.get("total") == 2 and len(body.get("accounts", [])) == 2,
              f"total={body.get('total')}")
        accs = {a["uid"]: a for a in body.get("accounts", [])}
        check("多重状态时 state 取主导原因（manual > cooling）",
              accs.get("u2", {}).get("state") == "manual", accs.get("u2", {}).get("state"))
        check("被折叠的冷却事实仍然保留",
              accs.get("u2", {}).get("cooling") is True
              and accs.get("u2", {}).get("cool_remaining_sec") == 300,
              accs.get("u2", {}).get("cool_remaining_sec"))
        check("单状态账号判定正确", accs.get("u1", {}).get("state") == "available",
              accs.get("u1", {}).get("state"))
        check("手动停用与系统禁用分开呈现", accs.get("u2", {}).get("manual_disabled") is True)
        check("凭证文件与账号关联", any(f.get("known") for f in body.get("files", [])))
        st, body, _ = c.call("POST", "/api/accounts/u2/enable", {"reason": ""})
        check("账号启用操作转发到上游", st == 200, f"{st} {str(body)[:80]}")

        st, body, _ = c.call("GET", "/api/settings")
        check("GET /api/settings 200", st == 200, st)
        check("api_key 被打码", body.get("config", {}).get("api_key") == "__SET__",
              body.get("config", {}).get("api_key"))
        st, body, _ = c.call("GET", "/api/settings/schedule-hint")
        check("定时任务翻译", st == 200 and any(t["key"] == "checkin" for t in body.get("tasks", [])), st)
        st, body, _ = c.call("PUT", "/api/settings",
                             {"patch": {"pool": {"max_in_flight": 5}}, "restart": False})
        check("设置保存", st == 200 and "pool" in body.get("changed", []), f"{st} {str(body)[:100]}")
        cfg = json.loads((srv_dir / "upstream" / "config.json").read_text(encoding="utf-8"))
        check("配置真的写进了 config.json", cfg["pool"]["max_in_flight"] == 5, cfg["pool"])
        st, _b, _ = c.call("PUT", "/api/settings", {"patch": {"api_key": "__SET__"}, "restart": False})
        check("打码占位符不会被当成新密钥写回",
              cfg["api_key"] == "internal-test-key", f"api_key={cfg['api_key']}")

        st, _b, _ = c.call("GET", "/api/logs")
        check("请求日志 200", st == 200, st)
        st, body, _ = c.call("GET", "/api/stats/summary?hours=24")
        check("用量汇总 200", st == 200 and "totals" in body, st)
        st, body, _ = c.call("GET", "/api/models")
        check("模型列表转发上游", st == 200 and body.get("count") == 2, f"{st}")
        st, body, _ = c.call("POST", "/api/playground", {"model": "deepseek-v4-flash", "prompt": "hi"})
        check("测试台非流式返回内容", st == 200 and body.get("ok") and body.get("content"),
              str(body.get("content"))[:40])
        st, raw, _ = c.call("POST", "/api/playground/stream",
                            {"model": "deepseek-v4-flash", "prompt": "hi"}, raw=True)
        txt = _as_text(raw)
        check("测试台流式返回 SSE", st == 200 and "data:" in txt and "[DONE]" in txt, f"{st} len={len(txt)}")
        st, body, _ = c.call("GET", "/api/system/about")
        check("系统信息 200", st == 200 and body.get("install_mode") == "all-in-one", st)

        print("\n--- 密钥与网关 ---")
        st, body, _ = c.call("POST", "/api/keys", {"name": "smoke", "quota_credits": 0})
        check("创建密钥返回明文", st == 200 and str(body.get("api_key", "")).startswith("wbk-"), st)
        plain = body.get("api_key")
        st, body, _ = c.call("GET", "/api/keys")
        check("密钥列表不含明文", st == 200 and all("api_key" not in k for k in body.get("keys", [])), st)
        st, _b, _ = c.call("GET", "/v1/models")
        check("网关：无密钥 401", st == 401, st)
        st, _b, _ = c.call("GET", "/v1/models", headers={"Authorization": "Bearer wbk-bogus"})
        check("网关：错误密钥 401", st == 401, st)
        st, body, _ = c.call("GET", "/v1/models", headers={"Authorization": f"Bearer {plain}"})
        check("网关：正确密钥 200 且透传模型", st == 200 and len(body.get("data", [])) == 2, st)
        st, body, _ = c.call("POST", "/v1/chat/completions",
                             {"model": "deepseek-v4-flash",
                              "messages": [{"role": "user", "content": "hi"}]},
                             headers={"Authorization": f"Bearer {plain}"})
        check("网关：非流式对话", st == 200 and body.get("choices"), f"{st} {str(body)[:100]}")
        st, raw, _ = c.call("POST", "/v1/chat/completions",
                            {"model": "deepseek-v4-flash", "stream": True,
                             "messages": [{"role": "user", "content": "hi"}]},
                            headers={"Authorization": f"Bearer {plain}"}, raw=True)
        txt = _as_text(raw)
        check("网关：流式原样透传", st == 200 and "data:" in txt and "[DONE]" in txt, f"{st} len={len(txt)}")

        time.sleep(0.6)
        st, body, _ = c.call("GET", "/api/logs?limit=20")
        logs = body.get("logs", []) if isinstance(body, dict) else []
        check("网关调用被记账", len(logs) >= 2, f"logs={len(logs)}")
        s_log = next((l for l in logs if l.get("stream")), None)
        check("流式调用的 token 已从尾部 usage 提取",
              bool(s_log) and s_log.get("prompt_tokens") == 7 and s_log.get("completion_tokens") == 2,
              str(s_log)[:140] if s_log else "无流式日志")
        check("流式调用的扣费已记账",
              bool(s_log) and abs(s_log.get("credits", 0) - 0.015) < 1e-6,
              s_log.get("credits") if s_log else "")

        st, body, _ = c.call("POST", "/api/keys", {"name": "restricted", "models": ["glm-5"]})
        plain2 = body.get("api_key")
        st, _b, _ = c.call("POST", "/v1/chat/completions",
                           {"model": "deepseek-v4-flash",
                            "messages": [{"role": "user", "content": "hi"}]},
                           headers={"Authorization": f"Bearer {plain2}"})
        check("网关：模型白名单拦截", st == 403, st)
        st, body, _ = c.call("GET", "/v1/models", headers={"Authorization": f"Bearer {plain2}"})
        check("网关：/v1/models 按白名单过滤",
              st == 200 and [m["id"] for m in body.get("data", [])] == ["glm-5"], st)
        st, body, _ = c.call("POST", "/api/keys", {"name": "ipblock", "ip_allow": "10.9.9.9/32"})
        st, _b, _ = c.call("GET", "/v1/models",
                           headers={"Authorization": f"Bearer {body.get('api_key')}"})
        check("网关：IP 白名单拦截", st == 403, st)

        print("\n--- 扫码加号（设备授权）---")
        # 假服务端约定：st-<realm>-N ↔ u-<realm>-N。uid 从返回的 state 推导，
        # 不写死序号（前面哪次 start 会占掉一位，写死必然偶发失败）。
        def uid_of(state):
            return "u-" + "-".join((state or "").split("-")[1:])

        st, body, _ = c.call("GET", "/api/accounts/login/pending")
        check("待授权列表 200 且为 http 模式", st == 200 and body.get("mode") == "http", st)
        st, body, _ = c.call("GET", "/api/accounts/login/regions")
        check("国际版地区列表含 HK", st == 200 and any(r["code"] == "HK" for r in body.get("regions", [])),
              str(body)[:100])

        st, body, _ = c.call("POST", "/api/accounts/login/start", {"realm": "cn"})
        s1 = (body or {}).get("state")
        check("发起加号拿到 state 与授权链接",
              st == 200 and s1 and str(body.get("url", "")).startswith("https://"), str(body)[:140])
        check("加号走进程内 http 模式", body.get("mode") == "http", body.get("mode"))

        st, body, _ = c.call("GET", f"/api/accounts/login/poll?state={s1}")
        check("未授权时轮询返回「未完成」", st == 200 and body.get("done") is False, str(body)[:120])
        check("未完成原因透传上游文案", "login ing" in str(body.get("message", "")), body.get("message"))

        st, b2, _ = c.call("POST", "/api/accounts/login/start", {"realm": "cn"})
        s2 = (b2 or {}).get("state")
        check("同一 realm 可并发开两个加号", st == 200 and s2 and s2 != s1, f"{s1} vs {s2}")
        st, body, _ = c.call("GET", "/api/accounts/login/pending")
        check("待授权列表同时挂两个会话", len(body.get("pending", [])) == 2,
              len(body.get("pending", [])))

        OAUTH["cn"]["authorized"].add(s1)
        st, body, _ = c.call("GET", f"/api/accounts/login/poll?state={s1}")
        check("授权完成后轮询返回账号",
              st == 200 and body.get("done") is True and body.get("uid") == "u-cn-1", str(body)[:200])
        check("CN 加号顺带完成每日签到", "签到" in str(body.get("message", "")), body.get("message"))
        check("签到端点被真的打过一次",
              OAUTH["cn"]["hits"].get("/v2/billing/meter/daily-checkin") == 1,
              OAUTH["cn"]["hits"])

        auth_file = srv_dir / "upstream" / "auths" / "workbuddy-u-cn-1.json"
        check("凭证按上游格式落盘", auth_file.exists(), str(auth_file))
        if auth_file.exists():
            rec = json.loads(auth_file.read_text(encoding="utf-8"))
            check("凭证是 account/auth 嵌套结构",
                  rec["account"]["uid"] == "u-cn-1" and rec["auth"]["accessToken"] == "at-u-cn-1",
                  str(rec)[:160])
            check("凭证 realm 正确", rec["auth"]["realm"] == "cn", rec["auth"].get("realm"))
            check("expiresAt 由 expiresIn 换算", rec["auth"]["expiresAt"] > time.time(),
                  rec["auth"].get("expiresAt"))

        st, body, _ = c.call("POST", "/api/accounts/login/cancel", {"state": s2})
        check("取消待授权会话", st == 200 and body.get("found") is True, f"{st} {str(body)[:80]}")
        st, body, _ = c.call("GET", "/api/accounts/login/pending")
        check("取消后从待授权列表消失",
              all(p["state"] != s2 for p in body.get("pending", [])), str(body)[:120])
        st, _b, _ = c.call("GET", "/api/accounts/login/poll?state=st-cn-999")
        check("未知 state 返回 404", st == 404, st)

        st, body, _ = c.call("POST", "/api/accounts/login/start",
                             {"realm": "global", "region": "ZZ"})
        check("非白名单地区被丢弃（回退默认）", st == 200 and body.get("region") == "",
              body.get("region"))
        st, body, _ = c.call("POST", "/api/accounts/login/start",
                             {"realm": "global", "region": "SG"})
        g1 = (body or {}).get("state")
        g_uid = uid_of(g1)
        check("国际版加号拿到 state 并记住指定地区",
              st == 200 and g1 and body.get("region") == "SG", str(body)[:140])
        OAUTH["global"]["authorized"].add(g1)
        st, body, _ = c.call("GET", f"/api/accounts/login/poll?state={g1}")
        check("国际版加号完成",
              st == 200 and body.get("done") is True and body.get("uid") == g_uid,
              str(body)[:220])
        check("国际版补注册地区 = 指定地区",
              OAUTH["global"]["regions"].get(g_uid) == "SG", OAUTH["global"]["regions"])
        check("地区列表双层信封被正确解析",
              OAUTH["global"]["hits"].get("/billing/area/get-country-code", 0) >= 1,
              OAUTH["global"]["hits"])
        check("trial 加油包已领取", "trial" in str(body.get("message", "")), body.get("message"))

        st, body, _ = c.call("POST", "/api/accounts/login/start", {"realm": "global"})
        g2 = (body or {}).get("state")
        OAUTH["global"]["authorized"].add(g2)
        st, body, _ = c.call("GET", f"/api/accounts/login/poll?state={g2}")
        check("未指定地区时取白名单首个（HK）而非接口返回顺序",
              OAUTH["global"]["regions"].get(uid_of(g2)) == "HK", OAUTH["global"]["regions"])

        st, body, _ = c.call("POST", "/api/accounts/refresh-region", {"uid": g_uid})
        check("已激活账号补做注册是幂等的", st == 200 and "已激活" in str(body.get("message", "")),
              f"{st} {str(body)[:120]}")
        st, _b, _ = c.call("POST", "/api/accounts/refresh-region", {"uid": "../evil"})
        check("refresh-region 拒绝非法 uid", st == 400, st)

        st, body, _ = c.call("DELETE", "/api/accounts/u-cn-1")
        check("删除刚加的凭证文件", st == 200 and not auth_file.exists(), f"{st} {str(body)[:80]}")

        print("\n--- 前端与鉴权边界 ---")
        st, raw, _ = c.call("GET", "/", raw=True)
        check("根路径返回前端页面", st == 200 and "WorkBuddy" in _as_text(raw), st)
        st, _r, _ = c.call("GET", "/assets/app.js", raw=True)
        check("静态资源可访问", st == 200, st)
        st, _r, _ = c.call("GET", "/#/accounts", raw=True)
        check("未知前端路由回落 index.html", st == 200, st)
        st, raw, _ = c.call("GET", "/%2e%2e/app/main.py", raw=True)
        body_text = _as_text(raw)
        check("路径穿越被拦（不回传后端源码）",
              st == 200 and "FastAPI(" not in body_text and "<html" in body_text.lower(),
              f"{st} len={len(body_text)}")
        c2 = Client()
        for path in ("/api/accounts", "/api/settings", "/api/system/about"):
            st, _b, _ = c2.call("GET", path)
            check(f"未登录访问 {path} 被拒", st == 401, st)
        st, _b, _ = c.call("POST", "/api/session/renew")
        check("会话续签可用", st == 200, st)

        print("\n--- 收尾 ---")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        applog = log_path.read_text(encoding="utf-8", errors="replace")
        errs = [l for l in applog.splitlines() if "Traceback" in l or "ERROR" in l]
        check("运行期间没有 Traceback / ERROR", not errs, "; ".join(errs[:3]))
        if errs:
            print("--- 应用日志 ---")
            print(applog[-4000:])
    finally:
        with contextlib.suppress(Exception):
            proc.kill()
        srv.shutdown()
        for s in oauth_srvs:
            with contextlib.suppress(Exception):
                s.shutdown()
        shutil.rmtree(srv_dir, ignore_errors=True)

    passed = sum(1 for _n, ok, _d in results if ok)
    print("\n" + "=" * 60)
    print(f"结果：{passed}/{len(results)} 通过")
    for n, ok, d in results:
        if not ok:
            print(f"  FAILED: {n}   [{d}]")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable,
                    help="用哪个解释器跑被测服务（需要装有 requirements.txt 的依赖）")
    args = ap.parse_args()
    sys.exit(run(Path(args.python)))
