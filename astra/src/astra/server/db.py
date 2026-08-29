from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

DEFAULT_DB = Path.home() / ".local" / "share" / "astra" / "astra.db"

_db_path: Path | None = None

SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    step_timeout INTEGER NOT NULL DEFAULT 15,
    decide_timeout INTEGER NOT NULL DEFAULT 15
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    decide_worker TEXT,
    decide_trigger TEXT,
    decide_started_at TEXT,
    decide_last_heartbeat_at TEXT,
    decide_token TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'regular',
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS steps (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    expect TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    close_reason TEXT,
    creator TEXT NOT NULL,
    worker TEXT,
    dispatch_count INTEGER NOT NULL DEFAULT 0,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS step_sources (
    step_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (step_id, project_id, fact_id),
    FOREIGN KEY (step_id, project_id) REFERENCES steps(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS findings (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS subgoals (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);
"""


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_legacy(conn)
        conn.execute(
            "INSERT OR IGNORE INTO settings (rowid, step_timeout, decide_timeout) VALUES (1, 15, 15)"
        )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """旧库（intents/reason_* 命名、confidence/evidence 审查字段）→ FGS v2。

    生产环境每轮全新建库，本迁移只服务本地开发库的平滑升级；
    步骤/事实数据全量保留，审查链遗产字段（confidence/evidence/challenged）丢弃。
    """
    # settings：列改名（老库才有旧列）
    settings_cols = _columns(conn, "settings")
    if "intent_timeout" in settings_cols:
        conn.execute("ALTER TABLE settings RENAME COLUMN intent_timeout TO step_timeout")
    if "reason_timeout" in settings_cols:
        conn.execute("ALTER TABLE settings RENAME COLUMN reason_timeout TO decide_timeout")

    # projects：decide 租约列改名
    project_cols = _columns(conn, "projects")
    renames = {
        "reason_worker": "decide_worker",
        "reason_trigger": "decide_trigger",
        "reason_started_at": "decide_started_at",
        "reason_last_heartbeat_at": "decide_last_heartbeat_at",
        "reason_token": "decide_token",
    }
    for old, new in renames.items():
        if old in project_cols:
            conn.execute(f"ALTER TABLE projects RENAME COLUMN {old} TO {new}")
    # 更老的库：bootstrap_enabled / bootstrap_mode 列补齐
    project_cols = _columns(conn, "projects")
    if "bootstrap_enabled" not in project_cols:
        conn.execute("ALTER TABLE projects ADD COLUMN bootstrap_enabled INTEGER NOT NULL DEFAULT 1")
        if "bootstrap_mode" in project_cols:
            conn.execute(
                "UPDATE projects SET bootstrap_enabled = CASE WHEN bootstrap_mode = 'disabled' THEN 0 ELSE 1 END"
            )

    # facts：丢弃审查链字段；summary kind 并入 regular（consolidate 已移除）
    fact_cols = _columns(conn, "facts")
    if "summary" not in fact_cols and "kind" not in fact_cols:
        conn.execute("ALTER TABLE facts ADD COLUMN kind TEXT NOT NULL DEFAULT 'regular'")
    for legacy_col in ("confidence", "evidence", "challenged"):
        if legacy_col in fact_cols:
            conn.execute(f"ALTER TABLE facts DROP COLUMN {legacy_col}")
    conn.execute("UPDATE facts SET kind = 'regular' WHERE kind = 'summary'")

    # intents → steps（数据搬迁，保留全部行）
    if _table_exists(conn, "intents"):
        conn.execute(
            """
            INSERT INTO steps (id, project_id, to_fact_id, description, creator, worker,
                               dispatch_count, last_heartbeat_at, created_at, concluded_at)
            SELECT id, project_id, to_fact_id, description, creator, worker,
                   dispatch_count, last_heartbeat_at, created_at, concluded_at
            FROM intents
            """
        )
        conn.execute(
            """
            INSERT INTO step_sources (step_id, project_id, fact_id)
            SELECT intent_id, project_id, fact_id FROM intent_sources
            """
        )
        conn.execute("DROP TABLE intent_sources")
        conn.execute("DROP TABLE intents")

    # steps 新列补齐（防中途版本库）
    step_cols = _columns(conn, "steps")
    if "expect" not in step_cols:
        conn.execute("ALTER TABLE steps ADD COLUMN expect TEXT")
    if "status" not in step_cols:
        conn.execute("ALTER TABLE steps ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")
    if "close_reason" not in step_cols:
        conn.execute("ALTER TABLE steps ADD COLUMN close_reason TEXT")


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    # dispatcher 以线程池并发打 HTTP，sync 端点多连接并发写：显式拉长锁等待，
    # 避免默认 5s 超时后裸 OperationalError: database is locked → 500
    conn = sqlite3.connect(str(_db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
