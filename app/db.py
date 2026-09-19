"""SQLite 存储：管理员、API 密钥、请求审计、系统审计、元数据。

标准库 sqlite3，不引入 ORM —— armv7 上少一个依赖少一份风险。
单进程单写入者，用一把可重入锁串行化即可（本服务并发量很低）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Iterable

from . import settings

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  username     TEXT UNIQUE NOT NULL,
  pw_hash      TEXT NOT NULL,
  salt         TEXT NOT NULL,
  role         TEXT NOT NULL DEFAULT 'admin',
  created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  name           TEXT NOT NULL,
  key_hash       TEXT UNIQUE NOT NULL,
  prefix         TEXT NOT NULL,
  created_at     INTEGER NOT NULL,
  expires_at     INTEGER,
  disabled       INTEGER NOT NULL DEFAULT 0,
  models         TEXT NOT NULL DEFAULT '',
  ip_allow       TEXT NOT NULL DEFAULT '',
  quota_credits  REAL NOT NULL DEFAULT 0,
  used_credits   REAL NOT NULL DEFAULT 0,
  request_count  INTEGER NOT NULL DEFAULT 0,
  last_used      INTEGER
);

CREATE TABLE IF NOT EXISTS request_logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER NOT NULL,
  key_id      INTEGER,
  key_name    TEXT,
  ip          TEXT,
  model       TEXT,
  stream      INTEGER NOT NULL DEFAULT 0,
  status      INTEGER NOT NULL DEFAULT 0,
  latency_ms  INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  credits     REAL NOT NULL DEFAULT 0,
  error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON request_logs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_logs_key ON request_logs(key_id);

CREATE TABLE IF NOT EXISTS audit (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     INTEGER NOT NULL,
  actor  TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(settings.DB_PATH), check_same_thread=False, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.executescript(SCHEMA)
        c.commit()
        _conn = c
    return _conn


def query(sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, tuple(args)).fetchall()


def one(sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: Iterable[Any] = ()) -> int:
    with _lock:
        c = conn()
        cur = c.execute(sql, tuple(args))
        c.commit()
        return cur.lastrowid or 0


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


# ── meta ───────────────────────────────────────────────────────────
def meta_get(key: str, default: str = "") -> str:
    r = one("SELECT v FROM meta WHERE k=?", (key,))
    return r["v"] if r else default


def meta_set(key: str, value: str) -> None:
    execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, value),
    )


def meta_get_json(key: str, default: Any = None) -> Any:
    raw = meta_get(key, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def meta_set_json(key: str, value: Any) -> None:
    meta_set(key, json.dumps(value, ensure_ascii=False))


# ── 审计 ───────────────────────────────────────────────────────────
def audit(actor: str, action: str, detail: str = "") -> None:
    execute(
        "INSERT INTO audit(ts, actor, action, detail) VALUES(?, ?, ?, ?)",
        (int(time.time()), actor, action, detail[:2000]),
    )
    execute(
        "DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT 5000)"
    )


def log_request(
    *,
    key_id: int | None,
    key_name: str,
    ip: str,
    model: str,
    stream: bool,
    status: int,
    latency_ms: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    credits: float = 0.0,
    error: str = "",
) -> None:
    execute(
        """INSERT INTO request_logs
           (ts, key_id, key_name, ip, model, stream, status, latency_ms,
            prompt_tokens, completion_tokens, credits, error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            int(time.time()),
            key_id,
            key_name,
            ip,
            model,
            int(stream),
            status,
            latency_ms,
            prompt_tokens,
            completion_tokens,
            credits,
            error[:500],
        ),
    )
    if key_id:
        execute(
            """UPDATE api_keys
               SET used_credits = used_credits + ?,
                   request_count = request_count + 1,
                   last_used = ?
               WHERE id = ?""",
            (credits, int(time.time()), key_id),
        )


def prune_logs() -> int:
    """按保留天数与总量上限清理历史，避免小容量存储被日志吃掉。"""
    cutoff = int(time.time()) - settings.LOG_RETENTION_DAYS * 86400
    n = execute("DELETE FROM request_logs WHERE ts < ?", (cutoff,))
    execute(
        """DELETE FROM request_logs WHERE id NOT IN
           (SELECT id FROM request_logs ORDER BY id DESC LIMIT ?)""",
        (settings.LOG_KEEP_MAX,),
    )
    return n
