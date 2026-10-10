"""Bounded execution for optional, local analysis tools.

The output files are created exclusively and never contain more than their
respective byte budgets.  This module does not make the child a sandbox: it
only bounds wall time, captured output, and the lifetime of its process tree.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class ProcessMetrics:
    returncode: int
    elapsed_ms: int
    stdout_bytes: int
    stderr_bytes: int


class AnalysisProcessError(RuntimeError):
    """A process failed without including untrusted child output in the error."""

    def __init__(self, reason: str, *, returncode: int | None = None) -> None:
        self.reason = reason
        self.returncode = returncode
        super().__init__(reason)


@dataclass
class _StreamState:
    bytes_written: int = 0
    exceeded: bool = False
    failed: bool = False


def _copy_bounded(
    pipe: BinaryIO,
    output: BinaryIO,
    limit: int,
    state: _StreamState,
    changed: threading.Event,
) -> None:
    try:
        while True:
            chunk = os.read(pipe.fileno(), 64 * 1024)
            if not chunk:
                break
            allowed = max(0, limit - state.bytes_written)
            if allowed:
                output.write(chunk[:allowed])
                state.bytes_written += min(len(chunk), allowed)
            if len(chunk) > allowed:
                state.exceeded = True
                changed.set()
                break
    except (OSError, ValueError):
        state.failed = True
        changed.set()
    finally:
        try:
            pipe.close()
        except OSError:
            pass
        changed.set()


class _WindowsJob:
    """Kill the assigned process and all its descendants when the job closes."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        import ctypes
        from ctypes import wintypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL

        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise AnalysisProcessError("job_setup_failed")
        self._kernel = kernel
        self._handle = handle
        try:
            limits = _ExtendedLimit()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise AnalysisProcessError("job_setup_failed")
            process_handle = getattr(process, "_handle", None)
            if process_handle is None or not kernel.AssignProcessToJobObject(handle, process_handle):
                raise AnalysisProcessError("job_setup_failed")
        except BaseException:
            self.close()
            raise

    def terminate(self) -> None:
        if self._handle and not self._kernel.TerminateJobObject(self._handle, 1):
            raise AnalysisProcessError("cleanup_failed")

    def close(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


def _resume_windows_process(process: subprocess.Popen[bytes]) -> None:
    """Resume the still-suspended process only after its job is assigned."""
    import ctypes
    from ctypes import wintypes

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    kernel.Thread32First.restype = wintypes.BOOL
    kernel.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    kernel.Thread32Next.restype = wintypes.BOOL
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # SNAPTHREAD
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        raise AnalysisProcessError("job_setup_failed")
    try:
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        available = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while available:
            if entry.th32OwnerProcessID == process.pid:
                thread = kernel.OpenThread(0x0002, False, entry.th32ThreadID)
                if not thread:
                    raise AnalysisProcessError("job_setup_failed")
                try:
                    if kernel.ResumeThread(thread) != 1:
                        raise AnalysisProcessError("job_setup_failed")
                    return
                finally:
                    kernel.CloseHandle(thread)
            entry.dwSize = ctypes.sizeof(entry)
            available = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        raise AnalysisProcessError("job_setup_failed")
    finally:
        kernel.CloseHandle(snapshot)


def _kill_tree(process: subprocess.Popen[bytes], job: _WindowsJob | None) -> None:
    if os.name == "nt":
        if job is not None:
            job.terminate()
            return
        # Assignment may have failed. Never allow the unassigned process to
        # continue; taskkill is a best-effort cleanup for this narrow window.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            # The process is still suspended if job assignment failed, so
            # terminating the parent also prevents it spawning descendants.
            if process.poll() is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_bounded(
    command: list[str],
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
) -> ProcessMetrics:
    """Run one command and capture its streams within strict byte budgets.

    The caller supplies fresh output paths. A nonzero exit, timeout, stream
    overflow, or cleanup failure raises ``AnalysisProcessError``.
    """
    if (
        not command
        or not math.isfinite(timeout)
        or timeout <= 0
        or not isinstance(stdout_limit, int)
        or not isinstance(stderr_limit, int)
        or stdout_limit < 0
        or stderr_limit < 0
    ):
        raise ValueError("invalid process budget or command")
    if stdout_path.resolve() == stderr_path.resolve():
        raise ValueError("stdout and stderr paths must differ")

    started = time.monotonic()
    stdout_state = _StreamState()
    stderr_state = _StreamState()
    changed = threading.Event()
    process: subprocess.Popen[bytes] | None = None
    job: _WindowsJob | None = None
    threads: list[threading.Thread] = []
    reason: str | None = None
    try:
        with stdout_path.open("xb") as stdout_file, stderr_path.open("xb") as stderr_file:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    close_fds=True,
                    **(
                        {
                            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)
                            | 0x00000004  # CREATE_SUSPENDED until job assignment
                        }
                        if os.name == "nt"
                        else {"start_new_session": True}
                    ),
                )
                if os.name == "nt":
                    job = _WindowsJob(process)
                    _resume_windows_process(process)
            except (OSError, AnalysisProcessError) as exc:
                if process is not None and process.poll() is None:
                    try:
                        _kill_tree(process, job)
                    except (OSError, AnalysisProcessError):
                        pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                raise AnalysisProcessError(
                    "job_setup_failed" if isinstance(exc, AnalysisProcessError) else "start_failed"
                ) from None

            assert process.stdout is not None and process.stderr is not None
            for pipe, output, limit, state in (
                (process.stdout, stdout_file, stdout_limit, stdout_state),
                (process.stderr, stderr_file, stderr_limit, stderr_state),
            ):
                thread = threading.Thread(
                    target=_copy_bounded,
                    args=(pipe, output, limit, state, changed),
                    daemon=True,
                )
                threads.append(thread)
                thread.start()

            deadline = started + timeout
            while True:
                if stdout_state.exceeded:
                    reason = "stdout_limit"
                    break
                if stderr_state.exceeded:
                    reason = "stderr_limit"
                    break
                if stdout_state.failed or stderr_state.failed:
                    reason = "io_error"
                    break
                if process.poll() is not None and all(not thread.is_alive() for thread in threads):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = "timeout"
                    break
                changed.wait(min(0.02, remaining))
                changed.clear()

            if reason is not None:
                try:
                    _kill_tree(process, job)
                except (OSError, AnalysisProcessError):
                    reason = "cleanup_failed"
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                reason = "cleanup_failed"
                try:
                    _kill_tree(process, job)
                    process.kill()
                except (OSError, AnalysisProcessError):
                    pass
            for thread in threads:
                thread.join(timeout=5)
            if any(thread.is_alive() for thread in threads):
                reason = "cleanup_failed"
            if reason is not None:
                raise AnalysisProcessError(reason, returncode=process.returncode)
            if process.returncode != 0:
                raise AnalysisProcessError("nonzero_exit", returncode=process.returncode)
            return ProcessMetrics(
                returncode=process.returncode,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                stdout_bytes=stdout_state.bytes_written,
                stderr_bytes=stderr_state.bytes_written,
            )
    except OSError:
        raise AnalysisProcessError("io_error") from None
    finally:
        if process is not None:
            if os.name == "nt":
                if job is None and process.poll() is None:
                    try:
                        _kill_tree(process, None)
                    except OSError:
                        pass
            else:
                # A child may outlive its parent and close both pipes. End the
                # entire session even after a successful parent exit.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if job is not None:
            job.close()
        if process is not None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            for thread in threads:
                thread.join(timeout=1)
            if not threads:
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
