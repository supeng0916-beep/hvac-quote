"""Retain bounded ezdxf diagnostics instead of treating exit 0 as complete fidelity."""

from contextlib import contextmanager
import logging


class WarningCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []
        self.count = 0

    def emit(self, record):
        self.count += 1
        if len(self.messages) < 20:
            self.messages.append(record.getMessage()[:500])


@contextmanager
def capture_warnings():
    logger = logging.getLogger("ezdxf")
    handler = WarningCapture()
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
