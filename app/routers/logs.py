"""请求日志、用量统计、模型中心、聊天测试台。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException

from .. import db, upstream
from ..settings import UPSTREAM_BASE
from .deps import require_admin

router = APIRouter(prefix="/api", tags=["logs"])


@router.get("/logs")
async def list_logs(
    limit: int = 50,
    offset: int = 0,
    key_id: int | None = None,
    status: str = "",
    model: str = "",
    q: str = "",
    _: dict = Depends(require_admin),
):
    limit = max(1, min(limit, 500))
    where, args = [], []
    if key_id:
        where.append("key_id=?")
        args.append(key_id)
    if status == "ok":
        where.append("status >= 200 AND status < 400")
    elif status == "error":
        where.append("(status < 200 OR status >= 400)")
    if model:
        where.append("model=?")
        args.append(model)
    if q:
        where.append("(key_name LIKE ? OR ip LIKE ? OR error LIKE ?)")
        args += [f"%{q}%", f"%{q}%", f"%{q}%"]
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    total = db.one(f"SELECT COUNT(*) AS n FROM request_logs {clause}", args)["n"]
    rows = db.query(
        f"SELECT * FROM request_logs {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        args + [limit, offset],
    )
    return {"total": total, "limit": limit, "offset": offset, "logs": db.rows_to_dicts(rows)}


@router.delete("/logs")
async def purge_logs(user: dict = Depends(require_admin)):
    n = db.execute("DELETE FROM request_logs")
    db.audit(user["username"], "logs.purge", f"deleted={n}")
    return {"ok": True, "deleted": n}


@router.get("/stats/summary")
async def summary(hours: int = 24, _: dict = Depends(require_admin)):
    hours = max(1, min(hours, 24 * 30))
    since = int(time.time()) - hours * 3600

    agg = db.one(
        """SELECT COUNT(*) AS requests,
                  SUM(CASE WHEN status >= 200 AND status < 400 THEN 1 ELSE 0 END) AS ok,
                  SUM(prompt_tokens) AS prompt_tokens,
                  SUM(completion_tokens) AS completion_tokens,
                  SUM(credits) AS credits,
                  AVG(latency_ms) AS avg_latency
           FROM request_logs WHERE ts >= ?""",
        (since,),
    )
    requests = agg["requests"] or 0
    ok = agg["ok"] or 0

    bucket = 3600 if hours <= 72 else 86400
    series = db.query(
        f"""SELECT (ts / {bucket}) * {bucket} AS t,
                   COUNT(*) AS n,
                   SUM(CASE WHEN status >= 200 AND status < 400 THEN 1 ELSE 0 END) AS ok,
                   SUM(credits) AS credits,
                   SUM(prompt_tokens + completion_tokens) AS tokens
            FROM request_logs WHERE ts >= ?
            GROUP BY t ORDER BY t""",
        (since,),
    )
    by_model = db.query(
        """SELECT model, COUNT(*) AS n, SUM(credits) AS credits,
                  SUM(prompt_tokens + completion_tokens) AS tokens,
                  AVG(latency_ms) AS avg_latency
           FROM request_logs WHERE ts >= ?
           GROUP BY model ORDER BY n DESC LIMIT 20""",
        (since,),
    )
    by_key = db.query(
        """SELECT key_id, key_name, COUNT(*) AS n, SUM(credits) AS credits
           FROM request_logs WHERE ts >= ?
           GROUP BY key_id ORDER BY n DESC LIMIT 20""",
        (since,),
    )
    return {
        "hours": hours,
        "bucket_sec": bucket,
        "totals": {
            "requests": requests,
            "ok": ok,
            "errors": requests - ok,
            "success_rate": round(ok / requests * 100, 1) if requests else 100.0,
            "prompt_tokens": agg["prompt_tokens"] or 0,
            "completion_tokens": agg["completion_tokens"] or 0,
            "credits": round(agg["credits"] or 0, 4),
            "avg_latency_ms": int(agg["avg_latency"] or 0),
        },
        "series": db.rows_to_dicts(series),
        "by_model": db.rows_to_dicts(by_model),
        "by_key": db.rows_to_dicts(by_key),
    }


@router.get("/stats/upstream")
async def upstream_stats(_: dict = Depends(require_admin)):
    """上游自己维护的请求指标（/v1/stats）。上游没起时返回空，不报错。"""
    try:
        return await upstream.stats()
    except upstream.UpstreamError as exc:
        return {"unavailable": str(exc)}


@router.get("/models")
async def list_models(_: dict = Depends(require_admin)):
    try:
        payload = await upstream.models()
    except upstream.UpstreamError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    data = payload.get("data") or []
    return {"count": len(data), "models": data}


@router.post("/playground")
async def playground(body: dict, _: dict = Depends(require_admin)):
    """测试台：绕过面板密钥体系，直接用上游内部密钥打一次非流式请求。

    不写 request_logs —— 它属于运维自测，混进用量统计会污染真实调用数据。
    """
    model = (body.get("model") or "").strip()
    prompt = (body.get("prompt") or "").strip()
    if not model or not prompt:
        raise HTTPException(400, "model 与 prompt 必填")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    if body.get("system"):
        payload["messages"].insert(0, {"role": "system", "content": body["system"]})

    import httpx

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=180.0, trust_env=False) as client:
            r = await client.post(
                f"{UPSTREAM_BASE}/v1/chat/completions",
                json=payload,
                headers=upstream.auth_headers(),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"请求上游失败：{exc}") from exc

    latency = int((time.time() - t0) * 1000)
    if r.status_code != 200:
        try:
            detail = r.json()
        except ValueError:
            detail = r.text[:500]
        return {"ok": False, "status": r.status_code, "latency_ms": latency, "error": detail}

    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    return {
        "ok": True,
        "status": 200,
        "latency_ms": latency,
        "content": (choice.get("message") or {}).get("content") or "",
        "reasoning": (choice.get("message") or {}).get("reasoning_content") or "",
        "model": data.get("model") or model,
        "usage": data.get("usage") or {},
    }


@router.post("/playground/stream")
async def playground_stream(body: dict, _: dict = Depends(require_admin)):
    """测试台的流式版本：原样透传上游 SSE，供前端逐字渲染。"""
    model = (body.get("model") or "").strip()
    prompt = (body.get("prompt") or "").strip()
    if not model or not prompt:
        raise HTTPException(400, "model 与 prompt 必填")

    messages = [{"role": "user", "content": prompt}]
    if body.get("system"):
        messages.insert(0, {"role": "system", "content": body["system"]})
    payload = {"model": model, "messages": messages, "stream": True}

    import httpx
    from fastapi.responses import StreamingResponse

    url = f"{UPSTREAM_BASE}/v1/chat/completions"
    headers = dict(upstream.auth_headers())
    headers["Content-Type"] = "application/json"

    async def relay():
        try:
            async with httpx.AsyncClient(timeout=300.0, trust_env=False) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as r:
                    if r.status_code != 200:
                        raw = await r.aread()
                        yield f"data: {raw[:400].decode('utf-8', 'replace')}\n\n".encode()
                        yield b"data: [DONE]\n\n"
                        return
                    async for chunk in r.aiter_raw():
                        yield chunk
        except httpx.HTTPError as exc:
            yield f'data: {{"error":"{exc}"}}\n\n'.encode()
            yield b"data: [DONE]\n\n"

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
