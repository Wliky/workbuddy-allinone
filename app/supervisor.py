"""上游 Go 网关（wb2api）的进程托管。

这是「一体化」替代掉 docker CLI + compose 的那个部件：
  · 进程直接由本服务 fork，不再需要 docker.sock、docker CLI、compose 插件；
  · 日志直接读管道，不再需要 `docker logs`；
  · 重启就是 restart()，不再需要容器编排层。
代价是上游和面板同生共死 —— 这是单容器方案的固有取舍，换来的是
玩客云上少 ~150MB 镜像体积、少一层权限暴露、少一个会失配的 Docker 版本依赖。
"""
from __future__ import annotations

import asyncio
import collections
import os
import shutil
import signal
import time
from pathlib import Path

import httpx

from . import db, settings

BACKOFF = [1, 2, 5, 10, 20, 30]  # 崩溃重启退避（秒），封顶 30s


class Supervisor:
    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.started_at: float | None = None
        self.last_exit_code: int | None = None
        self.last_exit_at: float | None = None
        self.restarts = 0
        self.logs: collections.deque[str] = collections.deque(maxlen=settings.UPSTREAM_LOG_LINES)
        self.last_error = ""
        self._lock = asyncio.Lock()
        self._reader: asyncio.Task | None = None
        self._watcher: asyncio.Task | None = None
        self._intent_stop = False
        self._log_fh = None

    # ── 状态 ───────────────────────────────────────────────────────
    @property
    def external(self) -> bool:
        return settings.UPSTREAM_EXTERNAL

    @property
    def running(self) -> bool:
        if self.external:
            # 外部托管时「在跑」只能由调用方自己的探活结论决定，不能凭本地进程猜
            return True
        return self.proc is not None and self.proc.returncode is None

    def status(self) -> dict:
        return {
            "running": self.running,
            "external": self.external,
            "pid": self.proc.pid if (not self.external and self.running) else None,
            "uptime_sec": int(time.time() - self.started_at) if self.started_at and not self.external else 0,
            "restarts": self.restarts,
            "last_exit_code": self.last_exit_code,
            "last_exit_at": self.last_exit_at,
            "last_error": self.last_error,
            "binary": str(settings.UPSTREAM_BIN),
            "binary_exists": settings.UPSTREAM_BIN.exists(),
            "config": str(settings.UPSTREAM_CONFIG),
            "config_exists": settings.UPSTREAM_CONFIG.exists(),
            "commit": settings.UPSTREAM_COMMIT,
            "addr": settings.UPSTREAM_ADDR,
        }

    def tail_logs(self, limit: int = 200) -> list[str]:
        items = list(self.logs)
        return items[-limit:]

    # ── 生命周期 ───────────────────────────────────────────────────
    async def start(self, reason: str = "manual") -> tuple[bool, str]:
        if self.external:
            ok, _ = await self._external_reachable()
            return ok, (
                "上游由外部托管（WB_UPSTREAM_EXTERNAL=1），本服务不管理它的进程"
                if ok else f"外部上游不可达：{settings.UPSTREAM_BASE}"
            )
        async with self._lock:
            if self.running:
                return True, "上游已在运行"
            if not settings.UPSTREAM_BIN.exists():
                self.last_error = f"缺少上游可执行文件 {settings.UPSTREAM_BIN}"
                return False, self.last_error
            if not settings.UPSTREAM_CONFIG.exists():
                self.last_error = f"缺少上游配置 {settings.UPSTREAM_CONFIG}"
                return False, self.last_error

            self._intent_stop = False
            self._open_log_file()
            env = dict(os.environ)
            env.setdefault("TZ", "Asia/Shanghai")
            self._append(f"[supervisor] 启动上游（{reason}）：{settings.UPSTREAM_BIN} -config {settings.UPSTREAM_CONFIG}")
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    str(settings.UPSTREAM_BIN),
                    "-config",
                    str(settings.UPSTREAM_CONFIG),
                    cwd=str(settings.UPSTREAM_DIR),  # config 里的 ./auths、./data 依赖 cwd
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                    start_new_session=True,  # 独立进程组，便于整组收掉
                )
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"启动失败：{exc}"
                self._append(f"[supervisor] {self.last_error}")
                return False, self.last_error

            self.started_at = time.time()
            self._reader = asyncio.create_task(self._read_output())
            self._watcher = asyncio.create_task(self._watch())

        ok, detail = await self.wait_ready()
        if ok:
            db.audit("system", "upstream.start", f"pid={self.proc.pid if self.proc else '?'} ({reason})")
            return True, f"上游已启动（pid={self.proc.pid if self.proc else '?'}）"
        return False, detail

    async def stop(self, reason: str = "manual") -> tuple[bool, str]:
        if self.external:
            return False, "上游由外部托管，请到它自己的部署处停止"
        async with self._lock:
            if not self.running:
                return True, "上游未在运行"
            self._intent_stop = True
            proc = self.proc
            self._append(f"[supervisor] 停止上游（{reason}）")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._append("[supervisor] 10s 未退出，SIGKILL")
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                await proc.wait()
        if self._reader:
            self._reader.cancel()
        if self._watcher:
            self._watcher.cancel()
        self.last_exit_code = proc.returncode
        self.last_exit_at = time.time()
        self.proc = None
        self.started_at = None
        db.audit("system", "upstream.stop", f"exit={proc.returncode} ({reason})")
        return True, "上游已停止"

    async def restart(self, reason: str = "manual") -> tuple[bool, str]:
        if self.external:
            return False, "上游由外部托管，本服务无法重启它（改动会在它下次重启后生效）"
        await self.stop(reason=f"restart:{reason}")
        await asyncio.sleep(0.4)
        return await self.start(reason=f"restart:{reason}")

    async def _external_reachable(self) -> tuple[bool, dict]:
        try:
            async with httpx.AsyncClient(timeout=4.0, trust_env=False) as client:
                r = await client.get(f"{settings.UPSTREAM_BASE}/healthz")
            return True, {"status": r.status_code}
        except httpx.HTTPError as exc:
            return False, {"error": str(exc)}

    async def wait_ready_external(self) -> tuple[bool, str]:
        ok, info = await self._external_reachable()
        return ok, ("外部上游可达" if ok else f"外部上游不可达：{info.get('error')}")

    # ── 内部 ───────────────────────────────────────────────────────
    def _open_log_file(self) -> None:
        if self._log_fh is not None:
            return
        try:
            settings.UPSTREAM_LOG.parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = open(settings.UPSTREAM_LOG, "a", encoding="utf-8", buffering=1)
        except OSError:
            self._log_fh = None

    def _append(self, line: str) -> None:
        self.logs.append(line)
        if self._log_fh is not None:
            try:
                self._log_fh.write(line + "\n")
            except OSError:
                pass
        else:
            self._open_log_file()

    async def _read_output(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        while True:
            try:
                raw = await proc.stdout.readline()
            except (asyncio.CancelledError, ValueError):
                return
            if not raw:
                return
            self._append(raw.decode("utf-8", "replace").rstrip("\r\n"))

    async def _watch(self) -> None:
        """非预期退出 → 退避重启。上游自己崩了不该让整个面板失能。"""
        proc = self.proc
        if proc is None:
            return
        code = await proc.wait()
        self.last_exit_code = code
        self.last_exit_at = time.time()
        self.proc = None
        self.started_at = None
        self._append(f"[supervisor] 上游进程退出，code={code}")
        if self._intent_stop:
            return
        delay = BACKOFF[min(self.restarts, len(BACKOFF) - 1)]
        self.restarts += 1
        self._append(f"[supervisor] {delay}s 后自动重启（第 {self.restarts} 次）")
        await asyncio.sleep(delay)
        if self._intent_stop:
            return
        ok, detail = await self.start(reason="auto-restart")
        if not ok:
            self.last_error = detail

    async def wait_ready(self) -> tuple[bool, str]:
        """探测 /healthz：连不上才算失败；503（无可用账号）也是「进程已就绪」。"""
        deadline = time.time() + settings.UPSTREAM_START_TIMEOUT
        last = ""
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
            while time.time() < deadline:
                if not self.running:
                    return False, self.last_error or "上游进程已退出，请查看日志"
                try:
                    r = await client.get(f"{settings.UPSTREAM_BASE}/healthz")
                    if r.status_code < 500 or r.status_code == 503:
                        return True, "上游就绪"
                    last = f"HTTP {r.status_code}"
                except Exception as exc:  # noqa: BLE001
                    last = str(exc)
                await asyncio.sleep(0.5)
        return False, f"等待上游就绪超时：{last}"

    async def health(self) -> dict:
        info = self.status()
        if not self.running:
            info["health"] = "down"
            return info
        try:
            async with httpx.AsyncClient(timeout=4.0, trust_env=False) as client:
                r = await client.get(f"{settings.UPSTREAM_BASE}/healthz")
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            info["health"] = "ok" if r.status_code == 200 else "degraded"
            info["healthy_accounts"] = body.get("healthy")
            info["total_accounts"] = body.get("total")
            info["realm_servable"] = body.get("realm_servable")
        except Exception as exc:  # noqa: BLE001
            info["health"] = "degraded"
            info["health_error"] = str(exc)
        return info

    def log_file_tail(self, limit: int = 300) -> list[str]:
        p = Path(settings.UPSTREAM_LOG)
        if not p.exists():
            return []
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                return [line.rstrip("\n") for line in collections.deque(fh, maxlen=limit)]
        except OSError:
            return []


supervisor = Supervisor()


def disk_usage() -> dict:
    try:
        u = shutil.disk_usage(str(settings.DATA_DIR))
        return {"total_mb": u.total // 1048576, "used_mb": u.used // 1048576, "free_mb": u.free // 1048576}
    except OSError:
        return {}
