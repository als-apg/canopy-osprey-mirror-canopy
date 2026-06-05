"""EPICS Archiver Appliance HTTP adapter.

Maps the Appliance's `/retrieval/data/getData.json` and management endpoints to
Canopy's `Sample` / `BinnedPoint` / `PVMetadata` types. Owns its own
`httpx.AsyncClient` configured with the per-archiver timeout from
`ArchiverInstanceConfig`.

This commit implements raw `fetch_window`, `health`, and `close`. Binned mode,
metadata, and search land in subsequent commits.
"""

import time
from typing import Literal
from datetime import UTC, datetime

import httpx

from canopy.mole.config import ArchiverInstanceConfig
from canopy.mole.schemas.pv import PVMetadata
from canopy.contracts.sample import Sample, BinnedPoint, AlarmSeverity
from canopy.mole.adapters.base import (
    AdapterHealth,
    ArchiverTimeout,
    ArchiverBadResponse,
    ArchiverUnavailable,
    BinningNotSupported,
    PVNotFoundInArchiver,
    normalize_severity,
    register_archiver_type,
)


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
        binning: Literal["auto", "time"],
        target_buckets: int | None,
        bin_size_ns: int | None,
    ) -> list[BinnedPoint]:
        """Map intent verbs to the Appliance's `optimized_<N>(PV)` operator.

        - ``binning="auto"``: ``N = target_buckets``.
        - ``binning="time"``: ``N`` is computed so each bucket approximates
          ``bin_size_ns``. The Appliance's optimized operator is adaptive,
          so widths are approximate; per-bucket `bin_ns` is computed from
          the gap between consecutive returned timestamps.
        """
        if binning == "auto":
            if target_buckets is None or target_buckets < 1:
                raise BinningNotSupported("binning='auto' requires target_buckets >= 1")
            n = int(target_buckets)
        elif binning == "time":
            if bin_size_ns is None or bin_size_ns < 1:
                raise BinningNotSupported("binning='time' requires bin_size_ns >= 1")
            n = max((t1_ns - t0_ns) // bin_size_ns, 1)
        else:
            raise BinningNotSupported(f"Unsupported binning mode: {binning!r}")

        operator_pv = f"optimized_{n}({pv})"
        params = {
            "pv": operator_pv,
            "from": _ns_to_iso(t0_ns),
            "to": _ns_to_iso(t1_ns),
            "donotchunk": "true",
        }
        response = await self._safe_get("/retrieval/data/getData.json", params=params, pv=pv)
        payload = self._parse_json(response)
        if not isinstance(payload, list) or not payload:
            return []
        return self._parse_binned_block(payload[0])

    async def metadata(self, pv: str) -> PVMetadata:
        """Pull `EGU` and `PREC` from the Appliance's `meta` block.

        The Appliance carries metadata in every `getData.json` response. We
        query a small recent window — the `meta` block is populated even when
        the time range has no samples, so this works for newly-archived PVs.
        Other metadata fields (description, alarm/display limits) are not
        carried by the Appliance and stay `None` here; they belong to Hare /
        EPICS Connector.
        """
        # 1-hour window ending at "now". large enough to land within retention, small enough to keep the response trivial.
        now = time.time_ns()
        one_hour_ago = now - 3600 * 1_000_000_000
        params = {
            "pv": pv,
            "from": _ns_to_iso(one_hour_ago),
            "to": _ns_to_iso(now),
            "donotchunk": "true",
        }
        response = await self._safe_get("/retrieval/data/getData.json", params=params, pv=pv)
        payload = self._parse_json(response)
        if not isinstance(payload, list) or not payload:
            raise PVNotFoundInArchiver(f"No metadata for PV '{pv}' in archiver '{self.name}'")
        meta = payload[0].get("meta") or {}
        egu = meta.get("EGU")
        prec_raw = meta.get("PREC")
        try:
            precision = int(prec_raw) if prec_raw not in (None, "") else None
        except (TypeError, ValueError):
            precision = None
        return PVMetadata(
            pv=pv,
            archiver=self.name,
            units=egu,
            precision=precision,
        )

    async def search(self, pattern: str, *, limit: int) -> list[str]:
        """`/retrieval/bpl/searchForPVsRegex` returns whitespace-separated PV names as plain text."""
        try:
            response = await self._client.get("/retrieval/bpl/searchForPVsRegex", params={"regex": pattern})
        except httpx.TimeoutException as exc:
            raise ArchiverTimeout(f"Appliance '{self.name}' search timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ArchiverUnavailable(f"Appliance '{self.name}' search unreachable: {exc}") from exc
        if not response.is_success:
            sc = response.status_code
            if sc >= 500:
                raise ArchiverUnavailable(f"Appliance '{self.name}' search returned HTTP {sc}")
            raise ArchiverBadResponse(f"Appliance '{self.name}' search returned HTTP {sc}")
        # Plain text, whitespace-separated; .split() collapses runs of whitespace.
        return (response.text or "").split()[:limit]

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

    def _parse_binned_block(self, block: dict) -> list[BinnedPoint]:
        rows = block.get("data")
        if not isinstance(rows, list):
            raise ArchiverBadResponse(f"Appliance '{self.name}': missing 'data' array in binned response")
        points: list[BinnedPoint] = []
        for i, row in enumerate(rows):
            try:
                secs = int(row["secs"])
                nanos = int(row.get("nanos", 0))
                val = row["val"]
            except (KeyError, TypeError, ValueError) as exc:
                raise ArchiverBadResponse(f"Appliance '{self.name}': malformed binned row: {exc}") from exc
            ts_ns = secs * 1_000_000_000 + nanos
            if isinstance(val, (int, float)):
                # Appliance fallback: when the requested window has too few samples
                # to bin, optimized_<N> returns the raw sample value as a scalar.
                # Promote to a degenerate single-sample bin so callers see a
                # uniform BinnedPoint stream.
                v = float(val)
                mean = mn = mx = v
                std = 0.0
                cnt = 1
            elif isinstance(val, list) and len(val) >= 5:
                try:
                    mean, std, mn, mx, cnt = val[:5]
                except (ValueError, TypeError) as exc:
                    raise ArchiverBadResponse(f"Appliance '{self.name}': cannot unpack binned value: {exc}") from exc
            else:
                raise ArchiverBadResponse(
                    f"Appliance '{self.name}': expected scalar or 5-element [mean, std, min, max, count] in optimized "
                    f"response, got {val!r}"
                )
            # bin_ns from the gap to the next bucket (last bucket gets 0).
            if i + 1 < len(rows):
                try:
                    next_ts = int(rows[i + 1]["secs"]) * 1_000_000_000 + int(rows[i + 1].get("nanos", 0))
                    bin_ns = max(next_ts - ts_ns, 1)
                except (KeyError, TypeError, ValueError):
                    bin_ns = 0
            else:
                bin_ns = 0
            points.append(
                BinnedPoint(
                    ts_ns=ts_ns,
                    bin_ns=bin_ns,
                    mean=float(mean),
                    std_dev=float(std),
                    min=float(mn),
                    max=float(mx),
                    count=int(cnt),
                    # The Appliance's optimized operator does not return per-bucket
                    # severity: set NO_ALARM
                    severity=AlarmSeverity.NO_ALARM,
                )
            )
        return points

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
