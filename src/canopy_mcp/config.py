"""Settings for canopy_mcp, read from ``CANOPY_*`` environment variables.

The variable names (``CANOPY_BASE_URL``, ``CANOPY_API_KEY``, ...) are shared
with the deployment manifests, so they stay flat rather than using the
``CANOPY_<MODULE>__`` nesting the backend modules use. ``CANOPY_API_KEY``
(singular, the key this client sends) is unrelated to the backend's
``CANOPY_API_KEYS`` registry. No ``.env`` file is read: the server is always
launched with an explicit environment.

A secret can come from a file instead of the environment
(``CANOPY_API_KEY_FILE``, ``CANOPY_ACCESS_TOKEN_FILE``). A file variable that
is set is the only source of its secret, so a file that cannot supply one
raises :class:`~canopy_mcp.access.CredentialFileError` instead of falling back.
"""

from typing import Literal
from pathlib import Path
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from canopy_mcp.access import read_secret_file


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CANOPY_", extra="ignore")

    # Where the canopy backend (canopy.main:app) is reachable.
    base_url: str = "http://127.0.0.1:8000"
    # Read-scope key sent as X-API-Key; empty means no header is sent.
    api_key: str = ""
    # File holding that key. When set it is the only source: CANOPY_API_KEY is ignored.
    api_key_file: Path | None = None
    # File holding the credential every request but GET /health must carry
    # as X-Canopy-Access; unset means no credential is asked for.
    access_token_file: Path | None = None
    # Address the server is reachable at for its own screenshot browser;
    # unset means the launcher derives it from the port it binds.
    self_url: str | None = None
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

    @field_validator("api_key_file", "access_token_file", "self_url", mode="before")
    @classmethod
    def _empty_counts_as_unset(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def _read_the_key_file(self) -> "Settings":
        # Resolved here so every reader of ``api_key`` (the client factory,
        # the relays, the tools) sees the file's key without knowing about it.
        if self.api_key_file is not None:
            self.api_key = read_secret_file("CANOPY_API_KEY_FILE", self.api_key_file)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
