"""Settings for canopy_mcp, read from ``CANOPY_*`` environment variables.

Every variable, its default and its meaning is in the table of the package
README (*Switches*); the comments below say only what the code needs. The names
stay flat (``CANOPY_BASE_URL``, not the ``CANOPY_<MODULE>__`` nesting the backend
modules use), and no ``.env`` file is read: the server is always launched with
an explicit environment. Two rules hold for every variable:

- An empty value counts as unset, except for ``CANOPY_BASE_URL``,
  ``CANOPY_DRAFTS_DIR`` and ``CANOPY_PV_MODE``, which must say something.
- A secret named by a ``*_FILE`` variable comes only from that file: one that
  cannot supply it raises :class:`~canopy_mcp.access.CredentialFileError`
  instead of falling back, and the PUT relay switched on without its secrets
  raises :class:`RelayConfigError`. Either way nothing is served.
"""

import re
from typing import Literal
from pathlib import Path
from functools import lru_cache

from pydantic import (
    Field,
    PrivateAttr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from canopy_mcp.access import read_secret_file

#: The address ``python -m canopy_mcp --http`` binds when not told otherwise.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
#: Where a server that was not told its own address is reachable.
DEFAULT_SELF_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"

#: Default bound, in seconds, of one relayed PV write.
DEFAULT_PUT_RELAY_TIMEOUT_S = 27.0

#: One ``CANOPY_HOST_STYLESHEETS`` entry: a relative ``.css`` path whose
#: segments are plain names that do not start with a dot.
HOST_STYLESHEET_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(/[A-Za-z0-9_-][A-Za-z0-9._-]*)*\.css$")
#: ``CANOPY_URL_PREFIX``: an absolute path of plain segments, without a trailing slash.
URL_PREFIX_RE = re.compile(r"^(/[A-Za-z0-9._~-]+)+$")
#: One ``CANOPY_PROXY_MARK_HEADERS`` entry: an HTTP header name.
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")

# Variables that keep failing on an empty value instead of falling back to the default.
_EMPTY_IS_AN_ERROR = frozenset({"base_url", "drafts_dir", "pv_mode"})


class RelayConfigError(RuntimeError):
    """The PUT relay is switched on without the secrets it must run behind."""


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
    # Address the server is reachable at, for its own screenshot browser and, without
    # a url_prefix, for the URLs the tools return; the launcher fills it in from the
    # port it serves when it is unset.
    self_url: str | None = None
    # Absolute base URL the tools' screen URLs name when it differs from self_url
    # (a server reached under another name than its own); unset means self_url.
    # The origins of self_url and public_url are also the only non-loopback origins
    # the API's WebSocket relays accept (canopy_mcp.http.is_cross_site_handshake).
    public_url: str | None = None
    # Path prefix an embedding host serves this server under (``/panel/canopy``);
    # when set, the URLs the tools return are that prefix plus the server path.
    url_prefix: str | None = None
    # Further request headers (whitespace-separated names) that mark a request
    # as having come through the embedding host's proxy, besides X-Forwarded-Prefix.
    proxy_mark_headers: str = ""
    # Directory holding the draft screens this server writes and serves.
    drafts_dir: Path = Path("drafts")
    # Built canopy-screen-runtime viewer (holds index.html and ir-map.json).
    viewer_dist: Path | None = None
    # PV source the served viewer uses: live hare, or the mock service.
    pv_mode: Literal["hare", "mock"] = "hare"
    # Built react-trace (holds index.html); unset means no Trace tab.
    trace_dist: Path | None = None
    # Query the Trace tab opens with when the URL carries none (pv=...&range=...).
    trace_default_query: str = ""
    # Query the correlation page opens with when the URL carries none (x=...&y=...).
    correlation_default_query: str = ""
    # Query the waveform page opens with when the URL carries none (pv=...).
    waveform_default_query: str = ""
    # Build-time channel catalog (a JSON string[]) served on /channels for
    # Trace's PV autocomplete; unset or missing means no catalog (404).
    channels_file: Path | None = None
    # Read-only screen library served beside the drafts: its screens are
    # ``library/<id>`` to the tools and ``/embed/library/<id>`` to the viewer,
    # and only its ``screens/`` subtree is served. Unset means no library.
    library_dir: Path | None = None
    # Whether the authoring tools (server.AUTHORING_TOOLS) are offered; off
    # leaves the tools that only read.
    authoring: bool = True
    # Seconds the WebSocket relays keep retrying an upstream that refuses the
    # connection before they close the browser socket; 0 means one attempt.
    relay_connect_retry_s: float = Field(default=0.0, ge=0)
    # Staged spec directory (``ir/screen-ir-<version>.schema.json``,
    # ``widgets/<ir id>.props.schema.json``, ``SPEC-SOURCE``) the check pass
    # validates against; unset means ``<parent of viewer_dist>/spec``.
    spec_dir: Path | None = None
    # Whether the screenshot Chromium launches with its own sandbox on
    # (``chromium_sandbox=True``); off by default, since the sandbox needs
    # privileges a container usually lacks.
    browser_sandbox: bool = False
    # Whether ``PUT /hare/v1/pvs/{pv}`` is relayed to the backend (the one
    # write route); off leaves it a 405 like every other mutating path.
    put_relay: bool = False
    # File holding the write-scope key the relay sends upstream as X-API-Key.
    # Required with the relay on; there is no environment form of this key.
    write_api_key_file: Path | None = None
    # Seconds one relayed write may take upstream before the server answers 504.
    put_relay_timeout_s: float = Field(default=DEFAULT_PUT_RELAY_TIMEOUT_S, gt=0)
    # Whether the viewer asks for a confirmation before each write (config.json
    # ``confirmWrites``); off by default.
    confirm_writes: bool = False
    # The embedding host's stylesheets, as whitespace-separated relative
    # ``.css`` paths the served pages link under the proxy prefix; empty means
    # none. Read the split form from ``host_stylesheet_paths``.
    host_stylesheets: str = ""
    # Library screen id the viewer opens on (config.json ``home``); unset means
    # the viewer's own default. A relative path inside the library only.
    home_screen: str | None = None
    # The deployment's color scheme file, the one canopy.core.color_scheme serves;
    # set, ``convert_legacy`` overwrites only with conversions made against it.
    theme_file: Path | None = None

    _write_api_key: str = PrivateAttr(default="")

    @field_validator("*", mode="before")
    @classmethod
    def _empty_counts_as_unset(cls, value: object, info: ValidationInfo) -> object:
        # A manifest that renders a variable empty gets the default, not an error.
        if value == "" and info.field_name is not None and info.field_name not in _EMPTY_IS_AN_ERROR:
            return cls.model_fields[info.field_name].default
        return value

    @field_validator("host_stylesheets")
    @classmethod
    def _refuse_a_host_stylesheet_outside_the_grammar(cls, value: str) -> str:
        # Each entry becomes an href under the proxy prefix, so one that could
        # leave the prefix or the attribute stops the server here.
        for entry in value.split():
            if HOST_STYLESHEET_RE.fullmatch(entry) is None:
                raise ValueError(
                    f"CANOPY_HOST_STYLESHEETS entry {entry!r} is not a relative .css path "
                    f"(each entry must match {HOST_STYLESHEET_RE.pattern})"
                )
        return value

    @field_validator("url_prefix")
    @classmethod
    def _refuse_a_url_prefix_outside_the_grammar(cls, value: str | None) -> str | None:
        if value is not None and URL_PREFIX_RE.fullmatch(value) is None:
            raise ValueError(
                f"CANOPY_URL_PREFIX {value!r} is not an absolute path of plain segments without a trailing slash"
            )
        return value

    @field_validator("proxy_mark_headers")
    @classmethod
    def _refuse_a_proxy_mark_that_is_not_a_header_name(cls, value: str) -> str:
        for entry in value.split():
            if HEADER_NAME_RE.fullmatch(entry) is None:
                raise ValueError(f"CANOPY_PROXY_MARK_HEADERS entry {entry!r} is not an HTTP header name")
        return value

    @field_validator("home_screen")
    @classmethod
    def _refuse_a_home_screen_outside_the_library(cls, value: str | None) -> str | None:
        # The id is resolved under the library by the viewer, so one that could
        # leave it (or name nothing) stops the server here.
        if value is None:
            return value
        segments = value.split("/")
        if "\\" in value or any(segment in ("", ".", "..") for segment in segments):
            raise ValueError(
                f"CANOPY_HOME_SCREEN {value!r} is not a library screen id "
                "(a relative path without a leading slash or a '.', '..' or empty segment)"
            )
        return value

    @model_validator(mode="after")
    def _read_the_key_file(self) -> "Settings":
        # Resolved here so every reader of ``api_key`` (the client factory,
        # the relays, the tools) sees the file's key without knowing about it.
        if self.api_key_file is not None:
            self.api_key = read_secret_file("CANOPY_API_KEY_FILE", self.api_key_file)
        return self

    @model_validator(mode="after")
    def _read_the_write_key_file(self) -> "Settings":
        # A write key file that is named must supply the key, relay on or off.
        if self.write_api_key_file is not None:
            self._write_api_key = read_secret_file("CANOPY_WRITE_API_KEY_FILE", self.write_api_key_file)
        return self

    @model_validator(mode="after")
    def _refuse_a_relay_without_its_secrets(self) -> "Settings":
        if not self.put_relay:
            return self
        if self.write_api_key_file is None:
            raise RelayConfigError("CANOPY_PUT_RELAY is on but CANOPY_WRITE_API_KEY_FILE is not set")
        if self.access_token_file is None:
            raise RelayConfigError("CANOPY_PUT_RELAY is on but CANOPY_ACCESS_TOKEN_FILE is not set")
        return self

    @property
    def staged_spec_dir(self) -> Path | None:
        """The spec staging directory: ``spec_dir``, else ``spec/`` beside the viewer dist, else ``None``."""
        if self.spec_dir is not None:
            return Path(self.spec_dir)
        if self.viewer_dist is not None:
            return Path(self.viewer_dist).parent / "spec"
        return None

    @property
    def host_stylesheet_paths(self) -> list[str]:
        """The validated ``host_stylesheets`` entries in order; empty when the setting is."""
        return self.host_stylesheets.split()

    @property
    def proxy_mark_header_names(self) -> tuple[str, ...]:
        """``X-Forwarded-Prefix`` and the ``proxy_mark_headers`` entries, lower-cased."""
        return ("x-forwarded-prefix", *(name.lower() for name in self.proxy_mark_headers.split()))

    @property
    def screen_base_url(self) -> str:
        """The absolute base of the tools' screen URLs without a prefix: ``public_url``, ``self_url``, the default."""
        return (self.public_url or self.self_url or DEFAULT_SELF_URL).rstrip("/")

    @property
    def write_api_key(self) -> str:
        """The key the PUT relay sends upstream; empty when no write-key file is configured."""
        return self._write_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
