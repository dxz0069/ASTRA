"""语义向量持久缓存（SQLite sidecar）测试：跨进程复用 / 禁用 / 损坏自愈 / 规模有界。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from astra.dispatcher import embeddings as E


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch, tmp_path):
    """每用例干净状态：清内存缓存、断开磁盘连接、env 全部指向临时目录。"""
    if E._disk_conn is not None:
        try:
            E._disk_conn.close()
        except sqlite3.Error:
            pass
    monkeypatch.setattr(E, "_disk_conn", None)
    monkeypatch.setattr(E, "_disk_writes", 0)
    E._vector_cache.clear()
    monkeypatch.setattr(E, "_api_failure_count", 0)
    monkeypatch.setenv("ASTRA_VECTOR_CACHE", str(tmp_path / "vec.sqlite3"))
    monkeypatch.setenv("ASTRA_EMBED_API_KEY", "test-key")
    monkeypatch.delenv("ASTRA_EMBED_PERSIST", raising=False)
    yield
    if E._disk_conn is not None:
        try:
            E._disk_conn.close()
        except sqlite3.Error:
            pass
        E._disk_conn = None
    E._vector_cache.clear()


def _fake_api(vectors: list[list[float]]):
    def _fake(texts: list[str]) -> list[list[float]] | None:
        assert len(texts) == len(vectors)
        return list(vectors)
    return _fake


def test_persist_roundtrip_survives_restart(monkeypatch, tmp_path):
    """重启语义（清内存缓存 + API 不可用）后向量仍可从磁盘取回——免重算。

    落盘为 float32 BLOB：比较用近似（相对 1e-6），不比逐位相等。
    """
    vec = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    monkeypatch.setattr(E, "_embed_via_api", _fake_api(vec))
    got = E.embed_texts(["hello", "world"])
    assert got == vec
    assert (tmp_path / "vec.sqlite3").exists()

    # 模拟进程重启：内存清空、连接重建、API 故障
    E._vector_cache.clear()
    monkeypatch.setattr(E, "_disk_conn", None)
    monkeypatch.setattr(E, "_embed_via_api", lambda texts: None)
    got2 = E.embed_texts(["hello", "world"])
    flat = [x for row in got2 for x in row]
    want = [x for row in vec for x in row]
    assert flat == pytest.approx(want, rel=1e-6)


def test_persist_disabled_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("ASTRA_EMBED_PERSIST", "0")
    monkeypatch.setattr(E, "_embed_via_api", _fake_api([[1.0, 0.0]]))
    assert E.embed_texts(["hello"]) == [[1.0, 0.0]]
    assert not (tmp_path / "vec.sqlite3").exists()

    # 关闭持久化时磁盘也不读：API 挂了直接降级
    E._vector_cache.clear()
    monkeypatch.setattr(E, "_embed_via_api", lambda texts: None)
    assert E.embed_texts(["hello"]) is None


def test_corrupt_file_self_heals(monkeypatch, tmp_path):
    path = tmp_path / "vec.sqlite3"
    path.write_bytes(b"not a sqlite file at all" * 100)
    monkeypatch.setattr(E, "_embed_via_api", _fake_api([[0.7, 0.8]]))
    assert E.embed_texts(["hello"]) == [[0.7, 0.8]]  # 不抛异常，损坏库删除重建
    assert E.embed_texts(["hello"]) == [[0.7, 0.8]]  # 重建后往返正常


def test_disk_store_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(E, "_PERSIST_MAX_ROWS", 5)
    monkeypatch.setattr(E, "_COMPACT_EVERY", 1)
    for i in range(10):
        E._disk_store([(f"text-{i}", [float(i), 1.0])])
    conn = sqlite3.connect(str(tmp_path / "vec.sqlite3"))
    count = conn.execute("SELECT COUNT(*) FROM vecs").fetchone()[0]
    conn.close()
    assert count <= 5
    # 淘汰的是最旧的：text-0 已删，text-9 仍在
    assert E._disk_lookup(["text-0"]) == {}
    assert E._disk_lookup(["text-9"]) != {}


def test_blob_decode_rejects_malformed():
    assert E._decode_blob(b"") is None
    assert E._decode_blob(b"\x01\x02\x03") is None  # 非 4 字节对齐
    assert E._decode_blob(b"\x00\x00\x80\x3f") == [1.0]
