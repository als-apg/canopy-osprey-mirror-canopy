"""canopy.core.logging — JSON formatter + settings-driven configuration."""

import sys
import json
import logging
import logging.config
from datetime import UTC, datetime

import pytest
from uvicorn.config import LOGGING_CONFIG as UVICORN_LOGGING_CONFIG

from canopy.core.config import get_settings
from canopy.core.logging import JsonFormatter, configure_logging


def _record(msg: str = "hello", args: tuple = (), exc_info=None, extra: dict | None = None) -> logging.LogRecord:
    record = logging.LogRecord("canopy.test", logging.INFO, __file__, 1, msg, args, exc_info)
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


class TestJsonFormatter:
    def test_basic_fields(self):
        payload = json.loads(JsonFormatter().format(_record("hello %s", args=("world",))))
        assert payload["level"] == "INFO"
        assert payload["logger"] == "canopy.test"
        assert payload["message"] == "hello world"
        # ts is parseable ISO-8601, UTC.
        assert datetime.fromisoformat(payload["ts"]).tzinfo is not None

    def test_extra_attributes_become_fields(self):
        record = _record(extra={"event": "screen_opened", "properties": {"a": 1}, "has_api_key": False})
        payload = json.loads(JsonFormatter().format(record))
        assert payload["event"] == "screen_opened"
        assert payload["properties"] == {"a": 1}
        assert payload["has_api_key"] is False

    def test_non_serializable_extra_falls_back_to_str(self):
        record = _record(extra={"when": datetime(2026, 7, 20, tzinfo=UTC)})
        payload = json.loads(JsonFormatter().format(record))
        assert payload["when"].startswith("2026-07-20")

    def test_exception_rendered_as_error_field(self):
        try:
            raise ValueError("boom")
        except ValueError:
            record = _record(exc_info=sys.exc_info())
        payload = json.loads(JsonFormatter().format(record))
        assert "ValueError: boom" in payload["error"]
        assert "Traceback" in payload["error"]

    def test_reserved_keys_in_extra_are_prefixed_not_overwritten(self):
        record = _record(extra={"level": "HACKED", "ts": "1999", "logger": "x", "error": "e", "event": "ok"})
        payload = json.loads(JsonFormatter().format(record))
        assert payload["level"] == "INFO"
        assert payload["logger"] == "canopy.test"
        assert payload["extra_level"] == "HACKED"
        assert payload["extra_ts"] == "1999"
        assert payload["extra_error"] == "e"
        assert "error" not in payload  # no exc_info on this record
        assert payload["event"] == "ok"


@pytest.fixture
def isolated_logging(monkeypatch):
    """Snapshot root logging state; configure_logging(force=True) replaces it."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


class TestConfigureLogging:
    def test_defaults_to_text_stdout_only(self, isolated_logging):
        configure_logging()
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert not isinstance(root.handlers[0].formatter, JsonFormatter)

    def test_json_format_writes_parseable_file(self, isolated_logging, tmp_path):
        log_path = tmp_path / "logs" / "backend.log"
        isolated_logging.setenv("CANOPY_LOG_FORMAT", "json")
        isolated_logging.setenv("CANOPY_LOG_PATH", str(log_path))
        get_settings.cache_clear()

        configure_logging()
        logging.getLogger("canopy.test").info("event line", extra={"event": "e1"})

        lines = log_path.read_text().splitlines()
        payload = json.loads(lines[-1])
        assert payload["message"] == "event line"
        assert payload["event"] == "e1"

    def test_file_log_rotates_at_size_cap(self, isolated_logging, tmp_path):
        log_path = tmp_path / "logs" / "backend.log"
        isolated_logging.setenv("CANOPY_LOG_PATH", str(log_path))
        isolated_logging.setattr("canopy.core.logging._LOG_MAX_BYTES", 1024)
        get_settings.cache_clear()

        configure_logging()
        for _ in range(100):
            logging.getLogger("canopy.test").info("x" * 64)

        assert log_path.with_name("backend.log.1").exists()
        assert log_path.stat().st_size <= 2048

    def test_unwritable_log_path_falls_back_to_stdout(self, isolated_logging, tmp_path):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        isolated_logging.setenv("CANOPY_LOG_PATH", str(blocker / "sub" / "backend.log"))
        get_settings.cache_clear()

        configure_logging()  # must not raise
        assert len(logging.getLogger().handlers) == 1

    def test_level_env_applies(self, isolated_logging):
        isolated_logging.setenv("CANOPY_LOG_LEVEL", "debug")
        get_settings.cache_clear()
        configure_logging()
        assert logging.getLogger().level == logging.DEBUG

    def test_unwritable_log_path_warns_through_configured_handlers(self, isolated_logging, tmp_path, capsys):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        isolated_logging.setenv("CANOPY_LOG_FORMAT", "json")
        isolated_logging.setenv("CANOPY_LOG_PATH", str(blocker / "sub" / "backend.log"))
        get_settings.cache_clear()

        configure_logging()

        captured = capsys.readouterr()
        payload = json.loads(captured.out.strip().splitlines()[-1])
        assert payload["level"] == "WARNING"
        assert "Could not open log file" in payload["message"]
        assert captured.err == ""  # not via logging.lastResort

    def test_framework_loggers_are_folded_into_root(self, isolated_logging, tmp_path):
        log_path = tmp_path / "backend.log"
        isolated_logging.setenv("CANOPY_LOG_FORMAT", "json")
        isolated_logging.setenv("CANOPY_LOG_PATH", str(log_path))
        get_settings.cache_clear()
        # What uvicorn does before it imports the app.
        logging.config.dictConfig(UVICORN_LOGGING_CONFIG)
        assert logging.getLogger("uvicorn.access").propagate is False

        configure_logging()
        logging.getLogger("uvicorn.access").info('127.0.0.1:1 - "GET /health HTTP/1.1" 200')
        logging.getLogger("uvicorn.error").info("Started server process [%d]", 1, extra={"color_message": "x"})
        logging.getLogger("arq").info("Starting worker")

        payloads = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert [p["logger"] for p in payloads] == ["uvicorn.access", "uvicorn.error", "arq"]
        assert "color_message" not in payloads[1]
        assert logging.getLogger("uvicorn.access").handlers == []
