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


@memory.command("map")
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.argument("project", required=False, default="")
@click.option("--out", type=click.Path(path_type=Path), default=Path("astra-star-map.html"), show_default=True, help="输出 HTML 路径")
def map_(db_path: str, project: str, out: Path):
    """星图可视化：生成单文件 HTML（内联 SVG，无外部依赖）——事实为星、航向为轨迹、质询红标。"""
    import html as _html
    import sqlite3

    db.configure(Path(db_path))
    conn = sqlite3.connect(str(Path(db_path)))
    conn.row_factory = sqlite3.Row
    try:
        if project:
            proj = conn.execute(
                "SELECT id, title, status, created_at FROM projects WHERE id LIKE ? OR title LIKE ? "
                "ORDER BY created_at DESC LIMIT 1",
                (f"%{project}%", f"%{project}%"),
            ).fetchone()
        else:
            proj = conn.execute(
                "SELECT id, title, status, created_at FROM projects ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not proj:
            click.echo("未匹配到项目")
            return
        facts = conn.execute(
            "SELECT id, description, kind, confidence, challenged FROM facts "
            "WHERE project_id = ? ORDER BY rowid",
            (proj["id"],),
        ).fetchall()
        intents = conn.execute(
            "SELECT id, description, worker, to_fact_id, concluded_at, challenged "
            "FROM intents WHERE project_id = ? ORDER BY created_at",
            (proj["id"],),
        ).fetchall()

        # 布局：星记按写入序从左到右蛇形铺开，航向画弧线指向归航星记
        cols, node_w, node_h, gap_x, gap_y = 6, 150, 70, 40, 30
        rows = max((len(facts) + cols - 1) // cols, 1)
        width = cols * (node_w + gap_x) + gap_x
        height = rows * (node_h + gap_y) + gap_y + 200
        pos: dict[str, tuple[int, int]] = {}
        cells = []
        for i, f in enumerate(facts):
            r, c = divmod(i, cols)
            x = gap_x + c * (node_w + gap_x)
            y = 160 + r * (node_h + gap_y)
            pos[f["id"]] = (x + node_w // 2, y)
            color = "#f59e0b" if f["id"] == "goal" else ("#94a3b8" if f["kind"] == "summary" else "#38bdf8")
            stroke = "#ef4444" if f["challenged"] else "#1e293b"
            dash = ' stroke-dasharray="4,3"' if f["kind"] == "summary" else ""
            desc = _html.escape((f["description"] or "")[:80])
            cells.append(
                f'<g class="node"><title>{_html.escape(f["description"] or "")}</title>'
                f'<rect x="{x}" y="{y}" width="{node_w}" height="{node_h}" rx="8" fill="{color}22" '
                f'stroke="{stroke}" stroke-width="{2 if f["challenged"] else 1}"{dash}/>'
                f'<text x="{x + 8}" y="{y + 20}" font-size="12" font-weight="bold" fill="#0f172a">{f["id"]}</text>'
                f'<text x="{x + 8}" y="{y + 38}" font-size="10" fill="#334155">{desc}</text>'
                f'<text x="{x + 8}" y="{y + 56}" font-size="9" fill="#64748b">{f["kind"]}/{f["confidence"]}'
                + (' <tspan fill="#ef4444">质询</tspan>' if f["challenged"] else "")
                + "</text></g>"
            )
        links = []
        for it in intents:
            if it["to_fact_id"] and it["to_fact_id"] in pos:
                tx, ty = pos[it["to_fact_id"]]
                # 从星记上方弧线进入（简化：全部从画布顶部的航向泳道出发）
                lane_y = 110
                color = "#ef4444" if it["challenged"] else "#22c55e" if it["concluded_at"] else "#a78bfa"
                links.append(
                    f'<path d="M {width // 2} {lane_y} Q {tx} {ty - 80} {tx} {ty}" fill="none" '
                    f'stroke="{color}" stroke-width="1.5" stroke-opacity="0.6">'
                    f"<title>{_html.escape(it['description'] or '')}</title></path>"
                )
        doc = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>ASTRA 星图 · {_html.escape(proj['title'])}</title><style>
body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:20px}}
h1{{font-size:18px}} .meta{{color:#94a3b8;font-size:13px;margin-bottom:8px}}
svg{{background:#1e293b;border-radius:12px}} text{{font-family:system-ui}}
.node:hover rect{{stroke-width:3;cursor:pointer}}
.legend span{{margin-right:16px;font-size:12px}}</style></head><body>
<h1>ASTRA 星图 · {_html.escape(proj['title'])}</h1>
<div class="meta">{proj['id']} ｜ {proj['status']} ｜ 建于 {proj['created_at']} ｜ 星记 {len(facts)} 条 ｜ 航向 {len(intents)} 条</div>
<div class="legend"><span>🟦 星记</span><span>🟨 目标</span><span>⬜ 摘要(Epitome)</span><span>🟥 边框=被质询</span><span>绿线=已归航航向</span><span>紫线=未归航</span><span>红线=被质询航向</span></div>
<svg width="{width}" height="{height}">{''.join(links)}{''.join(cells)}</svg>
</body></html>"""
        out.write_text(doc, encoding="utf-8")
        click.echo(f"星图已生成：{out.resolve()}（星记 {len(facts)}，航向 {len(intents)}）")
    finally:
        conn.close()


@main.command()
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.argument("project", required=False, default="")
@click.option("--out", type=click.Path(path_type=Path), default=None, help="输出 markdown 路径（默认 <project>-report.md）")
def report(db_path: str, project: str, out: Path):
    """渗透测试报告：星图渲染为中文报告（概要/风险清单/复现路径/证据链）——甲方交付物。"""
    import html as _html
    import re as _re
    import sqlite3

    db.configure(Path(db_path))
    conn = sqlite3.connect(str(Path(db_path)))
    conn.row_factory = sqlite3.Row
    try:
        if project:
            proj = conn.execute(
                "SELECT id, title, status, created_at FROM projects WHERE id LIKE ? OR title LIKE ? "
                "ORDER BY created_at DESC LIMIT 1",
                (f"%{project}%", f"%{project}%"),
            ).fetchone()
        else:
            proj = conn.execute(
                "SELECT id, title, status, created_at FROM projects ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not proj:
            click.echo("未匹配到项目")
            return
        facts = conn.execute(
            "SELECT id, description, kind, confidence, evidence, challenged "
            "FROM facts WHERE project_id = ? ORDER BY rowid",
            (proj["id"],),
        ).fetchall()
        intents = conn.execute(
            "SELECT id, description, worker, to_fact_id, created_at, concluded_at, challenged "
            "FROM intents WHERE project_id = ? ORDER BY created_at",
            (proj["id"],),
        ).fetchall()
    finally:
        conn.close()

    # 风险评级启发式：关键词 + 置信度（high 置信的 RCE/凭据/注入类发现升高危）
    HIGH_KWS = ("rce", "远程命令", "任意命令", "凭据", "密码", "私钥", "ak/sk", "token", "getshell", "webshell", "反弹")
    MED_KWS = ("注入", "ssrf", "xss", "越权", "上传", "反序列化", "穿越", "泄露", "sqli")

    def _risk(desc: str, confidence: str) -> str:
        low = desc.lower()
        if any(k in low for k in HIGH_KWS) and confidence == "high":
            return "高危"
        if any(k in low for k in HIGH_KWS) or (any(k in low for k in MED_KWS) and confidence == "high"):
            return "中危"
        if any(k in low for k in MED_KWS):
            return "低危"
        return "信息"

    findings = [f for f in facts if f["id"] != "goal" and f["kind"] != "summary"]
    risk_counts = {"高危": 0, "中危": 0, "低危": 0, "信息": 0}
    for f in findings:
        risk_counts[_risk(f["description"], f["confidence"])] += 1

    lines = [
        f"# 渗透测试报告 · {_html.escape(proj['title'])}",
        "",
        f"> 生成时间：{__import__('datetime').datetime.now().isoformat(timespec='seconds')} ｜ "
        f"数据来源：ASTRA 星图（{proj['id']}，{proj['status']}） ｜ "
        f"证据链：星记 {len(facts)} 条 / 航向 {len(intents)} 条",
        "",
        "## 一、测试概要",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 测试目标 | {_html.escape(proj['title'])} |",
        f"| 发现总数 | {len(findings)} |",
        f"| 高危 / 中危 / 低危 / 信息 | {risk_counts['高危']} / {risk_counts['中危']} / {risk_counts['低危']} / {risk_counts['信息']} |",
        "",
    ]
    goal = next((f["description"] for f in facts if f["id"] == "goal"), "")
    if goal:
        lines += ["**测试任务**：" + _html.escape(goal[:200]), ""]

    lines += ["## 二、风险发现清单", "", "| 编号 | 风险等级 | 置信度 | 描述 |", "|---|---|---|---|"]
    for i, f in enumerate(findings, 1):
        desc = _html.escape((f["description"] or "").replace("\n", " ")[:160])
        mark = " ⚠被质询" if f["challenged"] else ""
        lines.append(f"| {f['id']} | {_risk(f['description'], f['confidence'])} | {f['confidence']}{mark} | {desc} |")
    lines.append("")

    lines += ["## 三、攻击路径（决策链回放）", ""]
    for it in intents:
        state = "已归航→" + (it["to_fact_id"] or "?") if it["concluded_at"] else "探索中"
        mark = " 〖被质询否决〗" if it["challenged"] else ""
        desc = _html.escape((it["description"] or "").replace("\n", " ")[:140])
        lines.append(f"- `[{it['created_at']}]` {desc} → {state}{mark}")
    lines.append("")

    evidenced = [f for f in findings if (f["evidence"] or "").strip()]
    if evidenced:
        lines += ["## 四、证据链", ""]
        for f in evidenced:
            ev = _html.escape(f["evidence"][:400])
            lines += [f"**{f['id']}**：", "", "```", ev, "```", ""]
    lines += [
        "## 五、修复建议",
        "",
        "- 高危发现优先处置：限制相关端点访问权限、轮换已泄漏凭据/密钥；",
        "- 中危发现按影响面排期修复，补充输入校验与权限校验；",
        "- 建议针对本次攻击路径中未授权访问的入口做专项加固，并复测验证。",
        "",
        "---",
        "*本报告由 ASTRA 星尘记忆系统自动生成，星记均经实测验证（被质询项已标注）。*",
    ]

    out = out or Path(f"{_re.sub(r'[^a-zA-Z0-9_-]+', '-', proj['title']).strip('-') or 'astra'}-report.md")
    out.write_text("\n".join(lines), encoding="utf-8")
    click.echo(f"报告已生成：{out.resolve()}（发现 {len(findings)} 项：高危 {risk_counts['高危']}/中危 {risk_counts['中危']}）")
