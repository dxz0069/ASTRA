import sqlite3
from fastapi import APIRouter, HTTPException

from astra.server.db import get_conn
from astra.server.models import (
    CompleteRequest,
    CreateFactRequest,
    CreateFindingRequest,
    CreateProjectRequest,
    CreateStepRequest,
    CreateSubGoalRequest,
    DecideClaimRequest,
    Fact,
    Finding,
    HeartbeatRequest,
    Hint,
    ProjectDetail,
    ProjectMeta,
    ProjectSummary,
    ReopenRequest,
    ReopenResponse,
    Step,
    SubGoal,
    UpdateProjectTitleRequest,
    UpdateProjectStatusRequest,
    UpdateSubGoalStatusRequest,
)
from astra.server.services import (
    build_steps,
    claim_decide_atomic,
    check_project_active,
    check_project_completed,
    clear_project_decide,
    create_fact,
    create_finding,
    expire_decide_leases,
    expire_workers,
    get_completion_step_or_409,
    get_project_or_404,
    next_fact_id,
    next_hint_id,
    next_project_id,
    next_step_id,
    next_subgoal_id,
    project_meta_from_row,
    step_to_model,
    utcnow,
    validate_facts_exist,
    validate_goal_not_in_sources,
)

router = APIRouter(tags=["projects"])


def _load_findings(conn: sqlite3.Connection, project_id: str) -> list[Finding]:
    rows = conn.execute(
        "SELECT * FROM findings WHERE project_id = ? ORDER BY created_at", (project_id,)
    ).fetchall()
    return [Finding(**dict(r)) for r in rows]


def _load_subgoals(conn: sqlite3.Connection, project_id: str) -> list[SubGoal]:
    rows = conn.execute(
        "SELECT * FROM subgoals WHERE project_id = ? ORDER BY created_at", (project_id,)
    ).fetchall()
    return [SubGoal(**dict(r)) for r in rows]


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects():
    with get_conn() as conn:
        expire_workers(conn)
        expire_decide_leases(conn)
        rows = conn.execute("""
            SELECT p.*,
                (SELECT COUNT(*) FROM facts WHERE project_id = p.id) AS fact_count,
                (SELECT COUNT(*) FROM steps WHERE project_id = p.id) AS step_count,
                (SELECT COUNT(*) FROM steps WHERE project_id = p.id AND concluded_at IS NULL AND status = 'open' AND worker IS NOT NULL) AS working_step_count,
                (SELECT COUNT(*) FROM steps WHERE project_id = p.id AND concluded_at IS NULL AND status = 'open' AND worker IS NULL) AS unclaimed_step_count,
                (SELECT COUNT(*) FROM hints WHERE project_id = p.id) AS hint_count,
                (SELECT COUNT(*) FROM findings WHERE project_id = p.id) AS finding_count
            FROM projects p
            ORDER BY p.created_at
        """).fetchall()
        return [
            ProjectSummary(
                id=row["id"],
                title=row["title"],
                status=row["status"],
                bootstrap_enabled=bool(row["bootstrap_enabled"]),
                created_at=row["created_at"],
                decide=project_meta_from_row(row).decide,
                fact_count=row["fact_count"],
                step_count=row["step_count"],
                working_step_count=row["working_step_count"],
                unclaimed_step_count=row["unclaimed_step_count"],
                hint_count=row["hint_count"],
                finding_count=row["finding_count"],
            )
            for row in rows
        ]


@router.post("/projects", response_model=ProjectDetail, status_code=201)
def create_project(body: CreateProjectRequest):
    with get_conn() as conn:
        pid = next_project_id(conn)
        now = utcnow()

        conn.execute(
            "INSERT INTO projects (id, title, status, bootstrap_enabled, created_at) VALUES (?, ?, 'active', ?, ?)",
            (pid, body.title, body.bootstrap_enabled, now),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            ("origin", pid, body.origin),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            ("goal", pid, body.goal),
        )

        hints = []
        if body.hints:
            for h in body.hints:
                hid = next_hint_id(conn, pid)
                conn.execute(
                    "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                    (hid, pid, h.content, h.creator, now),
                )
                hints.append(Hint(id=hid, content=h.content, creator=h.creator, created_at=now))

        return ProjectDetail(
            project=ProjectMeta(
                id=pid,
                title=body.title,
                status="active",
                bootstrap_enabled=body.bootstrap_enabled,
                created_at=now,
                decide=None,
            ),
            facts=[
                Fact(id="origin", description=body.origin),
                Fact(id="goal", description=body.goal),
            ],
            steps=[],
            hints=hints,
            findings=[],
            subgoals=[],
        )


@router.post("/projects/{project_id}/facts", response_model=Fact, status_code=201)
def add_fact(project_id: str, body: CreateFactRequest):
    """写入一条新事实（外部注入，如平台回执/运维 hint 同级数据）。"""
    with get_conn() as conn:
        fact_id = create_fact(conn, project_id, body.description, body.kind)
        return Fact(id=fact_id, description=body.description, kind=body.kind)


@router.post("/projects/{project_id}/findings", response_model=Finding, status_code=201)
def add_finding(project_id: str, body: CreateFindingRequest):
    """写入一条沿途发现（Finding）——搜索过程的产出物，与 Goal 终点相对。"""
    with get_conn() as conn:
        finding_id = create_finding(conn, project_id, body.description)
        row = conn.execute(
            "SELECT * FROM findings WHERE id = ? AND project_id = ?",
            (finding_id, project_id),
        ).fetchone()
        return Finding(**dict(row))


@router.post("/projects/{project_id}/subgoals", response_model=SubGoal, status_code=201)
def add_subgoal(project_id: str, body: CreateSubGoalRequest):
    """Decide 新增动态 Sub Goal（阶段性里程碑）。"""
    with get_conn() as conn:
        check_project_active(conn, project_id)
        sgid = next_subgoal_id(conn, project_id)
        now = utcnow()
        conn.execute(
            "INSERT INTO subgoals (id, project_id, description, status, created_at) VALUES (?, ?, ?, 'active', ?)",
            (sgid, project_id, body.description, now),
        )
        return SubGoal(id=sgid, description=body.description, status="active", created_at=now)


@router.post("/projects/{project_id}/subgoals/{subgoal_id}/status", response_model=SubGoal)
def update_subgoal_status(project_id: str, subgoal_id: str, body: UpdateSubGoalStatusRequest):
    """Decide 更新 Sub Goal 状态（done/dropped/active）。"""
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = conn.execute(
            "SELECT * FROM subgoals WHERE id = ? AND project_id = ?",
            (subgoal_id, project_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "SubGoal not found")
        conn.execute(
            "UPDATE subgoals SET status = ? WHERE id = ? AND project_id = ?",
            (body.status, subgoal_id, project_id),
        )
        updated = conn.execute(
            "SELECT * FROM subgoals WHERE id = ? AND project_id = ?",
            (subgoal_id, project_id),
        ).fetchone()
        return SubGoal(**dict(updated))


@router.get("/projects/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str):
    with get_conn() as conn:
        expire_workers(conn, project_id)
        expire_decide_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)

        facts = conn.execute(
            "SELECT * FROM facts WHERE project_id = ?", (project_id,)
        ).fetchall()
        hints = conn.execute(
            "SELECT * FROM hints WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()

        return ProjectDetail(
            project=project_meta_from_row(row),
            facts=[Fact(**dict(f)) for f in facts],
            steps=build_steps(conn, project_id),
            hints=[Hint(**dict(h)) for h in hints],
            findings=_load_findings(conn, project_id),
            subgoals=_load_subgoals(conn, project_id),
        )


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


@router.put("/projects/{project_id}/title", response_model=ProjectMeta)
def update_project_title(project_id: str, body: UpdateProjectTitleRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute(
            "UPDATE projects SET title = ? WHERE id = ?",
            (body.title, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.put("/projects/{project_id}/status", response_model=ProjectMeta)
def update_project_status(project_id: str, body: UpdateProjectStatusRequest):
    with get_conn() as conn:
        expire_decide_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_status = row["status"]
        if current_status == "completed":
            raise HTTPException(409, "Completed projects cannot change status")
        if current_status == body.status:
            return project_meta_from_row(row)

        conn.execute(
            "UPDATE projects SET status = ? WHERE id = ?",
            (body.status, project_id),
        )
        if body.status == "stopped":
            conn.execute(
                "UPDATE steps SET worker = NULL WHERE project_id = ? AND concluded_at IS NULL",
                (project_id,),
            )
            clear_project_decide(conn, project_id)
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


def _verify_lease_token(row: sqlite3.Row, provided: str | None) -> None:
    """租约令牌校验：行内 token 存在时必须精确匹配（旧行 NULL 跳过=平滑过渡）。"""
    stored = row["decide_token"] if "decide_token" in row.keys() else None
    if stored and stored != (provided or ""):
        raise HTTPException(403, "Invalid lease token for this decide lease")


@router.post("/projects/{project_id}/decide/claim", response_model=ProjectMeta)
def claim_project_decide(project_id: str, body: DecideClaimRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_decide_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        if row["decide_worker"] == body.worker:
            # 幂等重认领：回传已持有的租约令牌（否则调用方拿 None token，
            # 后续 heartbeat/complete 全 403 直到租约过期）
            meta = project_meta_from_row(row)
            stored = row["decide_token"] if "decide_token" in row.keys() else None
            meta.decide_token = stored or None
            return meta

        # 原子认领（守卫 UPDATE）：并发穿透时败者 409；胜者下发新令牌
        lease_token = claim_decide_atomic(conn, project_id, body.worker, body.trigger)
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        meta = project_meta_from_row(updated)
        meta.decide_token = lease_token  # 仅 claim 响应下发；其余端点不回显
        return meta


@router.post("/projects/{project_id}/decide/heartbeat", response_model=ProjectMeta)
def heartbeat_project_decide(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_decide_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["decide_worker"]
        if current_worker is None:
            raise HTTPException(409, "Project decide is not currently claimed")
        if current_worker != body.worker:
            raise HTTPException(409, f"Project decide is currently claimed by {current_worker}")
        _verify_lease_token(row, body.lease_token)

        now = utcnow()
        conn.execute(
            "UPDATE projects SET decide_last_heartbeat_at = ? WHERE id = ?",
            (now, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/decide/release", response_model=ProjectMeta)
def release_project_decide(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_decide_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["decide_worker"]
        if current_worker is None:
            return project_meta_from_row(row)
        if current_worker != body.worker:
            raise HTTPException(409, f"Project decide is currently claimed by {current_worker}")
        _verify_lease_token(row, body.lease_token)

        clear_project_decide(conn, project_id)
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/complete", response_model=Step)
def complete_project(project_id: str, body: CompleteRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_decide_leases(conn, project_id)
        # 完成依据不得全为系统事实——origin 对每个项目必然存在，
        # 纯 origin 完成等于零发现强制完成任意项目；真实事实与 origin 混列不拦
        if all(fid in ("origin", "goal") for fid in body.from_):
            raise HTTPException(422, "from_ cannot reference only system facts (origin/goal)")
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)
        # 存在活跃 decide 租约时仅持有者可完成——防越权清租约连锁取消在途任务
        row = get_project_or_404(conn, project_id)
        live_holder = row["decide_worker"]
        if live_holder is not None and live_holder != body.worker:
            raise HTTPException(403, f"Project decide lease is held by {live_holder}")
        if live_holder is not None:
            _verify_lease_token(row, body.lease_token)

        now = utcnow()
        sid = next_step_id(conn, project_id)

        conn.execute(
            "INSERT INTO steps (id, project_id, to_fact_id, description, status, creator, worker, last_heartbeat_at, created_at, concluded_at) "
            "VALUES (?, ?, 'goal', ?, 'closed', ?, ?, ?, ?, ?)",
            (sid, project_id, body.description, body.worker, body.worker, now, now, now),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO step_sources (step_id, project_id, fact_id) VALUES (?, ?, ?)",
                (sid, project_id, fid),
            )
        conn.execute(
            """
            UPDATE projects
            SET status = 'completed',
                decide_worker = NULL,
                decide_trigger = NULL,
                decide_started_at = NULL,
                decide_last_heartbeat_at = NULL
            WHERE id = ?
            """,
            (project_id,),
        )

        return Step(
            id=sid,
            **{"from": body.from_},
            to="goal",
            description=body.description,
            status="closed",
            creator=body.worker,
            worker=body.worker,
            last_heartbeat_at=now,
            created_at=now,
            concluded_at=now,
        )


@router.post("/projects/{project_id}/reopen", response_model=ReopenResponse)
def reopen_project(project_id: str, body: ReopenRequest):
    with get_conn() as conn:
        expire_decide_leases(conn, project_id)
        check_project_completed(conn, project_id)
        completion = get_completion_step_or_409(conn, project_id)

        source_rows = conn.execute(
            "SELECT fact_id FROM step_sources WHERE step_id = ? AND project_id = ? ORDER BY rowid",
            (completion["id"], project_id),
        ).fetchall()
        source_ids = [row["fact_id"] for row in source_rows]
        if not source_ids:
            raise HTTPException(409, "Completion step is missing its source facts")

        now = utcnow()
        fact_id = next_fact_id(conn, project_id)
        step_id = next_step_id(conn, project_id)

        conn.execute(
            "DELETE FROM steps WHERE id = ? AND project_id = ?",
            (completion["id"], project_id),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fact_id, project_id, body.description),
        )
        conn.execute(
            "INSERT INTO steps (id, project_id, to_fact_id, description, status, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
            (step_id, project_id, fact_id, "external_feedback", body.creator, body.creator, now, now, now),
        )
        for source_id in source_ids:
            conn.execute(
                "INSERT INTO step_sources (step_id, project_id, fact_id) VALUES (?, ?, ?)",
                (step_id, project_id, source_id),
            )
        clear_project_decide(conn, project_id)
        conn.execute(
            "UPDATE projects SET status = 'active' WHERE id = ?",
            (project_id,),
        )

        updated_project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        updated_step = conn.execute(
            "SELECT * FROM steps WHERE id = ? AND project_id = ?",
            (step_id, project_id),
        ).fetchone()
        assert updated_project is not None
        assert updated_step is not None
        return ReopenResponse(
            project=project_meta_from_row(updated_project),
            fact=Fact(id=fact_id, description=body.description),
            step=step_to_model(conn, updated_step, project_id),
        )
