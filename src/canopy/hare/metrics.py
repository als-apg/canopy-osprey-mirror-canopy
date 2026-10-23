"""Hare's Prometheus gauges — the exemplar for module-level metrics.

The pattern (docs/observability/metrics.md): module gauges expose the numbers
the module's ``/health/summary`` already reports, read from the owning service
at scrape time rather than pushed on every state change. Nothing in the
broker or the WebSocket hot path knows these gauges exist.

One collector per module, registered once by the module's ``app.py``. It
reaches the service through the module's gateway, so it rides the lifespan's
existing bind/unbind and a scrape before startup or after shutdown reads
zeros. It calls the status method once per scrape however many gauges it
yields, and a failure in that call drops this module's series from the scrape,
logged, rather than failing the exposition for every module.
"""

import logging
from collections.abc import Iterable

from prometheus_client import REGISTRY, CollectorRegistry
from prometheus_client.core import Metric, GaugeMetricFamily
from prometheus_client.registry import Collector

from canopy.hare import gateway

logger = logging.getLogger(__name__)

# (metric name, help text, key in PVBroker.status() — the dict /health/summary is built from)
GAUGES = (
    ("canopy_hare_clients", "Broker clients: WebSocket sessions plus in-process consumers such as Vole.", "clients"),
    ("canopy_hare_active_monitors", "PVs with a live EPICS monitor.", "active_monitors"),
    ("canopy_hare_subscriptions", "PV subscriptions summed over all broker clients.", "subscriptions"),
)


class HareCollector(Collector):
    """Yields Hare's gauges from one ``PVBroker.status()`` call per scrape."""

    def collect(self) -> Iterable[Metric]:
        try:
            broker = gateway.get_broker()
        except RuntimeError:
            status: dict = {}  # not bound: before startup or after shutdown
        else:
            try:
                status = broker.status()
            except Exception as exc:
                logger.warning("Hare metrics skipped this scrape: broker status failed: %r", exc)
                return
        for name, help_text, field in GAUGES:
            yield GaugeMetricFamily(name, help_text, value=status.get(field, 0))


def register(registry: CollectorRegistry = REGISTRY) -> None:
    """Register the collector; ``canopy.hare.app`` calls this once at import."""
    registry.register(HareCollector())
