from pathlib import Path

import click
import uvicorn

from astra.dispatcher.logging import configure_logging
from astra.dispatcher.scheduler.loop import DispatcherLoop
from astra.server import db


@click.group()
def main():
    """ASTRA - 星图导航引擎：面向 AI 攻防全链路的状态空间搜索与决策系统."""


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=8000, show_default=True, help="Bind port")
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
def serve(host: str, port: int, db_path: str, log_level: str, access_log: bool):
    """Start the ASTRA API server."""
    db.configure(Path(db_path))
    from astra.server.app import app

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
    )


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--once", is_flag=True, help="Run one scheduling iteration and exit")
@click.option(
    "--startup-healthcheck-only",
    is_flag=True,
    help="Run startup worker healthchecks and exit",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
def dispatch(config_path: Path, once: bool, startup_healthcheck_only: bool, log_level: str):
    """Run the ASTRA dispatcher."""
    configure_logging(log_level, bare=startup_healthcheck_only)
    loop = DispatcherLoop(config_path)
    try:
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@main.group()
def memory():
    """记忆系统观测：星图事实、知识库条目与经验复利统计."""


@memory.command()
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.option(
    "--kb",
    "kb_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path(__file__).resolve().parent.parent.parent.parent / "container" / "knowledge" / "challenge-approaches.md",
    show_default=True,
    help="知识库 markdown 路径",
)
def stats(db_path: str, kb_path: Path):
    """展示四层记忆全景：星图事实分布 / 摘要压缩 / 知识库规模 / 经验复利战绩."""
    import json
    import re
    import sqlite3

    db.configure(Path(db_path))
    click.echo(f"数据库：{db_path}")
    try:
        conn = sqlite3.connect(str(Path(db_path)))
        projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        facts_total = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        by_kind = conn.execute(
            "SELECT kind, COUNT(*) FROM facts GROUP BY kind ORDER BY COUNT(*) DESC"
        ).fetchall()
        conn.close()
        click.echo(f"项目数：{projects} ｜ 星记总数：{facts_total}")
        for kind, count in by_kind:
            label = "摘要星记（Epitome，压缩自旧星记）" if kind == "summary" else kind
            click.echo(f"  - {label}: {count}")
    except sqlite3.Error as exc:
        click.echo(f"  （数据库不可读：{exc}）")

    click.echo(f"\n知识库：{kb_path}")
    if kb_path.exists():
        raw = kb_path.read_text(encoding="utf-8")
        entries = re.findall(r"^## (.+?)（([a-z0-9-]+)）", raw, re.MULTILINE)
        click.echo(f"  已固化攻击链条目：{len(entries)}")
    else:
        click.echo("  （文件不存在）")

    stats_path = kb_path.parent / "memory-stats.json"
    click.echo(f"\n经验复利统计：{stats_path}")
    try:
        stats_data = json.loads(stats_path.read_text(encoding="utf-8"))
        total_hits = sum(int(e.get("hits", 0)) for e in stats_data.values())
        total_misses = sum(int(e.get("misses", 0)) for e in stats_data.values())
        used = total_hits + total_misses
        rate = f"{100 * total_hits / used:.0f}%" if used else "暂无数据"
        click.echo(f"  条目：{len(stats_data)} ｜ 注入后命中：{total_hits} ｜ 未命中：{total_misses} ｜ 命中率：{rate}")
        top = sorted(
            stats_data.items(),
            key=lambda kv: int(kv[1].get("hits", 0)) - int(kv[1].get("misses", 0)),
            reverse=True,
        )[:5]
        for code, entry in top:
            if int(entry.get("hits", 0)) or int(entry.get("misses", 0)):
                click.echo(
                    f"  - {entry.get('name', code)}：{entry.get('hits', 0)} 命中 / "
                    f"{entry.get('misses', 0)} 未命中（last {entry.get('last_used', '?')}）"
                )
    except (OSError, json.JSONDecodeError):
        click.echo("  （暂无统计——首轮复利数据在赛后生成）")


@memory.command()
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.argument("project", required=False, default="")
def trace(db_path: str, project: str):
    """决策链回放：该项目的航向（决策）与星记（结论）时间线——"AI 为什么走这条路"可审计。"""
    import sqlite3

    db.configure(Path(db_path))
    conn = sqlite3.connect(str(Path(db_path)))
    conn.row_factory = sqlite3.Row
    try:
        if project:
            rows = conn.execute(
                "SELECT id, title, status, created_at FROM projects "
                "WHERE id LIKE ? OR title LIKE ? ORDER BY created_at DESC LIMIT 5",
                (f"%{project}%", f"%{project}%"),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title, status, created_at FROM projects "
                "ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
        if not rows:
            click.echo(f"未匹配到项目：{project or '（最近无项目）'}")
            return
        if len(rows) > 1:
            click.echo("匹配到多个项目（取最新）：")
            for r in rows:
                click.echo(f"  {r['id']}  {r['title']}  [{r['status']}]")
        proj = rows[0]
        click.echo(f"\n项目：{proj['title']}（{proj['id']}，{proj['status']}，建于 {proj['created_at']}）")

        intents = conn.execute(
            "SELECT id, description, worker, created_at, concluded_at, to_fact_id, challenged "
            "FROM intents WHERE project_id = ? ORDER BY created_at",
            (proj["id"],),
        ).fetchall()
        facts = conn.execute(
            "SELECT id, description, kind, confidence, challenged "
            "FROM facts WHERE project_id = ? ORDER BY rowid",
            (proj["id"],),
        ).fetchall()

        click.echo(f"\n决策链（航向 {len(intents)} 条）——AI 为什么走这条路：")
        for it in intents:
            state = "已归航→" + (it["to_fact_id"] or "?") if it["concluded_at"] else "未归航"
            mark = " 〖被质询〗" if it["challenged"] else ""
            desc = (it["description"] or "").replace("\n", " ")[:120]
            click.echo(f"  [{it['created_at']}] {it['worker'] or '?'}: {desc} → {state}{mark}")

        click.echo(f"\n星记序列（事实 {len(facts)} 条，按写入序）：")
        for f in facts:
            marks = []
            if f["kind"] == "summary":
                marks.append("摘要")
            if f["challenged"]:
                marks.append("被质询")
            tag = f"（{'、'.join(marks)}）" if marks else ""
            desc = (f["description"] or "").replace("\n", " ")[:120]
            click.echo(f"  {f['id']} [{f['confidence']}]{tag} {desc}")
    finally:
        conn.close()
