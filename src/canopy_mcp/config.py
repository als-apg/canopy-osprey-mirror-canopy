"""Settings for canopy_mcp, read from ``CANOPY_*`` environment variables.

The variable names (``CANOPY_BASE_URL``, ``CANOPY_API_KEY``, ...) are shared
with the deployment manifests, so they stay flat rather than using the
``CANOPY_<MODULE>__`` nesting the backend modules use. ``CANOPY_API_KEY``
(singular, the key this client sends) is unrelated to the backend's
``CANOPY_API_KEYS`` registry. No ``.env`` file is read: the server is always
launched with an explicit environment.
"""

from typing import Literal
from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CANOPY_", extra="ignore")

    # Where the canopy backend (canopy.main:app) is reachable.
    base_url: str = "http://127.0.0.1:8000"
    # Read-scope key sent as X-API-Key; empty means no header is sent.
    api_key: str = ""
    # Directory holding the draft screens this server writes and serves.
    drafts_dir: Path = Path("drafts")
    # Built canopy-screen-runtime viewer (holds index.html and ir-map.json).
    viewer_dist: Path | None = None
    # OSPREY panel id; when set, draft URLs are panel-relative.
    panel_id: str | None = None
    # PV source the served viewer uses: live hare, or the mock service.
    pv_mode: Literal["hare", "mock"] = "hare"
    # Built react-trace (holds index.html); unset means no Trace tab.
    trace_dist: Path | None = None
    # Query the Trace tab opens with when the URL carries none (pv=...&range=...).
    trace_default_query: str = ""
    # Query the correlation page opens with when the URL carries none (x=...&y=...).
    correlation_default_query: str = ""
    # Build-time channel catalog (a JSON string[]) served on /channels for
    # Trace's PV autocomplete; unset or missing means no catalog (404).
    channels_file: Path | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
