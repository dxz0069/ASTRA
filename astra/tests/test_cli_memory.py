"""memory 观测命令测试：trace 决策链回放 / map 图可视化 / report 渗透报告。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from click.testing import CliRunner  # noqa: E402

from astra.cli import main  # noqa: E402
from astra.server import db  # noqa: E402


def _seed_db(db_path: Path) -> None:
    db._db_path = None  # configure() 首调用生效：进程内跨用例换库需先复位
    db.configure(db_path)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, bootstrap_enabled, created_at) "
            "VALUES ('p1','demo-target','completed',1,'2026-08-24T10:00')"
        )
        facts = [
            ("goal", "获取目标系统权限", "regular"),
            ("origin", "http://target:8080", "regular"),
            ("f001", "登录接口存在 SQL 注入，可 union 拖库", "regular"),
            ("f002", "后台存在任意命令执行 RCE", "regular"),
        ]
        for fid, desc, kind in facts:
            conn.execute(
                "INSERT INTO facts (id, project_id, description, kind) VALUES (?,?,?,?)",
                (fid, "p1", desc, kind),
            )
        conn.execute(
            "INSERT INTO steps (id, project_id, to_fact_id, description, status, creator, worker, created_at, concluded_at) "
            "VALUES ('s1','p1','f002','由注入升级为 RCE','open','scout','scout','10:00','10:30')"
        )
        conn.execute(
            "INSERT INTO findings (id, project_id, description, created_at) "
            "VALUES ('fnd001','p1','SQL injection at /login allows union-based dump','10:30')"
        )


def _invoke(tmp_path: Path, args: list[str]):
    db_path = tmp_path / "cli.db"
    _seed_db(db_path)
    return CliRunner().invoke(main, args + ["--db-path", str(db_path)]), db_path


def test_memory_trace_outputs_decision_chain(tmp_path):
    result, _ = _invoke(tmp_path, ["memory", "trace", "demo"])
    assert result.exit_code == 0, result.output
    assert "SQL 注入" in result.output
    assert "已收束→f002" in result.output


def test_memory_map_writes_standalone_html(tmp_path):
    out = tmp_path / "map.html"
    result, _ = _invoke(tmp_path, ["memory", "map", "--out", str(out), "demo"])
    assert result.exit_code == 0, result.output
    html = out.read_text(encoding="utf-8")
    assert "<svg" in html and "f001" in html
    assert "事实" in html  # FGS 术语


def test_report_renders_risk_table_and_findings(tmp_path):
    out = tmp_path / "report.md"
    result, _ = _invoke(tmp_path, ["report", "--out", str(out), "demo"])
    assert result.exit_code == 0, result.output
    md = out.read_text(encoding="utf-8")
    assert "渗透测试报告" in md
    assert "## 三、攻击路径" in md and "已收束" in md
    assert "## 四、沿途发现" in md and "SQL injection at /login" in md
    assert "修复建议" in md
