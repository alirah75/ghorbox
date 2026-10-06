"""Allowlisted operational events, without exception or request serialization."""

import json
import logging
import sys
from enum import StrEnum
from typing import TextIO


class Event(StrEnum):
    STARTING = "application_starting"
    STOPPED = "application_stopped"
    STARTUP_FAILED = "startup_failed"
    UPDATE_FAILED = "update_failed"
    DELIVERY_FAILED = "delivery_worker_failed"
    STATISTICS_FAILED = "statistics_scheduler_failed"
    MAINTENANCE_FAILED = "maintenance_worker_failed"


class SafeEventFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == "feedback.events" and isinstance(record.msg, Event)


class SafeEventFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Never call getMessage(), formatException(), or serialize record.__dict__.
        return json.dumps({"event": str(record.msg)}, ensure_ascii=True)


def configure_logging(stream: TextIO | None = None) -> None:
    logging.captureWarnings(True)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.addFilter(SafeEventFilter())
    handler.setFormatter(SafeEventFormatter())
    root.addHandler(handler)
    # Third-party handlers can otherwise bypass the root policy.
    for entry in logging.root.manager.loggerDict.values():
        if isinstance(entry, logging.Logger):
            entry.handlers.clear()
            entry.propagate = True
    logging.getLogger("feedback.events").setLevel(logging.INFO)


def log_event(event: Event) -> None:
    logging.getLogger("feedback.events").info(event)
