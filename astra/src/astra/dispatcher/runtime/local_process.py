from __future__ import annotations

"""本地进程执行（local execution 模式）：subprocess 直跑星探 CLI，无需 Docker。

接口与 docker 版 ManagedProcess 对齐（start/communicate/kill/cancel）。
超时：优先 terminate，宽限期后 kill（Windows 上进程树 kill 尽力而为）。
"""

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger(__name__)
KILL_GRACE_SECONDS = 5.0


@dataclass(slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    cancel_reason: str | None = None


class LocalProcess:
    def __init__(
        self,
        command: list[str],
        env: dict[str, str],
        *,
        cwd: str | Path | None = None,
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ):
        self.command = command
        self.env = env
        self._cwd = str(cwd) if cwd is not None else None
        self._timeout_seconds = timeout_seconds
        self._kill_after_seconds = kill_after_seconds
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._returncode: int | None = None
        self._timed_out = False
        self._cancel_reason: str | None = None
        self._done = threading.Event()

    def start(self) -> None:
        full_env = dict(__import__("os").environ)
        full_env.update(self.env)
        # MSYS2 程序（sh/git 等）从 Windows 命令行重建 argv 时会破坏含空格的参数，
        # 禁用其参数转换以保持 Python list2cmdline 的引号语义
        full_env.setdefault("MSYS2_ARG_CONV_EXCL", "*")
        creationflags = 0
        # P1-3：POSIX 下新建会话/进程组——terminate 时才能 killpg 连同 dsh(node)
        # 派生的孙进程（nmap/curl 等）一并回收；否则只杀直接子进程，孙进程成孤儿
        popen_kwargs: dict = {}
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        # Windows 上 npm 全局 CLI 是 .cmd 包装，Popen 找不到裸命令名——用 which 解析真实可执行文件
        argv = list(self.command)
        if self.command and sys.platform == "win32":
            resolved = shutil.which(self.command[0])
            if resolved:
                argv[0] = resolved
        self._process = subprocess.Popen(
            argv,
            cwd=self._cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=full_env,
            creationflags=creationflags,
            **popen_kwargs,
        )
        # stdout/stderr 分开线程读取，避免管道缓冲死锁
        self._stdout_reader = threading.Thread(target=self._read_pipe, args=(self._process.stdout, self._stdout), daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_pipe, args=(self._process.stderr, self._stderr), daemon=True)
        self._stdout_reader.start()
        self._stderr_reader.start()

    def communicate(self, timeout: float | None) -> ProcessResult:
        assert self._process is not None
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._timed_out = True
            self.kill()
            try:
                self._process.wait(timeout=self._kill_after_seconds + KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        self._join_pipe(self._stdout_reader)
        self._join_pipe(self._stderr_reader)
        if self._returncode is None:
            self._returncode = self._process.returncode
        return ProcessResult(
            returncode=self._returncode if self._returncode is not None else 1,
            stdout="".join(self._stdout),
            stderr="".join(self._stderr),
            timed_out=self._timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def _join_pipe(self, reader: threading.Thread) -> None:
        reader.join(timeout=KILL_GRACE_SECONDS)

    @staticmethod
    def _read_pipe(pipe, sink: list[str]) -> None:
        assert pipe is not None
        try:
            for line in pipe:
                sink.append(line)
        except (OSError, ValueError):
            pass

    def kill(self) -> None:
        if self._process is None or self._process.poll() is not None:
            return
        LOG.info("terminating local process pid=%s command=%s", self._process.pid, self.command[:1])
        try:
            if sys.platform == "win32":
                self._process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                # P1-3：按进程组发 SIGTERM——连同 dsh 派生的孙进程一起回收；
                # 进程恰好已退出（竞态）时忽略 ProcessLookupError
                try:
                    os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        except OSError:
            pass
        deadline = time.monotonic() + max(self._kill_after_seconds, 1.0)
        while time.monotonic() < deadline and self._process.poll() is None:
            time.sleep(0.1)
        if self._process.poll() is None:
            LOG.warning("force killing local process pid=%s", self._process.pid)
            try:
                if sys.platform == "win32":
                    self._process.kill()
                else:
                    # P1-3：强杀同样按进程组 SIGKILL，杜绝孙进程逃逸成孤儿
                    try:
                        os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            except OSError:
                pass

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self.kill()

