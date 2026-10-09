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
    claim_step_atomic,
    conclude_step_atomic,
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
            closed_at=None,
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
        # 原子认领（守卫 UPDATE）：消灭并发双认领窗口；投入计数由 CASE 在写时原子判定
        claim_step_atomic(conn, project_id, step_id, body.worker)

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
    """Execute 收束（自证写回）：写新天枢 + 步骤落点，可携一条沿途 Finding。

    原子预留（conclude_step_atomic）抢写权 → 同事务内插 fact + 终写落点；
    并发败者整个请求回滚，不产生孤儿 fact。
    """
    with get_conn() as conn:
        check_project_active(conn, project_id)
        now = conclude_step_atomic(conn, project_id, step_id, body.worker)
        source_step = get_step_or_404(conn, project_id, step_id)
        is_strike = source_step["task_type"] == "strike"

        if body.finding_high_value and body.finding is None:
            raise HTTPException(422, "finding_high_value requires finding")
        if (body.reuse_fact_id is not None
                and (is_strike or body.finding is None)):
            raise HTTPException(422, "reuse_fact_id requires a finding on an Execute step")
        if is_strike:
            if (body.verification_status is None or body.verification_summary is None
                    or body.finding is not None or body.finding_high_value
                    or body.reuse_fact_id is not None):
                raise HTTPException(422, "Strike conclusion requires a verification status and summary only")
            finding_row = conn.execute(
                "SELECT * FROM findings WHERE id = ? AND project_id = ?",
                (source_step["finding_id"], project_id),
            ).fetchone()
            if (finding_row is None or finding_row["verification_status"] != "pending"
                    or finding_row["verification_step_id"] != step_id):
                raise HTTPException(409, "Strike step has no pending linked finding")
        elif body.verification_status is not None or body.verification_summary is not None:
            raise HTTPException(422, "Only Strike steps may update finding verification")

        if body.reuse_fact_id is None:
            fid = next_fact_id(conn, project_id)
            fact = Fact(id=fid, description=body.description, kind=body.kind)
            conn.execute(
                "INSERT INTO facts (id, project_id, description, kind) VALUES (?, ?, ?, ?)",
                (fid, project_id, body.description, body.kind),
            )
        else:
            if body.reuse_fact_id in ("origin", "goal"):
                raise HTTPException(422, "System facts cannot be reused as a step conclusion")
            fact_row = conn.execute(
                "SELECT * FROM facts WHERE id = ? AND project_id = ?",
                (body.reuse_fact_id, project_id),
            ).fetchone()
            if fact_row is None:
                raise HTTPException(404, f"Fact {body.reuse_fact_id} not found")
            fid = fact_row["id"]
            fact = Fact(**dict(fact_row))

        conn.execute(
            "UPDATE steps SET to_fact_id = ?, last_heartbeat_at = ?, concluded_at = ? WHERE id = ? AND project_id = ? AND worker = ?",
            (fid, now, now, step_id, project_id, body.worker),
        )

        finding: Finding | None = None
        if is_strike:
            cursor = conn.execute(
                """UPDATE findings SET verification_status = ?, verification_fact_id = ?,
                          verification_summary = ?
                   WHERE id = ? AND project_id = ? AND verification_status = 'pending'
                     AND verification_step_id = ?""",
                (body.verification_status, fid, body.verification_summary,
                 source_step["finding_id"], project_id, step_id),
            )
            if cursor.rowcount != 1:
                raise HTTPException(409, "Finding is no longer pending verification")
            finding_row = conn.execute(
                "SELECT * FROM findings WHERE id = ? AND project_id = ?",
                (source_step["finding_id"], project_id),
            ).fetchone()
            finding = Finding(**dict(finding_row))
        elif body.finding:
            existing = None
            if body.finding_high_value:
                normalized = " ".join(body.finding.casefold().split())
                pending = conn.execute(
                    """SELECT * FROM findings WHERE project_id = ? AND high_value = 1
                       AND verification_status = 'pending'""",
                    (project_id,),
                ).fetchall()
                existing = next(
                    (row for row in pending
                     if " ".join(row["description"].casefold().split()) == normalized),
                    None,
                )
            if existing is not None:
                finding = Finding(**dict(existing))
            else:
                finding_id = next_finding_id(conn, project_id)
                verification_step_id = next_step_id(conn, project_id) if body.finding_high_value else None
                conn.execute(
                    """INSERT INTO findings
                       (id, project_id, description, created_at, high_value,
                        verification_status, source_fact_id, source_step_id, verification_step_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (finding_id, project_id, body.finding, now, int(body.finding_high_value),
                     "pending" if body.finding_high_value else "not_requested",
                     fid, step_id, verification_step_id),
                )
                if verification_step_id is not None:
                    conn.execute(
                        """INSERT INTO steps
                           (id, project_id, description, expect, status, creator,
                            created_at, task_type, finding_id)
                           VALUES (?, ?, ?, ?, 'open', ?, ?, 'strike', ?)""",
                        (verification_step_id, project_id,
                         f"Independently verify high-value finding: {body.finding}",
                         "Confirm, refute, or report a blocked verification with evidence.",
                         body.worker, now, finding_id),
                    )
                    conn.execute(
                        "INSERT INTO step_sources (step_id, project_id, fact_id) VALUES (?, ?, ?)",
                        (verification_step_id, project_id, fid),
                    )
                finding_row = conn.execute(
                    "SELECT * FROM findings WHERE id = ? AND project_id = ?",
                    (finding_id, project_id),
                ).fetchone()
                finding = Finding(**dict(finding_row))

        updated = conn.execute(
            "SELECT * FROM steps WHERE id = ? AND project_id = ?",
            (step_id, project_id),
        ).fetchone()

        return ConcludeResponse(
            fact=fact,
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
        if row["task_type"] == "strike":
            raise HTTPException(409, "Strike steps require a verification conclusion")
        if row["to_fact_id"] is not None:
            raise HTTPException(409, "Step already concluded")
        if row["status"] == "closed":
            return step_to_model(conn, row, project_id)

        now = utcnow()
        conn.execute(
            "UPDATE steps SET status = 'closed', close_reason = ?, closed_at = ?, worker = NULL WHERE id = ? AND project_id = ?",
            (body.reason, now, step_id, project_id),
        )
        updated = conn.execute(
            "SELECT * FROM steps WHERE id = ? AND project_id = ?",
            (step_id, project_id),
        ).fetchone()
        return step_to_model(conn, updated, project_id)
