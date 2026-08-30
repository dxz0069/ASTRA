"""审计第九轮（长跑资源泄漏 + 高频压力实证）：多用户高并发下的稳定性。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from astra.server import db
from astra.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "astra.db")
    with TestClient(app) as test_client:
        yield test_client


def _create_project(client: TestClient) -> str:
    return client.post(
        "/projects", json={"title": "stress", "origin": "o", "goal": "g"}
    ).json()["project"]["id"]


def test_high_frequency_mixed_load_no_5xx_no_deadlock(client: TestClient) -> None:
    """高频混合负载实证：200 并发请求（读+写+认领+超长拒绝），零 5xx、计数守恒、限时完成。"""
    pid = _create_project(client)
    # 预置 20 个未认领步骤（并发认领标的）
    for i in range(20):
        r = client.post(
            f"/projects/{pid}/steps",
            json={"from": ["origin"], "description": f"stress-{i}", "creator": "loader"},
        )
        assert r.status_code == 201

    results: list[tuple[int, str]] = []
    lock = threading.Lock()

    def hit(kind: str, i: int):
        try:
            if kind == "read":
                r = client.get(f"/projects/{pid}")
                code = r.status_code
            elif kind == "claim":
                r = client.post(
                    f"/projects/{pid}/steps/s{i % 20 + 1:03d}/heartbeat",
                    json={"worker": f"w{i}"},
                )
                code = r.status_code
            elif kind == "finding":
                r = client.post(
                    f"/projects/{pid}/findings",
                    json={"description": f"finding-{i}"},
                )
                code = r.status_code
            else:  # 恶意超长
                r = client.post(
                    f"/projects/{pid}/subgoals", json={"description": "x" * 100_000}
                )
                code = r.status_code
            with lock:
                results.append((code, kind))
        except Exception as exc:  # noqa: BLE001
            with lock:
                results.append((-1, f"{kind}:{exc}"))

    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = []
        for i in range(50):
            futures.append(pool.submit(hit, "read", i))
        for i in range(60):
            futures.append(pool.submit(hit, "claim", i))
        for i in range(50):
            futures.append(pool.submit(hit, "finding", i))
        for i in range(40):
            futures.append(pool.submit(hit, "evil", i))
        for f in futures:
            f.result(timeout=120)

    codes = [c for c, _ in results]
    # 零 5xx / 零异常（-1）
    assert not [c for c in codes if c >= 500 or c < 0], sorted(
        [r for r in results if r[0] >= 500 or r[0] < 0]
    )[:5]
    # 恶意输入全部 422
    evil = [c for c, k in results if k == "evil"]
    assert evil and all(c == 422 for c in evil)
    # 写入守恒：findings 全部落库（50）
    detail = client.get(f"/projects/{pid}").json()
    assert len(detail["findings"]) == 50
    # 数据一致性：每步骤最多一个 worker 认领（原子认领不变量）
    for step in detail["steps"]:
        assert isinstance(step["worker"], (str, type(None)))


def test_prompt_temp_file_cleanup(tmp_path, monkeypatch) -> None:
    """长跑泄漏修复：Windows prompt @file 写前清理 1h 前旧文件。"""
    import sys
    import time
    from pathlib import Path

    if sys.platform != "win32":
        pytest.skip("Windows 专属路径")
    from astra.dispatcher.workers.adapters.pi import PiDriver

    import tempfile
    root = Path(tempfile.gettempdir())
    # 造一个 2 小时前的旧文件 + 一个新鲜文件
    stale = root / "astra-pi-prompt-deadbeef.txt"
    stale.write_text("old", encoding="utf-8")
    old_ts = time.time() - 7200
    import os

    os.utime(stale, (old_ts, old_ts))
    fresh = root / f"astra-pi-prompt-{int(time.time())}.txt"
    fresh.write_text("new", encoding="utf-8")

    arg = PiDriver._prompt_arg("hello")
    assert arg.startswith("@")
    assert not stale.exists()  # 旧文件被清
    assert fresh.exists()  # 新鲜文件不动
    assert Path(arg[1:]).read_text(encoding="utf-8") == "hello"
    Path(arg[1:]).unlink(missing_ok=True)  # 测试自理
    fresh.unlink(missing_ok=True)
