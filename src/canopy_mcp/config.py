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

from pydantic import Field, ValidationInfo, field_validator, model_validator
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
    # Build-time channel catalog (a JSON string[]) served on /channels for
    # Trace's PV autocomplete; unset or missing means no catalog (404).
    channels_file: Path | None = None
    # Read-only screen library served beside the drafts: its screens are
    # ``library/<id>`` to the tools and ``/embed/library/<id>`` to the viewer,
    # and only its ``screens/`` subtree is served. Unset means no library.
    library_dir: Path | None = None
    # Whether the seven authoring tools (new_screen, patch_node, add_node,
    # remove_node, delete_draft, convert_legacy, request_push) are offered;
    # off leaves the nine read-only tools.
    authoring: bool = True
    # Directory ``request_push`` writes its bundles into, one directory per
    # request, for an administrator to review. Unset means the tool answers
    # ``push_unavailable``.
    push_dir: Path | None = None
    # Directory holding the saved-view ``*.json`` files; unset means
    # ``<drafts_dir>/views``.
    views_dir: Path | None = None
    # Seconds the WebSocket relays keep retrying an upstream that refuses the
    # connection before they close the browser socket; 0 means one attempt.
    relay_connect_retry_s: float = Field(default=0.0, ge=0)
    # Directory of widget detail ``*.json`` files the catalog is built from
    # instead of the backend's /beaver registry; unset means the registry.
    widgets_dir: Path | None = None
    # Whether the screenshot Chromium launches with its own sandbox on
    # (``chromium_sandbox=True``); off by default, since the sandbox needs
    # privileges a container usually lacks.
    browser_sandbox: bool = False

    @field_validator(
        "api_key_file",
        "access_token_file",
        "self_url",
        "authoring",
        "views_dir",
        "relay_connect_retry_s",
        "widgets_dir",
        "library_dir",
        "push_dir",
        "browser_sandbox",
        mode="before",
    )
    @classmethod
    def _empty_counts_as_unset(cls, value: object, info: ValidationInfo) -> object:
        # A manifest that renders a variable empty gets the default, not an error.
        if value == "" and info.field_name is not None:
            return cls.model_fields[info.field_name].default
        return value

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
