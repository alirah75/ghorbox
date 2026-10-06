import io
import json
import logging

from app.core.logging import Event, configure_logging, log_event


def test_sensitive_logs_and_tracebacks_are_not_emitted():
    stream = io.StringIO()
    root = logging.getLogger()
    previous_handlers = root.handlers[:]
    previous_level = root.level
    try:
        configure_logging(stream)
        logging.getLogger("aiogram.event").error("sender=111 private body")
        logging.getLogger("aiohttp").error("https://api.telegram.org/botSECRET")
        logging.getLogger("feedback.events").info("accidental private body")
        try:
            raise ValueError("private payload")
        except ValueError:
            logging.getLogger("feedback.events").exception(Event.UPDATE_FAILED)
        log_event(Event.STARTING)
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        assert records == [
            {"event": "update_failed"},
            {"event": "application_starting"},
        ]
    finally:
        root.handlers = previous_handlers
        root.setLevel(previous_level)
        logging.captureWarnings(False)
