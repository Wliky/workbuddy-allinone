"""全局配置：环境变量 → 常量。所有路径都可在部署时覆盖。"""
from __future__ import annotations

import os
import secrets
from pathlib import Path


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key) or default


def _int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


# ── 目录 ───────────────────────────────────────────────────────────
SRV_DIR = Path(_env("WB_SRV_DIR", "/srv"))
DATA_DIR = Path(_env("WB_DATA_DIR", str(SRV_DIR / "data")))
STATIC_DIR = Path(_env("WB_STATIC_DIR", str(SRV_DIR / "web")))
UPSTREAM_DIR = Path(_env("WB_UPSTREAM_DIR", str(SRV_DIR / "upstream")))
BIN_DIR = Path(_env("WB_BIN_DIR", str(SRV_DIR / "bin")))

DB_PATH = Path(_env("WB_DB", str(DATA_DIR / "manager.db")))
UPSTREAM_CONFIG = Path(_env("WB_UPSTREAM_CONFIG", str(UPSTREAM_DIR / "config.json")))
UPSTREAM_CONFIG_EXAMPLE = Path(
    _env("WB_UPSTREAM_CONFIG_EXAMPLE", str(UPSTREAM_DIR / "config.example.json"))
)
AUTH_DIR = Path(_env("WB_AUTH_DIR", str(UPSTREAM_DIR / "auths")))
UPSTREAM_LOG = Path(_env("WB_UPSTREAM_LOG", str(DATA_DIR / "upstream.log")))

UPSTREAM_BIN = Path(_env("WB_UPSTREAM_BIN", str(BIN_DIR / "wb2api")))
LOGIN_BIN = Path(_env("WB_LOGIN_BIN", str(BIN_DIR / "login")))
SIGNIN_BIN = Path(_env("WB_SIGNIN_BIN", str(BIN_DIR / "signin_bin")))

# ── 加号（设备授权）登录 ────────────────────────────────────────────
# http   = 面板进程内直连上游设备授权端点（默认，不依赖任何外部二进制）
# binary = 保留旧路径：调用内置的 `login` 命令行工具（与上游 CLI 行为逐字一致）
LOGIN_MODE = _env("WB_LOGIN_MODE", "http").strip().lower()
if LOGIN_MODE not in ("http", "binary"):
    LOGIN_MODE = "http"

# 设备授权端点。做成可覆盖不是为了"换供应商"，而是因为：
#   · 测试要能把流量指到本地假上游，否则这段逻辑没法真正验证；
#   · 企业内网/镜像站替换域名时不必改代码。
# 默认值与上游 cmd/login 的常量逐字一致。
OAUTH_CN_BASE = _env("WB_OAUTH_CN_BASE", "https://copilot.tencent.com").rstrip("/")
OAUTH_CN_ORIGIN = _env("WB_OAUTH_CN_ORIGIN", "https://www.codebuddy.cn").rstrip("/")
OAUTH_GLOBAL_BASE = _env("WB_OAUTH_GLOBAL_BASE", "https://www.workbuddy.ai").rstrip("/")
OAUTH_GLOBAL_ORIGIN = _env("WB_OAUTH_GLOBAL_ORIGIN", "https://www.workbuddy.ai").rstrip("/")

OAUTH_TIMEOUT = _int("WB_OAUTH_TIMEOUT", 30)
# 一个待授权会话的存活时长：超时后轮询会收到明确的「已过期」而不是永远 pending。
# 设备流里用户在手机上走完登录通常 < 2 分钟，15 分钟是很宽的上限。
LOGIN_TTL = _int("WB_LOGIN_TTL", 900)
# 是否给设备授权请求带 cookie jar。上游 CLI 每次登录都是新进程（独立 jar），
# 参考实现（面板版）则明确不带 cookie、只靠 state 承载会话。
# 默认 0 = 跟随参考实现，也避免长时间运行的面板里跨会话串 cookie。
OAUTH_COOKIE_JAR = _env("WB_OAUTH_COOKIE_JAR", "0") == "1"

# 1 = 上游不由本服务托管（你自己在别处跑着 wb2api）。
# 面板/网关照常工作，只是「启动 / 停止 / 重启上游」变成空操作并如实提示。
UPSTREAM_EXTERNAL = _env("WB_UPSTREAM_EXTERNAL", "0") == "1"

# ── 监听 ───────────────────────────────────────────────────────────
# 一体化只有一个对外端口：面板、面板 API、OpenAI 兼容网关全部在 7864。
LISTEN_PORT = _int("WB_MANAGER_PORT", 7864)
# 上游 Go 进程只监听容器回环，外部不可达；不映射到宿主机。
UPSTREAM_ADDR = _env("WB_UPSTREAM_ADDR", "127.0.0.1:7863")
UPSTREAM_BASE = _env("WB_UPSTREAM_BASE", f"http://{UPSTREAM_ADDR}")

# ── 安全 ───────────────────────────────────────────────────────────
ADMIN_USERNAME = _env("WB_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = _env("WB_ADMIN_PASSWORD", "")          # 留空则首启随机生成并打印
SESSION_DAYS = _int("WB_SESSION_DAYS", 7)
SESSION_IDLE_HOURS = _int("WB_SESSION_IDLE_HOURS", 24)
# armv7 这类小核设备算力弱，迭代次数越高登录越慢（每次登录只算一次）。60000 ≈ 1~3s。
PBKDF2_ITERATIONS = _int("WB_PBKDF2_ITERS", 60000)
TRUST_PROXY = _env("WB_TRUST_PROXY", "0") == "1"        # 反代后置 1，取 X-Real-IP
SECURE_COOKIE = _env("WB_SECURE_COOKIE", "auto")        # auto | 1 | 0
ENABLE_DOCS = _env("WB_ENABLE_DOCS", "0") == "1"

# ── 上游维护信息（只用于界面展示与「更新」提示）──────────────────────
# 默认指向自己的 fork：界面上「上游仓库」的链接与更新提示应该指向你实际构建所用的仓库。
# 注意 GitHub 的 fork 不会自动跟随官方仓库，需要手动 Sync 后再重新构建。
UPSTREAM_REPO = _env("WB_UPSTREAM_REPO", "Wliky/workbuddy2api")
UPSTREAM_REF = _env("WB_UPSTREAM_REF", "master")
UPSTREAM_COMMIT = _env("WB_UPSTREAM_COMMIT", "")

# ── 运行时参数 ─────────────────────────────────────────────────────
UPSTREAM_TIMEOUT = _int("WB_UPSTREAM_TIMEOUT", 300)      # 长对话可能很久
UPSTREAM_START_TIMEOUT = _int("WB_UPSTREAM_START_TIMEOUT", 25)
LOG_RETENTION_DAYS = _int("WB_LOG_RETENTION_DAYS", 14)
LOG_KEEP_MAX = _int("WB_LOG_KEEP_MAX", 50000)
UPSTREAM_LOG_LINES = _int("WB_UPSTREAM_LOG_LINES", 800)  # 内存里保留的上游日志行数
AUTO_START_UPSTREAM = _env("WB_AUTO_START_UPSTREAM", "1") == "1"

VERSION = "0.1.0"


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPSTREAM_DIR, AUTH_DIR, UPSTREAM_DIR / "data", STATIC_DIR):
        d.mkdir(parents=True, exist_ok=True)


def session_secret() -> str:
    """签名密钥持久化到数据卷，重启后登录态不失效。"""
    p = DATA_DIR / ".session_secret"
    if not p.exists():
        p.write_text(secrets.token_hex(32), encoding="utf-8")
        try:
            p.chmod(0o600)
        except OSError:
            pass
    return p.read_text(encoding="utf-8").strip()
