"""一体化服务入口：面板 + 面板 API + OpenAI 兼容网关，单进程单端口。

与「两个容器拼起来」的方案相比，这里刻意去掉了：
  · docker CLI / compose 插件（上游进程由 supervisor 直接托管）
  · Next.js / Node 构建链（前端是零构建的静态页，armv7 上少一个坑）
  · 第二个容器与 docker.sock（面板不再需要宿主 root 权限）
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, oauth, security, settings, upstream
from .routers import accounts, auth, gateway, keys, logs, settings_api
from .supervisor import supervisor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("workbuddy")

app = FastAPI(
    title="WorkBuddy All-in-One",
    version=settings.VERSION,
    docs_url="/api/docs" if settings.ENABLE_DOCS else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if settings.ENABLE_DOCS else None,
)

app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(keys.router)
app.include_router(logs.router)
app.include_router(settings_api.router)
app.include_router(gateway.router)


# ── 启动 / 关闭 ────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup() -> None:
    settings.ensure_dirs()
    db.conn()

    note = upstream.ensure_config()
    if note:
        log.info("上游配置：%s", note)
    if not upstream.admin_enabled():
        log.warning(
            "admin.enabled = false：面板里的「停用 / 启用 / 复活账号」不可用。"
            "到「系统设置」把它打开即可（保存后会自动重启上游）。"
        )

    _bootstrap_admin()

    # 恢复未完成的加号会话：用户可能取了授权链接、还没在手机上点完，面板就重启了。
    # 上游那边的 state 仍然有效，所以这里不该丢掉它。
    await oauth.load_sessions()
    if settings.LOGIN_MODE == "binary":
        log.info("加号走兼容路径（WB_LOGIN_MODE=binary）：调用内置 login 工具")

    if settings.AUTO_START_UPSTREAM:
        ok, detail = await supervisor.start(reason="startup")
        log.info("上游：%s（%s）", detail, "ok" if ok else "failed")
    else:
        log.info("WB_AUTO_START_UPSTREAM=0，跳过上游自动启动")

    app.state.prune_task = asyncio.create_task(_prune_loop())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    task = getattr(app.state, "prune_task", None)
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    # 面板退出要把它拉起来的上游一起收掉，避免容器里留孤儿进程
    await supervisor.stop(reason="shutdown")
    await oauth.aclose()


def _bootstrap_admin() -> None:
    if db.one("SELECT id FROM users LIMIT 1"):
        return
    password = settings.ADMIN_PASSWORD
    generated = False
    if not password:
        password = secrets.token_urlsafe(12)
        generated = True
    pw_hash, salt = security.hash_password(password)
    db.execute(
        "INSERT INTO users(username, pw_hash, salt, role, created_at) VALUES(?,?,?,?,?)",
        (settings.ADMIN_USERNAME, pw_hash, salt, "admin", int(time.time())),
    )
    db.audit("system", "admin.created", settings.ADMIN_USERNAME)
    if generated:
        log.warning("=" * 62)
        log.warning("  已创建管理员账号：%s", settings.ADMIN_USERNAME)
        log.warning("  随机初始密码：%s", password)
        log.warning("  请立即登录并修改（设置页 → 修改密码）。")
        log.warning("  也可以在部署时用 WB_ADMIN_PASSWORD 指定，避免看日志。")
        log.warning("=" * 62)
    else:
        log.info("已用 WB_ADMIN_PASSWORD 创建管理员：%s", settings.ADMIN_USERNAME)


async def _prune_loop() -> None:
    while True:
        try:
            n = db.prune_logs()
            if n:
                log.info("清理过期请求日志 %s 条", n)
        except Exception as exc:  # noqa: BLE001
            log.warning("日志清理失败：%s", exc)
        await asyncio.sleep(6 * 3600)


# ── 健康检查（无鉴权，容器 healthcheck 用）─────────────────────────
@app.get("/api/healthz")
async def healthz():
    up = await supervisor.health()
    return {
        "ok": up["health"] != "down",
        "service": "workbuddy-allinone",
        "version": settings.VERSION,
        "upstream": up["health"],
        "accounts": {"healthy": up.get("healthy_accounts"), "total": up.get("total_accounts")},
    }


@app.get("/healthz")
async def healthz_plain():
    return await healthz()


# ── 静态前端（零构建 SPA）──────────────────────────────────────────
WEB_DIR = Path(settings.STATIC_DIR)
if (WEB_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIR / "assets")), name="assets")


@app.get("/{full_path:path}")
async def spa(full_path: str):
    """兜底路由：先找真实文件，找不到就交还 index.html 由前端路由处理。"""
    if full_path.startswith(("api/", "v1/")):
        return JSONResponse({"error": {"message": "Not Found"}}, status_code=404)

    root = WEB_DIR.resolve()
    target = (root / full_path).resolve() if full_path else root / "index.html"
    # 目录穿越防护：解析后必须仍在 WEB_DIR 之内
    if full_path and str(target).startswith(str(root)) and target.is_file():
        headers = {"Cache-Control": "public, max-age=3600"}
        if target.suffix == ".html":
            headers = {"Cache-Control": "no-cache"}
        return FileResponse(str(target), headers=headers)

    index = root / "index.html"
    if index.is_file():
        return FileResponse(str(index), headers={"Cache-Control": "no-cache"})
    return JSONResponse(
        {"error": {"message": f"前端资源缺失：{index} 不存在"}}, status_code=500
    )


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        {"detail": f"服务内部错误：{exc.__class__.__name__}: {exc}"}, status_code=500
    )
