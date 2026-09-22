"""The Promtail pipeline names log fields that JsonFormatter / AnalyticsService emit.

Nothing else enforces that contract: renaming a field in the service would not
fail a test or log an error, it would just empty every dashboard panel. These
tests read ``docker/observability/promtail-config.yaml`` and check each field
it extracts against a record actually emitted through the service.
"""

import json
import logging
from pathlib import Path

import yaml

from canopy.core.logging import JsonFormatter
from canopy.analytics.config import AnalyticsConfig
from canopy.analytics.schemas.events import AnalyticsEventDTO
from canopy.analytics.services.analytics_service import AnalyticsService

PROMTAIL_CONFIG = Path(__file__).resolve().parents[3] / "docker" / "observability" / "promtail-config.yaml"


def _stage(name: str) -> dict:
    config = yaml.safe_load(PROMTAIL_CONFIG.read_text())
    (scrape,) = config["scrape_configs"]
    (stage,) = [s[name] for s in scrape["pipeline_stages"] if name in s]
    return stage


def _emitted_event_payload(caplog) -> dict:
    caplog.set_level(logging.INFO, logger="canopy.analytics")
    AnalyticsService(AnalyticsConfig(enabled=True)).record(AnalyticsEventDTO(event="screen_opened"))
    (record,) = [r for r in caplog.records if r.name == "canopy.analytics"]
    return json.loads(JsonFormatter().format(record))


def test_promtail_extracts_only_fields_the_service_emits(caplog):
    payload = _emitted_event_payload(caplog)
    for extracted, source_field in _stage("json")["expressions"].items():
        assert source_field in payload, f"Promtail extracts {extracted!r} from a field the log line lacks"
    # Dashboards select analytics traffic by this label value.
    assert payload["type"] == "analytics"


def test_promtail_labels_and_timestamp_come_from_extracted_fields():
    extracted = set(_stage("json")["expressions"])
    assert set(_stage("labels")) <= extracted
    assert _stage("timestamp")["source"] in extracted
