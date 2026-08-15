from __future__ import annotations

import logging


class DispatcherLogFormatter(logging.Formatter):
    _PREFIX = "astra.dispatcher."

    def format(self, record: logging.LogRecord) -> str:
        name = record.name
        if name.startswith(self._PREFIX):
            shortname = name[len(self._PREFIX):]
        else:
            shortname = name
        setattr(record, "shortname", shortname)
        return super().format(record)


def configure_logging(level: str = "INFO", *, bare: bool = False) -> None:
    # Windows 管道/控制台默认 GBK：中文日志（UTF-8）会触发 UnicodeEncodeError 导致进程崩溃
    try:
        import sys

        if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 —— 编码修复失败不阻断启动
        pass
    handler = logging.StreamHandler()
    if bare:
        handler.setFormatter(logging.Formatter(fmt="%(message)s"))
    else:
        handler.setFormatter(
            DispatcherLogFormatter(
                fmt="[%(asctime)s] %(levelname)s %(shortname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), handlers=[handler], force=True)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
