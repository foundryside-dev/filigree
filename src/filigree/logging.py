"""Structured JSON logging for filigree.

Writes JSONL to <store>/filigree.log with rotation (5MB, 3 backups).

Besides the free-form records, every MCP tool call, HTTP request and CLI
command emits one ``event="call"`` record via :func:`log_outcome`::

    {"event": "call", "surface": "mcp"|"http"|"cli", "name": ..., "outcome":
     "ok"|"error"|"no_op"|"validation", "code": <error code or null>,
     "duration_ms": float, "population": <project population tag or null>}

``outcome`` is derived from the *returned* envelope (see :func:`classify_body`),
not only from exceptions, so dead-ends that come back as ordinary results are
visible.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal

CallOutcome = Literal["ok", "error", "no_op", "validation"]
CallSurface = Literal["mcp", "http", "cli"]

_LOG_FILENAME = "filigree.log"
_setup_lock = threading.Lock()
_MAX_BYTES = 5 * 1024 * 1024  # 5MB
_BACKUP_COUNT = 3


class _JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        if hasattr(record, "tool"):
            entry["tool"] = record.tool
        if hasattr(record, "args_data"):
            entry["args"] = record.args_data
        if hasattr(record, "duration_ms"):
            entry["duration_ms"] = record.duration_ms
        if hasattr(record, "error"):
            entry["error"] = record.error
        if hasattr(record, "call_fields"):
            # ``name`` is a reserved LogRecord attribute, so the call record's
            # fields travel in one dict and are flattened here.
            entry.update(record.call_fields)
        if record.exc_info and record.exc_info[1]:
            exc = record.exc_info[1]
            entry["exception"] = str(exc)
            entry["exception_type"] = type(exc).__name__
            entry["traceback"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, default=str)


def setup_logging(filigree_dir: Path) -> logging.Logger:
    """Set up structured JSON logging to .filigree/filigree.log.

    Returns a logger that writes JSONL with rotation.
    """
    logger = logging.getLogger("filigree")
    log_path = filigree_dir / _LOG_FILENAME
    target_filename = os.path.abspath(str(log_path))

    with _setup_lock:
        # Scan every RotatingFileHandler on the logger. Keep at most one that
        # matches the target path; close and remove the rest (stale paths,
        # plus any duplicate matches from a prior leak).
        surviving: RotatingFileHandler | None = None
        for h in logger.handlers[:]:
            if not isinstance(h, RotatingFileHandler):
                continue
            if h.baseFilename == target_filename and surviving is None:
                surviving = h
                continue
            logger.removeHandler(h)
            h.close()

        if surviving is None:
            surviving = RotatingFileHandler(
                str(log_path),
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
            )
            logger.addHandler(surviving)

        # Apply configuration unconditionally so a reused handler that was
        # attached without a formatter or correct level still satisfies the
        # function's contract (JSONL output, INFO level).
        if not isinstance(surviving.formatter, _JsonFormatter):
            surviving.setFormatter(_JsonFormatter())
        logger.setLevel(logging.INFO)
    return logger


def classify_body(body: Any) -> tuple[CallOutcome, str | None]:
    """Classify a decoded response/envelope body into ``(outcome, code)``.

    * non-null ``"error"`` value or truthy ``isError`` -> ``error`` (``validation`` when the
      envelope's ``code`` is ``VALIDATION``), with ``code`` taken from the body;
    * ``{"result": "no_op"}`` / ``{"undone": false}`` / ``{"status": "empty"}``
      -> ``no_op``;
    * anything else -> ``ok``.
    """
    if not isinstance(body, dict):
        return "ok", None
    # ``error: null`` is a healthy envelope (e.g. mcp_status_get carries
    # ``error``/``code`` keys as None), so test the value, not key presence.
    if body.get("error") is not None or body.get("isError"):
        raw_code = body.get("code")
        code = raw_code if isinstance(raw_code, str) else None
        return ("validation" if code == "VALIDATION" else "error"), code
    if body.get("result") == "no_op" or body.get("undone") is False or body.get("status") == "empty":
        return "no_op", None
    return "ok", None


_population_cache: dict[str, tuple[float, str | None]] = {}


def population_for(filigree_dir: Path | None) -> str | None:
    """Return the ``population`` tag for the project store at *filigree_dir*.

    ``None`` when there is no project or the tag is unset. Cached per config
    file (keyed on mtime) because the HTTP surface asks on every request.
    """
    if filigree_dir is None:
        return None
    from filigree.core import CONFIG_FILENAME, read_population

    config_path = filigree_dir / CONFIG_FILENAME
    try:
        mtime = config_path.stat().st_mtime
    except OSError:
        return None
    key = str(config_path)
    cached = _population_cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    value = read_population(filigree_dir)
    _population_cache[key] = (mtime, value)
    return value


def log_outcome(
    logger: logging.Logger,
    *,
    surface: CallSurface,
    name: str,
    outcome: CallOutcome,
    code: str | None,
    duration_ms: float,
    population: str | None = None,
    extra: dict[str, Any] | None = None,
    msg: str = "call",
) -> None:
    """Emit the single ``event="call"`` record shared by MCP, HTTP and CLI.

    *extra* carries additional LogRecord attributes (e.g. ``tool``/``args_data``
    for the MCP record's legacy ``args`` field). Never raises: instrumentation
    must not break the call it observes.
    """
    try:
        logger.info(
            msg,
            extra={
                **(extra or {}),
                "call_fields": {
                    "event": "call",
                    "surface": surface,
                    "name": name,
                    "outcome": outcome,
                    "code": code,
                    "duration_ms": duration_ms,
                    "population": population,
                },
            },
        )
    except Exception:  # pragma: no cover - logging must never break a call
        logging.getLogger(__name__).debug("log_outcome failed", exc_info=True)
