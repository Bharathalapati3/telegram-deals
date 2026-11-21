"""Logging: console + rotating file, plain text or JSON lines."""
from __future__ import annotations

import json
import logging
import logging.handlers
import time
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


def setup_logging(log_dir: Path, level: str = "INFO", fmt: str = "text") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter: logging.Formatter
    if fmt == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_h = logging.handlers.RotatingFileHandler(log_dir / "bot.log", maxBytes=5_000_000,
                                                  backupCount=5, encoding="utf-8")
    file_h.setFormatter(formatter)
    err_h = logging.handlers.RotatingFileHandler(log_dir / "errors.log", maxBytes=2_000_000,
                                                 backupCount=3, encoding="utf-8")
    err_h.setLevel(logging.WARNING)
    err_h.setFormatter(formatter)
    for h in (console, file_h, err_h):
        root.addHandler(h)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
