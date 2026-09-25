"""Structured logging + request/run correlation ids."""

from __future__ import annotations

import contextvars
import logging
import sys
import time
import uuid
from typing import Any

import structlog

from app.observability.redaction import is_secret_key

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)
run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="")

# Redaction lives in its own module: it does both key-name hiding and
# credential-shape scrubbing of values, and the reasoning behind the pattern
# choices is long enough to deserve the space. Re-exported here because
# everything already imports it from this module.
from app.observability.redaction import redact  # noqa: E402


def _add_context(_logger: Any, _name: str, event_dict: dict) -> dict:
    rid = request_id_var.get()
    run = run_id_var.get()
    if rid:
        event_dict.setdefault("request_id", rid)
    if run:
        event_dict.setdefault("agent_run_id", run)
    return event_dict


def _redact_event(_logger: Any, _name: str, event_dict: dict) -> dict:
    """Last line of defence, on every log line this process emits.

    Call sites are supposed to redact before logging. This runs anyway, because
    the one call site that forgets is the one that writes a token to disk — and
    a log file is the least guarded place a credential can end up.
    """
    return {
        key: ("<redacted>" if is_secret_key(key) else value)
        for key, value in redact(event_dict).items()
    }


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=getattr(logging, level.upper(), 20)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_context,
            _redact_event,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), 20)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "app") -> Any:
    return structlog.get_logger(name)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


class Timer:
    """Context manager yielding elapsed milliseconds."""

    def __init__(self) -> None:
        self.ms: float = 0.0
        self._t0 = 0.0

    def __enter__(self) -> Timer:
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.ms = (time.perf_counter() - self._t0) * 1000.0
