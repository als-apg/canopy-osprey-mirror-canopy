"""Application logging for both process types (canopy-api and canopy-worker).

Configured from core settings (``CANOPY_LOG_FORMAT`` / ``CANOPY_LOG_PATH`` /
``CANOPY_LOG_LEVEL``). The "json" format emits one JSON object per line with
any ``extra={...}`` attributes merged in as first-class fields, so a log
shipper (Promtail in ``docker/observability/``) can parse lines with a plain
``json`` pipeline stage. File output is best-effort: if the log file or its
directory cannot be created, logging falls back to stdout only rather than
failing startup.

Framework loggers (uvicorn, arq) are folded into the same root handlers, so
HTTP access lines and worker lifecycle messages land in the same file, in the
same format, as application logs.
"""

import sys
import json
import logging
import traceback
from pathlib import Path
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

from canopy.core.config import get_settings

# Rotation bounds for the file log. It exists only for a log shipper to tail,
# so a short window is enough - and without rotation a log path that isn't on
# a mounted volume grows in the container's writable layer for the life of the
# container.
_LOG_MAX_BYTES = 50 * 1024 * 1024
_LOG_BACKUP_COUNT = 3

# Attributes every LogRecord carries; anything else on the record came from
# `extra={...}` at the call site and is merged into the JSON line.
_STANDARD_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message",
    "asctime",
    "taskName",
    # uvicorn attaches an ANSI-coloured duplicate of `message` for its own
    # terminal formatter; it carries nothing the JSON line needs.
    "color_message",
}

# Keys JsonFormatter writes itself. An `extra` that collides is written under
# an `extra_` prefix rather than allowed to overwrite: `level` is a Promtail
# index label, so a collision would corrupt querying, not just the one line.
_RESERVED_KEYS = frozenset({"ts", "level", "logger", "message", "error"})

# Loggers that uvicorn / arq configure with their own handlers (uvicorn's with
# propagate=False) before the app is imported; see _adopt_framework_loggers.
_FRAMEWORK_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "arq")

_TEXT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts / level / logger / message + extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key.startswith("_"):
                continue
            payload[f"extra_{key}" if key in _RESERVED_KEYS else key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["error"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return json.dumps(payload, default=str)


def _resolve_level(name: str) -> int | None:
    """Map a log level name to its number.

    Parameters
    ----------
    name : str
        Level name, matched case-insensitively and ignoring surrounding whitespace.

    Returns
    -------
    int | None
        The level number, or ``None`` when ``name`` is not a known level.
    """
    return logging.getLevelNamesMapping().get(name.strip().upper())


def configure_logging(level: int | None = None) -> None:
    """Configure application logging. Idempotent, safe to call repeatedly.

    Parameters
    ----------
    level : int | None
        Level number that overrides ``CANOPY_LOG_LEVEL`` when given, kept for callers
        that predate the settings-driven config. When omitted, ``CANOPY_LOG_LEVEL`` is
        used; an unknown name there falls back to ``INFO`` and logs a warning through
        the configured handlers instead of failing process startup.

    Returns
    -------
    None
    """
    settings = get_settings()
    unknown_level: str | None = None
    if level is None:
        resolved = _resolve_level(settings.log_level)
        if resolved is None:
            unknown_level = settings.log_level
        level = logging.INFO if resolved is None else resolved

    formatter: logging.Formatter
    if settings.log_format == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(_TEXT_FORMAT)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    file_error: OSError | None = None
    if settings.log_path:
        try:
            log_path = Path(settings.log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(log_path, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUP_COUNT, encoding="utf-8")
            )
        except OSError as exc:
            file_error = exc

    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=handlers, force=True)
    _adopt_framework_loggers()

    if file_error is not None:
        # Best-effort: a bad log path must not fail process startup. Reported
        # after basicConfig so the warning goes through the configured handlers
        # (structured, in JSON mode) instead of logging.lastResort on stderr.
        logging.getLogger(__name__).warning(
            "Could not open log file %s (%s); logging to stdout only", settings.log_path, file_error
        )
    if unknown_level is not None:
        logging.getLogger(__name__).warning("Unknown log level %r, falling back to INFO", unknown_level)


def _adopt_framework_loggers() -> None:
    """Route uvicorn's and arq's loggers through the root handlers.

    Both frameworks apply their own dictConfig before importing the app:
    uvicorn attaches stderr/stdout handlers to ``uvicorn`` and
    ``uvicorn.access`` with ``propagate=False`` (so access logs would never
    reach the JSON file), and arq attaches a stdout handler to ``arq`` (so its
    lines would print twice). Dropping those handlers and letting the records
    propagate puts everything through one formatter, one file, and one level
    (``CANOPY_LOG_LEVEL``).
    """
    for name in _FRAMEWORK_LOGGERS:
        framework_logger = logging.getLogger(name)
        for handler in framework_logger.handlers:
            handler.close()
        framework_logger.handlers.clear()
        framework_logger.propagate = True
        framework_logger.setLevel(logging.NOTSET)
