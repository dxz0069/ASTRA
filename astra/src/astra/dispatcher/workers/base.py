from __future__ import annotations

import abc
import re
import shlex
import uuid
from dataclasses import dataclass

from astra.dispatcher.config import WorkerConfig


@dataclass(slots=True)
class DriverResult:
    argv: list[str]
    session: str | None = None


class WorkerDriver(abc.ABC):
    type_name: str

    def supports_conclude(self) -> bool:
        return True

    def prepare_session(self) -> str | None:
        return None

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return self.build_healthcheck(worker)

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        return shlex.join(self.build_startup_healthcheck(worker))

    @abc.abstractmethod
    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        raise NotImplementedError

    @abc.abstractmethod
    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        raise NotImplementedError

    def build_decide(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        """Build a decision invocation.

        Drivers that do not need phase-specific tool isolation can keep the
        regular execute command.  Pi overrides this to expose only read-only
        tools while inspecting the graph.
        """
        return self.build_execute(worker, prompt, session)

    def build_challenge(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        return self.build_execute(worker, prompt, session)

    def build_strike(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        """Build an independent Finding verification invocation.

        Verification needs execution tools to reproduce a reported claim. The
        dedicated hook leaves phase-specific overrides available to drivers.
        """
        return self.build_execute(worker, prompt, session)

    @abc.abstractmethod
    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        raise NotImplementedError

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        return session

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        return stdout


class SeedSessionDriver(WorkerDriver):
    def prepare_session(self) -> str | None:
        return str(uuid.uuid4())


class RegexSessionDriver(WorkerDriver):
    session_pattern = re.compile(r"session id:\s*([0-9a-fA-F-]+)")

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        match = self.session_pattern.search(stderr)
        if match:
            return match.group(1)
        return None
