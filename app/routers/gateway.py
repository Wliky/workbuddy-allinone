"""对外 OpenAI 兼容网关（/v1/*）。

面板和网关同源同端口：管理端在 /api/*，网关在 /v1/*。
鉴权用面板分发的密钥（不是上游那把内部密钥），并在这一层完成
IP 白名单、模型白名单、配额、以及全量审计 —— 上游只做它擅长的事。
"""
from __future__ import annotations

import json
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .. import db, security, settings, upstream
from ..supervisor import supervisor

router = APIRouter(tags=["gateway"])

_HOP_HEADERS = {
    "host", "authorization", "content-length", "connection", "accept-encoding",
    "transfer-encoding", "keep-alive", "upgrade", "proxy-authorization",
}


def _error(status: int, message: str, code: str = "invalid_request_error", err_type: str = "invalid_request_error"):
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "code": code}},
    )


def _extract_credits(usage: dict | None) -> float:
    """上游的扣费字段名在不同版本里不完全一致，逐个试。"""
    if not isinstance(usage, dict):
        return 0.0
    for k in ("credit", "credits", "total_credit", "cost"):
        v = usage.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


async def _authenticate(request: Request) -> tuple[dict | None, str, JSONResponse | None]:
    """返回 (密钥行, 来源 IP, 错误响应)。三者中错误响应非空即代表鉴权失败。"""
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None, "", _error(401, "缺少 Authorization: Bearer <API Key>",
                                "missing_api_key", "authentication_error")
    plain = header[7:].strip()
    if not plain:
        return None, "", _error(401, "API Key 为空", "missing_api_key", "authentication_error")

    row = db.one("SELECT * FROM api_keys WHERE key_hash=?", (security.key_hash(plain),))
    if not row:
        return None, "", _error(401, "API Key 无效", "invalid_api_key", "authentication_error")

    now = int(time.time())
    if row["disabled"]:
        return None, "", _error(403, "该 API Key 已被停用", "key_disabled", "permission_error")
    if row["expires_at"] and row["expires_at"] < now:
        return None, "", _error(403, "该 API Key 已过期", "key_expired", "permission_error")
    if row["quota_credits"] and row["quota_credits"] > 0 and row["used_credits"] >= row["quota_credits"]:
        return None, "", _error(429, "该 API Key 已用尽配额", "quota_exceeded", "insufficient_quota")

    ip = security.client_ip(request)
    if not security.ip_allowed(ip, row["ip_allow"]):
        db.audit(row["name"], "gateway.ip_denied", f"ip={ip}")
        return None, ip, _error(403, f"来源 IP {ip} 不在白名单内", "ip_not_allowed", "permission_error")

    return row, ip, None


def _model_allowed(row, model: str) -> bool:
    allow = [m.strip() for m in (row["models"] or "").split(",") if m.strip()]
    return (not allow) or (model in allow)


def _log(row, ip, model, stream, status, t0, usage=None, error="", credits_override=None):
    usage = usage or {}
    db.log_request(
        key_id=row["id"],
        key_name=row["name"],
        ip=ip,
        model=model,
        stream=stream,
        status=status,
        latency_ms=int((time.time() - t0) * 1000),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        credits=credits_override if credits_override is not None else _extract_credits(usage),
        error=error,
    )


@router.get("/v1/models")
async def v1_models(request: Request):
    row, _ip, err = await _authenticate(request)
    if err is not None:
        return err
    if not supervisor.running:
        return _error(503, "上游网关未运行", "upstream_down", "api_error")
    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            r = await client.get(
                f"{settings.UPSTREAM_BASE}/v1/models",
                headers={"Authorization": f"Bearer {upstream.internal_key()}"},
            )
        data = r.json() if r.status_code == 200 else {"object": "list", "data": []}
    except (httpx.HTTPError, ValueError):
        return _error(502, "访问上游失败", "upstream_error", "api_error")

    data = dict(data or {})
    allow = [m.strip() for m in (row["models"] or "").split(",") if m.strip()]
    if allow:
        data["data"] = [m for m in (data.get("data") or []) if m.get("id") in allow]
    return JSONResponse(content=data)


@router.post("/v1/chat/completions")
async def v1_chat_completions(request: Request):
    row, ip, err = await _authenticate(request)
    if err is not None:
        return err

    try:
        body = await request.json()
    except ValueError:
        return _error(400, "请求体不是合法 JSON")

    model = (body.get("model") or "").strip()
    if not model:
        return _error(400, "缺少 model 字段")
    if not _model_allowed(row, model):
        _log(row, ip, model, bool(body.get("stream")), 403, time.time(), error="model_not_allowed")
        return _error(403, f"该 API Key 不允许使用模型 {model}", "model_not_allowed", "permission_error")

    if not supervisor.running:
        _log(row, ip, model, bool(body.get("stream")), 503, time.time(), error="upstream_down")
        return _error(503, "上游网关未运行", "upstream_down", "api_error")

    url = f"{settings.UPSTREAM_BASE}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {upstream.internal_key()}",
        "Content-Type": "application/json",
        "Accept": request.headers.get("accept") or "application/json",
    }
    # 会话粘性相关头透传给上游（上游自己实现粘性与 trace）
    for h in ("x-conversation-request-id", "x-trace-id"):
        if request.headers.get(h):
            headers[h] = request.headers[h]

    streaming = bool(body.get("stream"))
    t0 = time.time()

    if not streaming:
        try:
            async with httpx.AsyncClient(timeout=settings.UPSTREAM_TIMEOUT, trust_env=False) as client:
                r = await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException:
            _log(row, ip, model, False, 504, t0, error="upstream_timeout")
            return _error(504, "上游请求超时", "upstream_timeout", "api_error")
        except httpx.HTTPError as exc:
            _log(row, ip, model, False, 502, t0, error=str(exc))
            return _error(502, f"访问上游失败：{exc}", "upstream_error", "api_error")

        usage, err = None, ""
        if r.status_code == 200:
            try:
                data = r.json()
                usage = data.get("usage")
            except ValueError:
                err = "上游返回非 JSON"
        else:
            err = r.text[:300]
        _log(row, ip, model, False, r.status_code, t0, usage=usage, error=err)
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    # 流式：原样透传字节，只在尾部缓冲里找最后一条 usage 用于记账。
    # 不能用 aiter_lines 重新拼 SSE —— 会破坏上游的分帧。
    async def relay():
        status_code = 502
        err = ""
        tail = b""
        try:
            async with httpx.AsyncClient(timeout=settings.UPSTREAM_TIMEOUT, trust_env=False) as client:
                async with client.stream("POST", url, json=body, headers=headers) as r:
                    status_code = r.status_code
                    if r.status_code != 200:
                        body_bytes = await r.aread()
                        err = body_bytes[:300].decode("utf-8", "replace")
                        yield body_bytes
                    else:
                        async for chunk in r.aiter_raw():
                            yield chunk
                            tail = (tail + chunk)[-131072:]
        except httpx.TimeoutException:
            err = "upstream_timeout"
            yield b'data: {"error":{"message":"upstream timeout"}}\n\n'
        except httpx.HTTPError as exc:
            err = str(exc)
            yield json.dumps({"error": {"message": f"upstream error: {exc}"}}).encode()
        finally:
            usage = None
            for line in reversed(tail.split(b"\n")):
                if b'"usage"' not in line:
                    continue
                payload = line.strip()
                if payload.startswith(b"data:"):
                    payload = payload[5:].strip()
                try:
                    obj = json.loads(payload)
                except (ValueError, TypeError):
                    continue
                if isinstance(obj, dict) and obj.get("usage"):
                    usage = obj["usage"]
                    break
            _log(row, ip, model, True, status_code, t0, usage=usage, error=err)

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 让 nginx 类反代不要缓冲 SSE
            "Connection": "keep-alive",
        },
    )
