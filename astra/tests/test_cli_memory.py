"""memory 观测命令测试：trace 决策链回放 / map 星图可视化 / report 渗透报告。"""

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
            "INSERT INTO projects VALUES ('p1','demo-target','completed','2026-08-24T10:00',1,NULL,NULL,NULL,NULL)"
        )
        facts = [
            ("goal", "获取目标系统权限", "regular", "medium", None, 0),
            ("f001", "登录接口存在 SQL 注入，可 union 拖库", "regular", "high", "sqlmap banner", 0),
            ("f002", "后台存在任意命令执行 RCE", "regular", "high", None, 0),
            ("f003", "旧发现摘要", "summary", "medium", None, 0),
        ]
        for fid, desc, kind, conf, ev, ch in facts:
            conn.execute(
                "INSERT INTO facts (id,project_id,description,kind,confidence,evidence,challenged) "
                "VALUES (?,?,?,?,?,?,?)",
                (fid, "p1", desc, kind, conf, ev, ch),
            )
        conn.execute(
            "INSERT INTO intents (id,project_id,to_fact_id,description,creator,created_at,concluded_at,challenged) "
            "VALUES ('i1','p1','f002','由注入升级为 RCE','scout','10:00','10:30',1)"
        )


def _invoke(tmp_path: Path, args: list[str]):
    db_path = tmp_path / "cli.db"
    _seed_db(db_path)
    return CliRunner().invoke(main, args + ["--db-path", str(db_path)]), db_path


def test_memory_trace_outputs_decision_chain(tmp_path):
    result, _ = _invoke(tmp_path, ["memory", "trace", "demo"])
    assert result.exit_code == 0, result.output
    assert "SQL 注入" in result.output
    assert "已归航→f002" in result.output
    assert "被质询" in result.output


def test_memory_map_writes_standalone_html(tmp_path):
    out = tmp_path / "map.html"
    result, _ = _invoke(tmp_path, ["memory", "map", "--out", str(out), "demo"])
    assert result.exit_code == 0, result.output
    html = out.read_text(encoding="utf-8")
    assert "<svg" in html and "f001" in html
    assert "stroke-dasharray" in html  # 摘要星记虚线框
    assert "质询" in html  # 被质询标记


def test_report_renders_risk_table_and_evidence(tmp_path):
    out = tmp_path / "report.md"
    result, _ = _invoke(tmp_path, ["report", "--out", str(out), "demo"])
    assert result.exit_code == 0, result.output
    md = out.read_text(encoding="utf-8")
    assert "渗透测试报告" in md
    assert "| 高危 / 中危 / 低危 / 信息 | 1 / 1 / 0 / 0 |" in md  # RCE=高危(高置信), 注入+高置信=中危
    assert "## 三、攻击路径" in md and "被质询否决" in md
    assert "## 四、证据链" in md and "sqlmap banner" in md
    assert "修复建议" in md
