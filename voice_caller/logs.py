"""Logging with secret redaction. Every handler gets a filter that replaces any
loaded secret value (and per-call stream tokens) with *** before it is written."""
from __future__ import annotations

import logging
import sys
import threading

_SECRETS: set[str] = set()
_LOCK = threading.Lock()


def register_secret(value: str) -> None:
    if value and len(value) >= 6:
        with _LOCK:
            _SECRETS.add(value)


def redact(text: str) -> str:
    if not _SECRETS or not text:
        return text
    with _LOCK:
        secrets = sorted(_SECRETS, key=len, reverse=True)
    for s in secrets:
        if s in text:
            text = text.replace(s, "***")
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        red = redact(msg)
        if red != msg or record.args:
            record.msg, record.args = red, ()
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def setup_logging(verbose: bool = False, logfile=None) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s", "%H:%M:%S")
    handlers = [logging.StreamHandler(sys.stderr)]
    if logfile:
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactingFilter())
        root.addHandler(h)
    handlers[0].setLevel(logging.DEBUG if verbose else logging.INFO)
    root.setLevel(logging.DEBUG)
    for noisy in ("aiohttp.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
