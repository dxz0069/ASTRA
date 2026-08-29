from fastapi import APIRouter, HTTPException

from astra.server.db import get_conn
from astra.server.models import (
    CloseStepRequest,
    ConcludeRequest,
    ConcludeResponse,
    CreateStepRequest,
    Fact,
    Finding,
    HeartbeatRequest,
    Step,
)
from astra.server.services import (
    check_project_active,
    get_claimable_open_step_or_404,
    get_releasable_open_step_or_404,
    get_step_or_404,
    next_fact_id,
    next_finding_id,
    next_step_id,
    step_to_model,
    utcnow,
    validate_facts_exist,
    validate_goal_not_in_sources,
    validate_step_creator_worker,
)

router = APIRouter(tags=["steps"])


@router.post(
    "/projects/{project_id}/steps",
    response_model=Step,
    status_code=201,
)
def create_step(project_id: str, body: CreateStepRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)
        validate_step_creator_worker(body.creator, body.worker)

        now = utcnow()
        sid = next_step_id(conn, project_id)
        claimed = body.worker is not None
        conn.execute(
            "INSERT INTO steps (id, project_id, to_fact_id, description, expect, status, creator, worker, last_heartbeat_at, created_at) "
            "VALUES (?, ?, NULL, ?, ?, 'open', ?, ?, ?, ?)",
            (
                sid,
                project_id,
                body.description,
                body.expect,
                body.creator,
                body.worker,
                now if claimed else None,
                now,
            ),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO step_sources (step_id, project_id, fact_id) VALUES (?, ?, ?)",
                (sid, project_id, fid),
            )

        return Step(
            id=sid,
            **{"from": body.from_},
            to=None,
            description=body.description,
            expect=body.expect,
            status="open",
            creator=body.creator,
            worker=body.worker,
            last_heartbeat_at=now if claimed else None,
            created_at=now,
        )


@router.post(
    "/projects/{project_id}/steps/{step_id}/heartbeat",
    response_model=Step,
)
def heartbeat(project_id: str, step_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        get_claimable_open_step_or_404(conn, project_id, step_id, body.worker)

        now = utcnow()
        prev = conn.execute(
            "SELECT worker FROM steps WHERE id = ? AND project_id = ?",
            (step_id, project_id),
        ).fetchone()
        # 投入卡：首次认领/换 worker 认领记一次派发（同 worker 心跳续租不计数）
        claim_dispatch = 1 if prev is None or prev["worker"] is None or prev["worker"] != body.worker else 0
        conn.execute(
            "UPDATE steps SET worker = ?, last_heartbeat_at = ?, dispatch_count = dispatch_count + ? "
            "WHERE id = ? AND project_id = ?",
            (body.worker, now, claim_dispatch, step_id, project_id),
        )

        updated = conn.execute(
            "SELECT * FROM steps WHERE id = ? AND project_id = ?",
            (step_id, project_id),
        ).fetchone()
        return step_to_model(conn, updated, project_id)


@router.post(
    "/projects/{project_id}/steps/{step_id}/release",
    response_model=Step,
)
def release(project_id: str, step_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = get_releasable_open_step_or_404(conn, project_id, step_id, body.worker)

        if row["worker"] == body.worker:
            conn.execute(
                "UPDATE steps SET worker = NULL WHERE id = ? AND project_id = ?",
                (step_id, project_id),
            )
            row = conn.execute(
                "SELECT * FROM steps WHERE id = ? AND project_id = ?",
                (step_id, project_id),
            ).fetchone()

        return step_to_model(conn, row, project_id)


@router.post(
    "/projects/{project_id}/steps/{step_id}/conclude",
    response_model=ConcludeResponse,
)
def conclude(project_id: str, step_id: str, body: ConcludeRequest):
    """Execute 收束：submit_fact——写新星记 + 步骤落点，可携一条沿途 Finding。"""
    with get_conn() as conn:
        check_project_active(conn, project_id)
        get_claimable_open_step_or_404(conn, project_id, step_id, body.worker)

        now = utcnow()
        fid = next_fact_id(conn, project_id)

        conn.execute(
            "INSERT INTO facts (id, project_id, description, kind) VALUES (?, ?, ?, ?)",
            (fid, project_id, body.description, body.kind),
        )
        conn.execute(
            "UPDATE steps SET to_fact_id = ?, worker = ?, last_heartbeat_at = ?, concluded_at = ? WHERE id = ? AND project_id = ?",
            (fid, body.worker, now, now, step_id, project_id),
        )

        finding: Finding | None = None
        if body.finding:
            finding_id = next_finding_id(conn, project_id)
            conn.execute(
                "INSERT INTO findings (id, project_id, description, created_at) VALUES (?, ?, ?, ?)",
                (finding_id, project_id, body.finding, now),
            )
            finding = Finding(id=finding_id, description=body.finding, created_at=now)

        updated = conn.execute(
            "SELECT * FROM steps WHERE id = ? AND project_id = ?",
            (step_id, project_id),
        ).fetchone()

        return ConcludeResponse(
            fact=Fact(id=fid, description=body.description, kind=body.kind),
            step=step_to_model(conn, updated, project_id),
            finding=finding,
        )


@router.post(
    "/projects/{project_id}/steps/{step_id}/close",
    response_model=Step,
)
def close_step(project_id: str, step_id: str, body: CloseStepRequest):
    """Decide 关闭步骤：留痕（close_reason）防重开死路；append-only 保留行。"""
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = get_step_or_404(conn, project_id, step_id)
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "Step already concluded")
        if row["status"] == "closed":
            return step_to_model(conn, row, project_id)

        conn.execute(
            "UPDATE steps SET status = 'closed', close_reason = ?, worker = NULL WHERE id = ? AND project_id = ?",
            (body.reason, step_id, project_id),
        )
        updated = conn.execute(
            "SELECT * FROM steps WHERE id = ? AND project_id = ?",
            (step_id, project_id),
        ).fetchone()
        return step_to_model(conn, updated, project_id)
