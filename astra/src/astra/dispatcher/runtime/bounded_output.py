from __future__ import annotations

from collections import deque
from threading import Lock

MAX_STDOUT_CHARS = 4 * 1024 * 1024
MAX_STDERR_CHARS = 256 * 1024
TRUNCATION_MARKER = "\n[ASTRA output truncated]\n"


class BoundedOutput:
    """Keep a bounded head and tail without accumulating an unbounded process log."""

    def __init__(self, limit: int):
        if limit <= len(TRUNCATION_MARKER):
            raise ValueError("output limit must exceed truncation marker length")
        self._limit = limit
        self._head_limit = (limit - len(TRUNCATION_MARKER)) // 4
        self._tail_limit = limit - len(TRUNCATION_MARKER) - self._head_limit
        self._head_chunks: list[str] = []
        self._head_length = 0
        self._head: str | None = None
        self._tail: deque[str] = deque()
        self._tail_length = 0
        self._truncated = False
        self._lock = Lock()

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            if not self._truncated and self._head_length + len(chunk) <= self._limit:
                self._head_chunks.append(chunk)
                self._head_length += len(chunk)
                return
            if not self._truncated:
                existing = "".join(self._head_chunks)
                split = max(0, self._head_limit - len(existing))
                self._head = (existing + chunk[:split])[: self._head_limit]
                self._head_chunks.clear()
                self._head_length = 0
                self._truncated = True
                self._append_tail(existing[self._head_limit :])
                self._append_tail(chunk[split:])
                return
            self._append_tail(chunk)

    def _append_tail(self, chunk: str) -> None:
        if not chunk:
            return
        if len(chunk) >= self._tail_limit:
            self._tail.clear()
            self._tail.append(chunk[-self._tail_limit :])
            self._tail_length = self._tail_limit
            return
        self._tail.append(chunk)
        self._tail_length += len(chunk)
        while self._tail_length > self._tail_limit and self._tail:
            excess = self._tail_length - self._tail_limit
            oldest = self._tail[0]
            if len(oldest) <= excess:
                self._tail.popleft()
                self._tail_length -= len(oldest)
            else:
                self._tail[0] = oldest[excess:]
                self._tail_length -= excess

    def snapshot(self) -> tuple[str, bool]:
        with self._lock:
            if not self._truncated:
                return "".join(self._head_chunks), False
            assert self._head is not None
            return self._head + TRUNCATION_MARKER + "".join(self._tail), True
