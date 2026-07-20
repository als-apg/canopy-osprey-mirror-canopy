"""Application logging for both process types (canopy-api and canopy-worker).

Configured from core settings (``CANOPY_LOG_FORMAT`` / ``CANOPY_LOG_PATH`` /
``CANOPY_LOG_LEVEL``). The "json" format emits one JSON object per line with
any ``extra={...}`` attributes merged in as first-class fields, so a log
shipper (Promtail in ``docker/observability/``) can parse lines with a plain
``json`` pipeline stage. File output is best-effort: if the log file or its
directory cannot be created, logging falls back to stdout only rather than
failing startup.
"""

import sys
import json
import logging
import traceback
from pathlib import Path
from datetime import UTC, datetime

from canopy.core.config import get_settings

# Attributes every LogRecord carries; anything else on the record came from
# `extra={...}` at the call site and is merged into the JSON line.
_STANDARD_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message",
    "asctime",
    "taskName",
}

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
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["error"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return json.dumps(payload, default=str)


def _resolve_level(name: str, fallback: int) -> int:
    resolved = logging.getLevelNamesMapping().get(name.upper())
    return resolved if resolved is not None else fallback


def configure_logging(level: int | None = None) -> None:
    """Configure application logging. Idempotent, safe to call repeatedly.

    ``level`` overrides ``CANOPY_LOG_LEVEL`` when given (kept for callers that
    predate the settings-driven config).
    """
    settings = get_settings()
    if level is None:
        level = _resolve_level(settings.log_level, logging.INFO)

    formatter: logging.Formatter
    if settings.log_format == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(_TEXT_FORMAT)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if settings.log_path:
        try:
            log_path = Path(settings.log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_path))
        except OSError:
            # Best-effort: a bad log path must not fail process startup.
            logging.getLogger(__name__).warning("Could not open log file %s; logging to stdout only", settings.log_path)

    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=handlers, force=True)
