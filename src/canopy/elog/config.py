"""E-log configuration.

Sourced from env vars with prefix ``CANOPY_ELOG__`` and nested delimiter
``__``. Each client app gets its own elog-plus application token, so the
browser and desktop clients never hold one::

    CANOPY_ELOG__PROVIDER=elog_plus
    CANOPY_ELOG__ELOG_PLUS__BASE_URL=https://elog.example.org/api/elog-apptoken
    CANOPY_ELOG__APPS__SQUIRREL__TOKEN=<elog-plus application token>
    CANOPY_ELOG__APPS__SQUIRREL__DEFAULT_LOGBOOK_IDS=["<logbook id>"]
    CANOPY_ELOG__APPS__TRACE__TOKEN=<elog-plus application token>

The app name is the ``{app}`` path segment of every route; env var names are
case-insensitive, so ``APPS__SQUIRREL__`` lands in ``apps["squirrel"]``.
"""

from pydantic import Field, BaseModel, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsError, SettingsConfigDict


class ElogPlusConfig(BaseModel):
    """Settings for the ``elog_plus`` provider (https://github.com/slaclab/elog-plus).

    ``base_url`` must be elog-plus' application-token ingress. At SLAC that is
    ``/api/elog-apptoken``, not ``/api/elog``: the latter sits behind Vouch,
    which strips the token header before it reaches elog-plus.
    """

    base_url: str = ""
    # Matches ELOG_PLUS_AUTH_HEADER on the elog-plus service.
    auth_header: str = "x-vouch-idp-accesstoken"


class ElogAppConfig(BaseModel):
    """One client app (``squirrel``, ``trace``, ...) and its elog-plus identity."""

    token: SecretStr | None = None
    # Logbooks a client preselects in its post dialog. JSON list in env.
    default_logbook_ids: list[str] = Field(default_factory=list)

    @field_validator("token")
    @classmethod
    def _strip_token(cls, value: SecretStr | None) -> SecretStr | None:
        # A token read from a secret file often ends in a newline, which no HTTP
        # header value may carry; every request would then fail.
        if value is None:
            return None
        return SecretStr(value.get_secret_value().strip())


class ElogConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CANOPY_ELOG__",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    # Empty disables the module: every app's config reports enabled=false.
    provider: str = ""
    elog_plus: ElogPlusConfig = ElogPlusConfig()
    apps: dict[str, ElogAppConfig] = {}
    # Outbound HTTP(S) proxy for e-log calls. Process proxy env vars are
    # deliberately ignored (the client is built with trust_env=False), but
    # SSL_CERT_FILE and SSL_CERT_DIR are still honored for a custom CA bundle.
    proxy_url: str | None = None
    timeout_s: float = 15.0
    # Reject attachment uploads larger than this (bytes) with 413.
    max_attachment_bytes: int = 20 * 1024 * 1024

    @field_validator("provider")
    @classmethod
    def _strip_provider(cls, value: str) -> str:
        return value.strip()

    @field_validator("proxy_url")
    @classmethod
    def _empty_proxy_is_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


def get_elog_config() -> ElogConfig:
    return ElogConfig()


def describe_settings_error(exc: ValidationError | SettingsError) -> str:
    """Why the ``CANOPY_ELOG__*`` settings failed to parse, safe to show any caller.

    Names the offending variables but never their values: pydantic's own
    message echoes the input, which may sit next to a token in a deploy secret.
    """
    if isinstance(exc, ValidationError):
        problems = [
            f"{_env_name(error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False, include_context=False)
        ]
        return "Invalid e-log settings: " + "; ".join(problems)
    # SettingsError names the field and source only (e.g. a DEFAULT_LOGBOOK_IDS that isn't JSON).
    return f"Invalid e-log settings: {exc}"


def _env_name(loc: tuple) -> str:
    return "CANOPY_ELOG__" + "__".join(str(part).upper() for part in loc)
