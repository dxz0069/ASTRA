from __future__ import annotations

from astra.dispatcher.workers.adapters import MockDriver, PiDriver
from astra.dispatcher.workers.base import WorkerDriver


DRIVERS: dict[str, WorkerDriver] = {
    "pi": PiDriver(),
    "mock": MockDriver(),
}


def get_driver(name: str) -> WorkerDriver:
    return DRIVERS[name]
