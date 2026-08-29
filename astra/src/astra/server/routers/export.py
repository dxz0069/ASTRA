import os
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from datetime import datetime
import yaml

from astra.server.db import get_conn
from astra.server.services import expire_decide_leases, expire_workers, get_project_or_404

router = APIRouter(tags=["export"])


def format_export_timestamp(value: str | None) -> str | None:
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _load_project_data(conn, project_id: str):
    expire_workers(conn, project_id)
    expire_decide_leases(conn, project_id)
    proj = get_project_or_404(conn, project_id)

    facts = conn.execute(
        "SELECT id, description, kind FROM facts WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    hints = conn.execute(
        "SELECT content, creator, created_at FROM hints WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    steps = conn.execute(
        "SELECT * FROM steps WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    findings = conn.execute(
        "SELECT * FROM findings WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    subgoals = conn.execute(
        "SELECT * FROM subgoals WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()

    sources_by_step = {}
    for s in steps:
        rows = conn.execute(
            "SELECT fact_id FROM step_sources WHERE step_id = ? AND project_id = ? ORDER BY rowid",
            (s["id"], project_id),
        ).fetchall()
        sources_by_step[s["id"]] = [r["fact_id"] for r in rows]

    return proj, facts, hints, steps, findings, subgoals, sources_by_step


def _export_yaml(conn, project_id: str) -> str:
    proj, facts, hints, steps, findings, subgoals, sources_by_step = _load_project_data(conn, project_id)

    origin_desc = ""
    goal_desc = ""
    for f in facts:
        if f["id"] == "origin":
            origin_desc = f["description"]
        elif f["id"] == "goal":
            goal_desc = f["description"]

    data: dict = {
        "project": {
            "title": proj["title"],
            "origin": origin_desc,
            "goal": goal_desc,
            "bootstrap_enabled": bool(proj["bootstrap_enabled"]),
        }
    }

    if hints:
        data["hints"] = [
            {
                "content": h["content"],
                "creator": h["creator"],
                "created_at": format_export_timestamp(h["created_at"]),
            }
            for h in hints
        ]

    data["facts"] = [
        {
            "id": f["id"],
            "description": f["description"],
            "kind": f["kind"],
        }
        for f in facts
    ]

    step_list = []
    for s in steps:
        entry: dict = {
            "from": sources_by_step.get(s["id"], []),
            "to": s["to_fact_id"],
            "description": s["description"],
            "status": s["status"],
            "creator": s["creator"],
            "worker": s["worker"],
            "created_at": format_export_timestamp(s["created_at"]),
            "concluded_at": format_export_timestamp(s["concluded_at"]),
        }
        if s["expect"]:
            entry["expect"] = s["expect"]
        if s["close_reason"]:
            entry["close_reason"] = s["close_reason"]
        step_list.append(entry)

    if step_list:
        data["steps"] = step_list

    if findings:
        data["findings"] = [
            {
                "id": f["id"],
                "description": f["description"],
                "created_at": format_export_timestamp(f["created_at"]),
            }
            for f in findings
        ]

    active_subgoals = [sg for sg in subgoals if sg["status"] == "active"]
    if active_subgoals:
        data["subgoals"] = [
            {"id": sg["id"], "description": sg["description"]}
            for sg in active_subgoals
        ]

    # 大图防护——事实数超阈值时拒绝导出（防 yaml.dump 全量序列化 OOM）
    max_export_facts = int(os.environ.get("ASTRA_MAX_EXPORT_FACTS", "10000"))
    if len(facts) > max_export_facts:
        raise HTTPException(
            status_code=413,
            detail=f"Project too large to export ({len(facts)} facts > {max_export_facts} limit)",
        )
    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _export_timeline(conn, project_id: str) -> str:
    proj, facts, hints, steps, findings, subgoals, sources_by_step = _load_project_data(conn, project_id)

    facts_by_id = {f["id"]: f["description"] for f in facts}

    events: list[tuple[str, int, str]] = []  # (timestamp, order, text)
    order = 0

    origin_desc = facts_by_id.get("origin", "")
    goal_desc = facts_by_id.get("goal", "")
    ts = format_export_timestamp(proj["created_at"]) or ""
    block = f"[{ts}] PROJECT CREATED\n  origin: {origin_desc}\n  goal: {goal_desc}"
    events.append((proj["created_at"] or "", order, block))
    order += 1

    for h in hints:
        ts = format_export_timestamp(h["created_at"]) or ""
        block = f"[{ts}] HINT by {h['creator']}\n  {h['content']}"
        events.append((h["created_at"] or "", order, block))
        order += 1

    for sg in subgoals:
        ts = format_export_timestamp(sg["created_at"]) or ""
        block = f"[{ts}] SUBGOAL {sg['id']} ({sg['status']})\n  {sg['description']}"
        events.append((sg["created_at"] or "", order, block))
        order += 1

    for s in steps:
        src = sources_by_step.get(s["id"], [])
        from_str = ", ".join(src)

        ts = format_export_timestamp(s["created_at"]) or ""
        meta = f"  from: {from_str}"
        if s["worker"] and not s["concluded_at"]:
            meta += f"\n  worker: {s['worker']} (in progress)"
        block = f"[{ts}] STEP DECLARED {s['id']} by {s['creator']}\n{meta}\n  {s['description']}"
        events.append((s["created_at"] or "", order, block))
        order += 1

        if s["status"] == "closed" and s["close_reason"] and not s["to_fact_id"]:
            ts = format_export_timestamp(s["created_at"] or "") or ""
            block = f"[{ts}] STEP CLOSED {s['id']}\n  reason: {s['close_reason']}"
            events.append((s["created_at"] or "", order, block))
            order += 1

        if not s["concluded_at"] or not s["to_fact_id"]:
            continue

        ts = format_export_timestamp(s["concluded_at"]) or ""
        actor = s["worker"] or s["creator"]

        if s["to_fact_id"] == "goal":
            block = f"[{ts}] PROJECT COMPLETED by {actor}\n  via: {s['id']} from {from_str}"
        else:
            fact_desc = facts_by_id.get(s["to_fact_id"], "")
            block = f"[{ts}] STEP CONCLUDED {s['id']} by {actor}\n  from: {from_str}\n  produced: {s['to_fact_id']}\n  {fact_desc}"
        events.append((s["concluded_at"] or "", order, block))
        order += 1

    for f in findings:
        ts = format_export_timestamp(f["created_at"]) or ""
        block = f"[{ts}] FINDING {f['id']}\n  {f['description']}"
        events.append((f["created_at"] or "", order, block))
        order += 1

    events.sort(key=lambda e: (e[0], e[1]))

    return "\n\n".join(e[2] for e in events) + "\n"


@router.get("/projects/{project_id}/export")
def export_project(project_id: str, format: str = "yaml"):
    if format not in ("yaml", "timeline"):
        raise HTTPException(400, "Supported formats: yaml, timeline")

    with get_conn() as conn:
        if format == "timeline":
            text = _export_timeline(conn, project_id)
        else:
            text = _export_yaml(conn, project_id)

        return Response(content=text, media_type="text/plain")
