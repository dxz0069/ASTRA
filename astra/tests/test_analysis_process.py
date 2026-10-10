from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from astra import analysis_process
from astra.analysis_process import AnalysisProcessError, run_bounded


def _run(
    tmp_path: Path,
    code: str,
    *,
    timeout: float = 5,
    stdout_limit: int = 4096,
    stderr_limit: int = 4096,
):
    return run_bounded(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        stdout_path=tmp_path / "stdout.bin",
        stderr_path=tmp_path / "stderr.bin",
        timeout=timeout,
        stdout_limit=stdout_limit,
        stderr_limit=stderr_limit,
    )


def _is_active(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists() and proc_stat.read_text().split()[2] == "Z":
        return False
    return True


def test_success_captures_bytes_and_metrics(tmp_path: Path) -> None:
    metrics = _run(
        tmp_path,
        "import sys; sys.stdout.buffer.write(b'abc'); sys.stderr.buffer.write(b'xy')",
    )
    assert metrics.returncode == 0
    assert metrics.elapsed_ms >= 0
    assert metrics.stdout_bytes == 3
    assert metrics.stderr_bytes == 2
    assert (tmp_path / "stdout.bin").read_bytes() == b"abc"
    assert (tmp_path / "stderr.bin").read_bytes() == b"xy"


def test_nonzero_exit_does_not_echo_child_output(tmp_path: Path) -> None:
    with pytest.raises(AnalysisProcessError) as caught:
        _run(tmp_path, "import sys; sys.stderr.write('untrusted secret'); sys.exit(7)")
    assert caught.value.reason == "nonzero_exit"
    assert caught.value.returncode == 7
    assert "untrusted secret" not in str(caught.value)


def test_stdout_limit_kills_process_and_caps_file(tmp_path: Path) -> None:
    with pytest.raises(AnalysisProcessError) as caught:
        _run(
            tmp_path,
            "import sys,time; sys.stdout.buffer.write(b'x'*1048576); sys.stdout.flush(); time.sleep(30)",
            timeout=5,
            stdout_limit=1024,
        )
    assert caught.value.reason == "stdout_limit"
    assert (tmp_path / "stdout.bin").stat().st_size <= 1024


def test_timeout_cleans_up_child_process(tmp_path: Path) -> None:
    code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print(child.pid,flush=True); time.sleep(30)"
    )
    with pytest.raises(AnalysisProcessError) as caught:
        _run(tmp_path, code, timeout=3)
    assert caught.value.reason == "timeout"
    child_pid = int((tmp_path / "stdout.bin").read_text().strip())
    deadline = time.monotonic() + 3
    while _is_active(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _is_active(child_pid)


def test_stderr_limit_caps_file(tmp_path: Path) -> None:
    with pytest.raises(AnalysisProcessError) as caught:
        _run(
            tmp_path,
            "import sys,time; sys.stderr.buffer.write(b'x'*1048576); sys.stderr.flush(); time.sleep(30)",
            stderr_limit=128,
        )
    assert caught.value.reason == "stderr_limit"
    assert (tmp_path / "stderr.bin").stat().st_size <= 128


def test_success_ends_descendants_with_closed_streams(tmp_path: Path) -> None:
    code = (
        "import subprocess,sys; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(child.pid,flush=True)"
    )
    _run(tmp_path, code)
    child_pid = int((tmp_path / "stdout.bin").read_text().strip())
    deadline = time.monotonic() + 3
    while _is_active(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _is_active(child_pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows job assignment")
def test_failed_job_assignment_never_runs_child(tmp_path: Path, monkeypatch) -> None:
    def fail_assignment(_process):
        raise AnalysisProcessError("job_setup_failed")

    monkeypatch.setattr(analysis_process, "_WindowsJob", fail_assignment)
    with pytest.raises(AnalysisProcessError) as caught:
        _run(tmp_path, "from pathlib import Path; Path('started.txt').write_text('ran')")
    assert caught.value.reason == "job_setup_failed"
    assert not (tmp_path / "started.txt").exists()
