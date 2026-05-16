"""EPICS Archiver Appliance HTTP adapter.

Maps the Appliance's `/retrieval/data/getData.json` and management endpoints to
Canopy's `Sample` / `BinnedPoint` / `PVMetadata` types. Owns its own
`httpx.AsyncClient` configured with the per-archiver timeout from
`ArchiverInstanceConfig`.

This commit implements raw `fetch_window`, `health`, and `close`. Binned mode,
metadata, and search land in subsequent commits.
"""

import time
from datetime import UTC, datetime

import httpx

from canopy.contracts.sample import Sample, BinnedPoint
from canopy.mole.config import ArchiverInstanceConfig
from canopy.mole.adapters.base import (
    AdapterHealth,
    ArchiverTimeout,
    ArchiverBadResponse,
    ArchiverUnavailable,
    PVNotFoundInArchiver,
    normalize_severity,
    register_archiver_type,
)
from canopy.mole.schemas.pv import PVMetadata


def _ns_to_iso(ts_ns: int) -> str:
    """Convert UTC nanoseconds to Appliance-friendly ISO-8601 with ms precision and `Z` suffix."""
    dt = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=UTC)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@register_archiver_type("epics_archiver_appliance")
class EpicsArchiverApplianceAdapter:
    """Adapter for one EPICS Archiver Appliance instance."""

    type_name: str = "epics_archiver_appliance"

    def __init__(self, name: str, url: str, timeout_s: float = 30.0) -> None:
        self.name = name
        self._url = url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._url,
            timeout=httpx.Timeout(timeout_s),
        )

    @classmethod
    def from_config(cls, name: str, cfg: ArchiverInstanceConfig) -> "EpicsArchiverApplianceAdapter":
        return cls(name=name, url=cfg.url, timeout_s=cfg.timeout_s)

    async def close(self) -> None:
        if not self._client.is_closed:
            await self._client.aclose()

    async def health(self) -> AdapterHealth:
        """Reachability probe via the Appliance's `getApplianceInfo` management endpoint."""
        start = time.monotonic()
        try:
            response = await self._client.get("/mgmt/bpl/getApplianceInfo", timeout=2.0)
            ok = response.status_code < 500
            return AdapterHealth(
                healthy=ok,
                latency_ms=(time.monotonic() - start) * 1000,
                last_error=None if ok else f"HTTP {response.status_code}",
                last_check_ts_ns=time.time_ns(),
            )
        except Exception as exc:
            return AdapterHealth(
                healthy=False,
                latency_ms=(time.monotonic() - start) * 1000,
                last_error=str(exc),
                last_check_ts_ns=time.time_ns(),
            )

    async def fetch_window(
        self,
        pv: str,
        t0_ns: int,
        t1_ns: int,
        *,
        max_points: int | None = None,  # ignored in raw mode; service decimates if needed
    ) -> list[Sample]:
        """Raw `getData.json` query — one PV, time-window. Returns stored samples."""
        params = {
            "pv": pv,
            "from": _ns_to_iso(t0_ns),
            "to": _ns_to_iso(t1_ns),
            "donotchunk": "true",
        }
        response = await self._safe_get("/retrieval/data/getData.json", params=params, pv=pv)
        payload = self._parse_json(response)
        if not isinstance(payload, list) or not payload:
            return []
        return self._parse_raw_block(payload[0])

    async def fetch_binned(
        self,
        pv: str,
        t0_ns: int,
        t1_ns: int,
        *,
        binning,
        target_buckets,
        bin_size_ns,
    ) -> list[BinnedPoint]:
        raise NotImplementedError("Filled in commit 6")

    async def metadata(self, pv: str) -> PVMetadata:
        raise NotImplementedError("Filled in commit 7")

    async def search(self, pattern: str, *, limit: int) -> list[str]:
        raise NotImplementedError("Filled in commit 7")

    # --- internal helpers ------------------------------------------------- #

    async def _safe_get(self, path: str, *, params: dict, pv: str) -> httpx.Response:
        try:
            response = await self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise ArchiverTimeout(f"Appliance {self.name} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ArchiverUnavailable(f"Appliance {self.name} unreachable: {exc}") from exc
        self._check_status(response, pv)
        return response

    def _check_status(self, response: httpx.Response, pv: str) -> None:
        if response.is_success:
            return
        sc = response.status_code
        if sc in (400, 404):
            # The Appliance returns 400 for unknown PVs in most deployments.
            raise PVNotFoundInArchiver(f"PV '{pv}' not in archiver '{self.name}' (HTTP {sc})")
        if sc >= 500:
            raise ArchiverUnavailable(f"Appliance '{self.name}' returned HTTP {sc}")
        raise ArchiverBadResponse(f"Appliance '{self.name}' returned unexpected HTTP {sc}")

    def _parse_json(self, response: httpx.Response):
        try:
            return response.json()
        except ValueError as exc:
            raise ArchiverBadResponse(f"Appliance '{self.name}' returned invalid JSON: {exc}") from exc

    def _parse_raw_block(self, block: dict) -> list[Sample]:
        rows = block.get("data")
        if not isinstance(rows, list):
            raise ArchiverBadResponse(f"Appliance '{self.name}': missing 'data' array in response")
        samples: list[Sample] = []
        for row in rows:
            try:
                secs = int(row["secs"])
                nanos = int(row.get("nanos", 0))
                value = row["val"]
                severity = row.get("severity")
            except (KeyError, TypeError, ValueError) as exc:
                raise ArchiverBadResponse(f"Appliance '{self.name}': malformed sample row: {exc}") from exc
            samples.append(
                Sample(
                    ts_ns=secs * 1_000_000_000 + nanos,
                    value=value,
                    severity=normalize_severity(severity),
                    source="archive",
                )
            )
        return samples
