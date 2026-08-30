from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

from astra.server.models import ProjectDecide, ProjectMeta, Step


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_project_id(conn: sqlite3.Connection) -> str:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'project'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'project'").fetchone()
    return f"proj_{row['value']:03d}"


def _next_scoped_id(
    conn: sqlite3.Connection, kind: str, prefix: str, project_id: str
) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO scoped_counters (project_id, kind, value) VALUES (?, ?, 0)",
        (project_id, kind),
    )
    conn.execute(
        "UPDATE scoped_counters SET value = value + 1 WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    )
    row = conn.execute(
        "SELECT value FROM scoped_counters WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    ).fetchone()
    assert row is not None
    return f"{prefix}{row['value']:03d}"


def next_fact_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "fact", "f", project_id)


def next_step_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "step", "s", project_id)


def next_hint_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "hint", "h", project_id)


def next_finding_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "finding", "fnd", project_id)


def next_subgoal_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "subgoal", "sg", project_id)


def create_fact(
    conn: sqlite3.Connection,
    project_id: str,
    description: str,
    kind: str = "regular",
    creator: str = "system",
) -> str:
    check_project_active(conn, project_id)
    fact_id = next_fact_id(conn, project_id)
    conn.execute(
        "INSERT INTO facts (id, project_id, description, kind) VALUES (?, ?, ?, ?)",
        (fact_id, project_id, description, kind),
    )
    return fact_id


def create_finding(conn: sqlite3.Connection, project_id: str, description: str) -> str:
    check_project_active(conn, project_id)
    finding_id = next_finding_id(conn, project_id)
    conn.execute(
        "INSERT INTO findings (id, project_id, description, created_at) VALUES (?, ?, ?, ?)",
        (finding_id, project_id, description, utcnow()),
    )
    return finding_id


def get_project_or_404(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def check_project_active(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "active":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_hint_writable(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] not in ("active", "stopped", "completed"):
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_completed(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "completed":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def validate_facts_exist(
    conn: sqlite3.Connection, project_id: str, fact_ids: list[str]
) -> None:
    for fid in fact_ids:
        row = conn.execute(
            "SELECT 1 FROM facts WHERE id = ? AND project_id = ?", (fid, project_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Fact {fid} not found")


def validate_goal_not_in_sources(fact_ids: list[str]) -> None:
    if "goal" in fact_ids:
        raise HTTPException(400, "goal cannot be used in from")


def validate_step_creator_worker(creator: str, worker: str | None) -> None:
    if worker is not None and worker != creator:
        raise HTTPException(400, "worker must be null or equal to creator")


def get_step_or_404(
    conn: sqlite3.Connection, project_id: str, step_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM steps WHERE id = ? AND project_id = ?",
        (step_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Step not found")
    return row


def claim_step_atomic(
    conn: sqlite3.Connection, project_id: str, step_id: str, worker: str
) -> None:
    """守卫式原子认领：WHERE 条件在写入时重估，消灭 SELECT-检查-UPDATE 的 TOCTOU 窗口。

    高并发下两个请求同时通过预检查时，只有第一个 UPDATE 生效（rowcount=1）；
    第二个 rowcount=0 → 重读行产出精确 409。认领即登记派发计数（CASE 原子判定）。
    """
    expire_workers(conn, project_id)  # 过期租约先清（语义与旧 get_claimable 对齐）
    now = utcnow()
    cursor = conn.execute(
        """
        UPDATE steps
        SET worker = ?,
            last_heartbeat_at = ?,
            dispatch_count = dispatch_count + (CASE WHEN worker IS NULL OR worker != ? THEN 1 ELSE 0 END)
        WHERE id = ? AND project_id = ?
          AND status = 'open'
          AND to_fact_id IS NULL
          AND (worker IS NULL OR worker = ?)
        """,
        (worker, now, worker, step_id, project_id, worker),
    )
    if cursor.rowcount == 0:
        row = get_step_or_404(conn, project_id, step_id)
        if row["status"] == "closed":
            raise HTTPException(409, "Step is closed")
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "Step already concluded")
        raise HTTPException(409, f"Step is currently claimed by {row['worker']}")


def conclude_step_atomic(
    conn: sqlite3.Connection, project_id: str, step_id: str, worker: str
) -> str:
    """原子收束预留：抢到写权（worker 钉为本请求）后返回 utcnow；失败抛 409。

    后续 fact 插入 + to_fact_id 终写与本预留同处一个请求事务（get_conn 统一
    commit/rollback），不存在孤儿数据。
    """
    expire_workers(conn, project_id)  # 过期租约先清（语义与旧 get_claimable 对齐）
    now = utcnow()
    cursor = conn.execute(
        """
        UPDATE steps
        SET worker = ?, last_heartbeat_at = ?
        WHERE id = ? AND project_id = ?
          AND status = 'open'
          AND to_fact_id IS NULL
          AND (worker IS NULL OR worker = ?)
        """,
        (worker, now, step_id, project_id, worker),
    )
    if cursor.rowcount == 0:
        row = get_step_or_404(conn, project_id, step_id)
        if row["status"] == "closed":
            raise HTTPException(409, "Step is closed")
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "Step already concluded")
        raise HTTPException(409, f"Step is currently claimed by {row['worker']}")
    return now


def claim_decide_atomic(
    conn: sqlite3.Connection, project_id: str, worker: str, trigger: str
) -> str:
    """原子 decide 认领（同图串行保证）：返回租约令牌；失败抛 409。"""
    now = utcnow()
    lease_token = __import__("secrets").token_hex(16)
    cursor = conn.execute(
        """
        UPDATE projects
        SET decide_worker = ?, decide_trigger = ?, decide_started_at = ?,
            decide_last_heartbeat_at = ?, decide_token = ?
        WHERE id = ? AND status = 'active'
          AND (decide_worker IS NULL OR decide_worker = ?)
        """,
        (worker, trigger, now, now, lease_token, project_id, worker),
    )
    if cursor.rowcount == 0:
        row = get_project_or_404(conn, project_id)
        if row["status"] != "active":
            raise HTTPException(403, f"Project is {row['status']}")
        raise HTTPException(409, f"Project decide is currently claimed by {row['decide_worker']}")
    return lease_token


def get_claimable_open_step_or_404(
    conn: sqlite3.Connection, project_id: str, step_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_step_or_404(conn, project_id, step_id)
    if row["status"] == "closed":
        raise HTTPException(409, "Step is closed")
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Step already concluded")
    if row["worker"] is not None and row["worker"] != worker:
        raise HTTPException(409, f"Step is currently claimed by {row['worker']}")
    return row


def get_releasable_open_step_or_404(
    conn: sqlite3.Connection, project_id: str, step_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_step_or_404(conn, project_id, step_id)
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Step already concluded")
    if row["worker"] is None:
        return row
    if row["worker"] != worker:
        raise HTTPException(409, f"Step is currently claimed by {row['worker']}")
    return row


def get_completion_step_or_409(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT * FROM steps WHERE project_id = ? AND to_fact_id = 'goal'",
        (project_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, "Completed project is missing its completion step")
    if len(rows) != 1:
        raise HTTPException(409, "Completed project has multiple completion steps")
    return rows[0]


def step_to_model(conn: sqlite3.Connection, row: sqlite3.Row, project_id: str) -> Step:
    sources = conn.execute(
        "SELECT fact_id FROM step_sources WHERE step_id = ? AND project_id = ? ORDER BY rowid",
        (row["id"], project_id),
    ).fetchall()
    return Step(
        id=row["id"],
        **{"from": [s["fact_id"] for s in sources]},
        to=row["to_fact_id"],
        description=row["description"],
        expect=row["expect"],
        status=row["status"],
        close_reason=row["close_reason"],
        closed_at=row["closed_at"],
        creator=row["creator"],
        worker=row["worker"],
        last_heartbeat_at=row["last_heartbeat_at"],
        dispatch_count=int(row["dispatch_count"] or 0),
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
    )


def build_steps(conn: sqlite3.Connection, project_id: str) -> list[Step]:
    rows = conn.execute(
        "SELECT * FROM steps WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return [step_to_model(conn, r, project_id) for r in rows]


def get_step_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT step_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["step_timeout"]


def get_decide_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT decide_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["decide_timeout"]


def project_decide_from_row(row: sqlite3.Row) -> ProjectDecide | None:
    if row["decide_worker"] is None:
        return None
    return ProjectDecide(
        worker=row["decide_worker"],
        trigger=row["decide_trigger"],
        started_at=row["decide_started_at"],
        last_heartbeat_at=row["decide_last_heartbeat_at"],
    )


def project_meta_from_row(row: sqlite3.Row) -> ProjectMeta:
    return ProjectMeta(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        bootstrap_enabled=bool(row["bootstrap_enabled"]),
        created_at=row["created_at"],
        decide=project_decide_from_row(row),
    )


def clear_project_decide(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        """
        UPDATE projects
        SET decide_worker = NULL,
            decide_trigger = NULL,
            decide_started_at = NULL,
            decide_last_heartbeat_at = NULL,
            decide_token = NULL
        WHERE id = ?
        """,
        (project_id,),
    )


def expire_workers(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_step_timeout(conn)
    now = utcnow()
    query = """
        UPDATE steps
        SET worker = NULL
        WHERE to_fact_id IS NULL
          AND status = 'open'
          AND worker IS NOT NULL
          AND last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE project_id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)


def expire_decide_leases(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_decide_timeout(conn)
    now = utcnow()
    query = """
        UPDATE projects
        SET decide_worker = NULL,
            decide_trigger = NULL,
            decide_started_at = NULL,
            decide_last_heartbeat_at = NULL,
            decide_token = NULL
        WHERE decide_worker IS NOT NULL
          AND decide_last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(decide_last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)
