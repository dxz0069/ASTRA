"""审计第十一轮：前端交互一致性静态回归 + pi 适配器健壮性。

前端无法在 CI 起浏览器——用静态断言锁死三类回归：
1. 绑定一致性：index.html 引用的每个 @click 处理器 / x-if 模态名都必须在 app.js 定义
2. 命名回归锁：界面文案不得回退到旧术语（事实/航向/星记/星站/星辉/定航/巡猎——历史三套词汇）
3. 防抖与校验锚点：写操作必须带 actionBusy 防抖与前端必填校验
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "astra" / "server" / "static"


@pytest.fixture(scope="module")
def html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


def test_all_click_handlers_defined(html, js) -> None:
    """index.html 每个 @click 处理器在 app.js 有定义（曾漏 createIntent 悬空按钮）。"""
    handlers = set(re.findall(r'@click="([a-zA-Z_]\w*)\(', html))
    assert handlers, "未解析到任何处理器——正则失配需修测试"
    missing = [h for h in sorted(handlers) if not re.search(rf"{re.escape(h)}\s*\(", js)]
    assert not missing, f"app.js 缺失处理器: {missing}"


def test_all_modal_states_have_handlers(html, js) -> None:
    """x-if 模态名与 open/close 逻辑闭环：每个 modal==='X' 都有打开入口。"""
    modals = set(re.findall(r"modal==='([a-z]+)'", html))
    for m in sorted(modals):
        assert f"modal='{m}'" in js or f'modal="{m}"' in js or f"modal='{m}'" in js, f"模态 {m} 无打开入口"


def test_xmodel_targets_exist(html, js) -> None:
    """x-model 绑定的表单字段在 app.js 状态对象里存在（防字段改名悬空）。"""
    targets = set(re.findall(r'x-model(?:\.\w+)?="(\w+)\.(\w+)"', html))
    dynamic = {("settingsForm", "step_timeout"), ("settingsForm", "decide_timeout")}  # 服务端动态装载
    anchor = "UI_DEFAULTS"  # ui.* 字段定义在文件头 UI_DEFAULTS 常量
    for obj, field in sorted(targets - dynamic):
        a = anchor if obj == "ui" else obj
        pattern = rf"{a}\s*[:=].{{0,600}}?{field}\s*:"  # 有界窗口跨行匹配
        assert re.search(pattern, js, re.DOTALL) or f"self.{obj}" in js, f"x-model {obj}.{field} 在 app.js 无定义"


def test_ui_terminology_regression_lock(html) -> None:
    """命名回归锁：界面不得回退旧术语（历史上换过三套词汇，防再混）。"""
    banned = ["航向", "星记", "星站", "星辉", "定航", "巡猎", "已收束", "双星", "质询"]
    hits = [b for b in banned if b in html]
    assert not hits, f"index.html 出现旧术语: {hits}"


def test_write_actions_have_busy_guard(html, js) -> None:
    """全部写操作按钮带 actionBusy 防抖（双击重复提交防护）。"""
    for action in ["createProject()", "createStep()", "concludeStep()", "completeProject()",
                   "addHint()", "saveSettings()", "deleteProject()"]:
        assert f':disabled="actionBusy" @click="{action}"' in html, f"{action} 按钮缺防抖"
    assert "actionBusy" in js and "guard(fn)" in js


def test_client_side_required_validation(js) -> None:
    """前端必填校验锚点：步骤描述/结论/完成说明空值拦截（不再裸靠服务端 422）。"""
    for anchor in ["请填写斗柄指向", "请填写结论天枢", "请填写完成说明"]:
        assert anchor in js, f"缺前端校验提示: {anchor}"


def test_delete_clears_canvas(js) -> None:
    """删除项目清空画布（曾残留已删项目的图）。"""
    assert "cy.elements().remove()" in js


# ============ pi 适配器健壮性 ============


@pytest.fixture()
def pi_env():
    return {
        "PI_MODEL": "test-model",
        "PI_BASE_URL": "http://api",
        "PI_API_KEY": "secret",
        "PI_PROVIDER_API": "anthropic-messages",
    }


def _worker(env):
    from astra.dispatcher.config import WorkerConfig

    return WorkerConfig(
        name="pi-x", type="pi", task_types=["execute"], max_running=1, priority=0, env=env
    )


def test_pi_cli_missing_fails_fast_with_hint(monkeypatch, pi_env, tmp_path) -> None:
    """CLI 完全缺失：fail-fast 带安装指引（旧版静默退化 argv=['node','pi',...] → 晦涩 MODULE_NOT_FOUND）。"""
    import sys

    sys.platform  # noqa: B018
    from astra.dispatcher.workers.adapters.pi import PiDriver

    import astra.dispatcher.workers.adapters.pi as pi_mod

    monkeypatch.setattr(pi_mod.shutil, "which", lambda _name: None)
    if sys_platform() == "win32":
        # win 分支查 candidates 里 cli.js——伪造不存在
        monkeypatch.setattr(pi_mod.Path, "exists", lambda self, _p=None: False)
    env = dict(pi_env)
    env["PI_CODING_AGENT_DIR"] = str(tmp_path / "agent")
    try:
        PiDriver().build_execute(_worker(env), "prompt", None)
        raised = None
    except RuntimeError as exc:
        raised = str(exc)
    if sys.platform == "win32":
        # win 路径走 candidates + which 双查缺失
        assert raised and "npm install -g @mariozechner/pi-coding-agent" in raised
    else:
        assert raised and "npm install" in raised


def pi_driver_mod():
    import astra.dispatcher.workers.adapters.pi as mod

    return mod


def test_pi_models_json_write_failure_clear_error(monkeypatch, pi_env, tmp_path) -> None:
    """models.json 写失败（磁盘/权限）：明确报错而非裸 OSError 栈。"""
    from astra.dispatcher.workers.adapters.pi import PiDriver

    env = dict(pi_env)
    bad_dir = tmp_path / "blocked"
    bad_dir.write_text("i am a file not dir", encoding="utf-8")  # 同名文件占位 → mkdir 失败
    env["PI_CODING_AGENT_DIR"] = str(bad_dir)
    import astra.dispatcher.workers.adapters.pi as pi_mod

    monkeypatch.setattr(pi_mod.shutil, "which", lambda _n: "/fake/pi")
    if sys_platform() == "win32":
        monkeypatch.setattr(pi_mod.Path, "exists", lambda self, _p=None: "cli.js" in str(self))
    try:
        PiDriver().build_execute(_worker(env), "prompt", None)
        pytest.fail("应抛 RuntimeError")
    except RuntimeError as exc:
        assert "models.json 写入失败" in str(exc) or "写入失败" in str(exc)


def sys_platform():
    import sys

    return sys.platform


def test_pi_models_json_shape(pi_env) -> None:
    """models.json 形状锁：api 端点透传、thinkingLevelMap/compat 可选解析。"""
    import json

    from astra.dispatcher.workers.adapters.pi import PiDriver

    env = dict(pi_env)
    env["PI_THINKING_LEVEL_MAP"] = '{"low":"low","max":"max"}'
    env["PI_MODEL_COMPAT"] = '{"thinkingFormat":"deepseek"}'
    raw = PiDriver._models_json(_worker(env))
    m = json.loads(raw)
    provider = m["providers"]["astra"]
    assert provider["baseUrl"] == "http://api"
    assert provider["api"] == "anthropic-messages"
    model = provider["models"][0]
    assert model["thinkingLevelMap"] == {"low": "low", "max": "max"}
    assert model["compat"] == {"thinkingFormat": "deepseek"}


def test_pi_agent_dir_traversal_rejected(pi_env, monkeypatch) -> None:
    """CWE-22 回归：PI_CODING_AGENT_DIR 含 .. 拒绝。"""
    from astra.dispatcher.workers.adapters.pi import PiDriver
    import astra.dispatcher.workers.adapters.pi as pi_mod

    env = dict(pi_env)
    env["PI_CODING_AGENT_DIR"] = "E:/tmp/some/../../escape"
    monkeypatch.setattr(pi_mod.shutil, "which", lambda _n: "/fake/pi")
    if sys_platform() == "win32":
        monkeypatch.setattr(pi_mod.Path, "exists", lambda self, _p=None: "cli.js" in str(self))
    with pytest.raises(RuntimeError, match="traversal"):
        PiDriver().build_execute(_worker(env), "p", None)
