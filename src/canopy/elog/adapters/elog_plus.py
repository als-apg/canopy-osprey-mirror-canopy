"""Adapter for elog-plus (https://github.com/slaclab/elog-plus), SLAC's e-log backend.

Ported from react-squirrel-backend, keeping what it learned about elog-plus:

- v1 endpoints only. v2's ``NewEntryDTO`` has no ``additionalAuthors`` and
  ignores unknown fields, so a v2 post would silently drop them.
- Every response is wrapped in an ``ApiResultResponse`` envelope whose
  ``payload`` holds the result.
- Entry text is stored as HTML (elog-plus parses it with jsoup to find inline
  images) and never run through a markdown processor, so we render it here.
  elog-plus doesn't clean the HTML it is sent either, so we do that here too.
- elog-plus rejects the whole post if any ``additionalAuthors`` entry is not
  an LDAP email it knows.
- ``eventAt`` is a ``LocalDateTime`` stored as UTC (the server runs in UTC),
  so it goes out in UTC with no offset, and ``loggedAt`` comes back the same way.

Each instance holds one app's application token, so elog-plus records that
app as the author of everything it posts.
"""

import os
import ssl
import time
import logging
from typing import Any
from datetime import UTC, datetime
from contextlib import contextmanager
from urllib.parse import quote

import nh3
import httpx
import markdown as markdown_lib

from canopy.core.errors import BadRequest
from canopy.elog.config import ElogConfig, ElogAppConfig
from canopy.elog.errors import ElogNotConfigured, ElogUpstreamError, ElogUpstreamTimeout
from canopy.elog.schemas.elog import (
    ElogTagDTO,
    ElogUserDTO,
    ElogLogbookDTO,
    ElogAttachmentDTO,
    ElogEntryCreateDTO,
    ElogEntryResultDTO,
    ElogRecentEntryDTO,
)
from canopy.elog.adapters.base import AdapterHealth, register_elog_provider

logger = logging.getLogger(__name__)

_MARKDOWN_EXTENSIONS = ["extra", "sane_lists", "nl2br"]
_HEALTH_TIMEOUT_S = 2.0
_ERROR_EXCERPT_CHARS = 500
_NO_EMAIL = "elog-plus /v1/users/me returned no email for this app's token"

# What survives in entry text: elog-plus's own sanitizeEntryText list (jsoup's
# Safelist.basic() plus h1-h4 and its entry-reference tag), plus the tables and
# rules markdown's "extra" emits. Everything else, raw HTML included, is dropped;
# <script> and <style> lose their content too.
_ALLOWED_TAGS = {
    "a", "b", "blockquote", "br", "cite", "code", "dd", "dl", "dt", "em", "i", "li", "ol", "p", "pre",
    "q", "small", "span", "strike", "strong", "sub", "sup", "u", "ul",
    "h1", "h2", "h3", "h4",
    "table", "thead", "tbody", "tr", "th", "td", "hr",
    "elog-entry-ref",
}  # fmt: skip
_ALLOWED_ATTRIBUTES = {"a": {"href"}, "blockquote": {"cite"}, "q": {"cite"}, "elog-entry-ref": {"id"}}
_ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}


def render_markdown(md: str) -> str:
    """Render ``md`` to HTML and clean it down to what an e-log entry may contain.

    elog-plus stores ``text`` as sent and renders it in places (email, other
    clients) that don't clean it, so a key with e-log write access must not be
    able to store script or event handlers under an app's name.
    """
    html = markdown_lib.markdown(md, extensions=_MARKDOWN_EXTENSIONS, output_format="html")
    return nh3.clean(html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRIBUTES, url_schemes=_ALLOWED_URL_SCHEMES)


def _ssl_verify() -> ssl.SSLContext | bool:
    """The ``verify=`` httpx would use with ``trust_env=True``.

    The client turns ``trust_env`` off to ignore the process's proxy variables,
    which also turns off ``SSL_CERT_FILE`` and ``SSL_CERT_DIR``. Read those two
    here so a deployment that trusts its own CA through them still works.
    """
    if cafile := os.environ.get("SSL_CERT_FILE"):
        return ssl.create_default_context(cafile=cafile)
    if capath := os.environ.get("SSL_CERT_DIR"):
        return ssl.create_default_context(capath=capath)
    return True


def _unwrap(body: Any) -> Any:
    """Strip elog-plus' ``ApiResultResponse`` envelope when present."""
    if isinstance(body, dict) and "payload" in body:
        return body["payload"]
    return body


def _to_elog_datetime(value: datetime) -> str:
    """elog-plus takes a UTC ``LocalDateTime``: convert an aware value, keep a naive one."""
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.isoformat(timespec="seconds")


def _from_elog_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _email_of(user: Any) -> str:
    if not isinstance(user, dict):
        return ""
    return user.get("mail") or user.get("email") or ""


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("errorMessage"):
        return str(body["errorMessage"])[:_ERROR_EXCERPT_CHARS]
    return response.text.strip()[:_ERROR_EXCERPT_CHARS]


def _segment(value: str) -> str:
    """Quote a client-supplied id for use as one path segment of an elog-plus URL.

    The app's token is attached to every request, so an id must never be able
    to steer the call to a different elog-plus endpoint.
    """
    if value in {".", ".."}:
        raise BadRequest(f"Invalid e-log id '{value}'")
    return quote(value, safe="")


@contextmanager
def _parsing(what: str):
    """Turn a payload that doesn't have the shape we expect into a 502."""
    try:
        yield
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ElogUpstreamError(f"elog-plus returned an unexpected {what} payload: {exc}") from exc


@register_elog_provider("elog_plus")
class ElogPlusAdapter:
    """Calls the elog-plus REST API with one app's application token."""

    provider_name: str = "elog_plus"

    def __init__(
        self,
        app: str,
        *,
        base_url: str,
        token: str,
        auth_header: str = "x-vouch-idp-accesstoken",
        proxy_url: str | None = None,
        timeout_s: float = 15.0,
    ) -> None:
        self.app = app
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={auth_header: token},
            timeout=timeout_s,
            proxy=proxy_url or None,
            # Ignore HTTP(S)_PROXY and friends: the e-log route is set by
            # CANOPY_ELOG__PROXY_URL alone, not by whatever the process inherited.
            trust_env=False,
            verify=_ssl_verify(),
        )
        # The token's own email, from /v1/users/me. Filters recent entries.
        self._email: str | None = None

    @classmethod
    def from_config(cls, app: str, app_config: ElogAppConfig, config: ElogConfig) -> "ElogPlusAdapter":
        if not config.elog_plus.base_url:
            raise ElogNotConfigured("CANOPY_ELOG__ELOG_PLUS__BASE_URL is not set")
        if app_config.token is None or not app_config.token.get_secret_value():
            raise ElogNotConfigured(f"No e-log token is configured for app '{app}'")
        return cls(
            app,
            base_url=config.elog_plus.base_url,
            token=app_config.token.get_secret_value(),
            auth_header=config.elog_plus.auth_header,
            proxy_url=config.proxy_url,
            timeout_s=config.timeout_s,
        )

    # ------------------------------------------------------------------ #
    # HTTP plumbing
    # ------------------------------------------------------------------ #

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        """Issue one request and return the unwrapped payload, or raise a domain error."""
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            logger.warning("elog-plus %s %s timed out for app %s", method, path, self.app)
            raise ElogUpstreamTimeout(f"elog-plus {method} {path} timed out") from exc
        except httpx.HTTPError as exc:
            # Name the error type only, and drop the cause from the traceback: some
            # messages quote the request headers, token included (h11's "Illegal
            # header value b'<token>\n'"), and this text reaches logs, the client's
            # error detail and the health summary.
            error = type(exc).__name__
            logger.warning("elog-plus %s %s failed for app %s: %s", method, path, self.app, error)
            raise ElogUpstreamError(f"elog-plus is unreachable ({error})") from None
        if not response.is_success:
            detail = _error_detail(response)
            logger.warning(
                "elog-plus %s %s returned %d for app %s: %s", method, path, response.status_code, self.app, detail
            )
            message = f"elog-plus returned {response.status_code}"
            if response.is_redirect or response.status_code == 401:
                # The Vouch-gated ingress answers an app token with a login redirect.
                message += " (is CANOPY_ELOG__ELOG_PLUS__BASE_URL the application-token ingress?)"
            raise ElogUpstreamError(f"{message}: {detail}" if detail else message)
        try:
            return _unwrap(response.json())
        except ValueError as exc:
            raise ElogUpstreamError(f"elog-plus {method} {path} returned a non-JSON body") from exc

    async def _own_email(self) -> str:
        """This token's email, looked up once per adapter (one adapter per app token)."""
        if self._email is None:
            email = _email_of(await self._call("GET", "/v1/users/me"))
            if not email:
                # Without it the author filter would drop out and list everyone's entries.
                raise ElogUpstreamError(_NO_EMAIL)
            self._email = email
        return self._email

    @staticmethod
    def _entry_id(payload: Any) -> str:
        entry_id = (payload.get("id") or payload.get("entryId")) if isinstance(payload, dict) else payload
        if not entry_id:
            raise ElogUpstreamError("elog-plus returned no entry id")
        return str(entry_id)

    @staticmethod
    def _entry_body(entry: ElogEntryCreateDTO) -> dict[str, Any]:
        """The v1 ``EntryNewDTO`` JSON for ``entry``."""
        body: dict[str, Any] = {
            "logbooks": list(entry.logbook_ids),
            "title": entry.title,
            "text": render_markdown(entry.body_markdown),
            "tags": list(entry.tag_ids),
        }
        if entry.attachment_ids:
            body["attachments"] = list(entry.attachment_ids)
        if entry.additional_authors:
            body["additionalAuthors"] = list(entry.additional_authors)
        if entry.important:
            body["important"] = True
        if entry.event_at is not None:
            body["eventAt"] = _to_elog_datetime(entry.event_at)
        return body

    # ------------------------------------------------------------------ #
    # Adapter Protocol
    # ------------------------------------------------------------------ #

    async def list_logbooks(self) -> list[ElogLogbookDTO]:
        # elog-plus defaults to the logbooks the token can read, plus every publicly
        # readable one. Ask for Write (which includes Admin) so callers only see logbooks
        # a post can go to. Root tokens get every logbook either way.
        items = await self._call("GET", "/v1/logbooks", params={"filterForAuthorizationTypes": "Write"}) or []
        with _parsing("logbooks"):
            return [ElogLogbookDTO(id=item["id"], name=item["name"]) for item in items]

    async def list_tags(self, logbook_id: str) -> list[ElogTagDTO]:
        items = await self._call("GET", f"/v1/logbooks/{_segment(logbook_id)}/tags") or []
        with _parsing("tags"):
            return [ElogTagDTO(id=item["id"], name=item["name"]) for item in items]

    async def search_users(self, search: str, limit: int) -> list[ElogUserDTO]:
        items = await self._call("GET", "/v1/users", params={"search": search, "limit": limit}) or []
        with _parsing("users"):
            return [
                ElogUserDTO(
                    uid=item.get("uid", item.get("id", "")),
                    common_name=item.get("commonName", item.get("name", "")),
                    surname=item.get("surname", ""),
                    gecos=item.get("gecos", ""),
                    mail=item.get("mail", item.get("email", "")),
                )
                for item in items
            ]

    async def list_recent_entries(self, logbook_ids: list[str], limit: int) -> list[ElogRecentEntryDTO]:
        email = await self._own_email()
        params = [("logbooks", logbook_id) for logbook_id in logbook_ids]
        params += [("authors", email), ("limit", str(limit))]
        items = await self._call("GET", "/v1/entries", params=params) or []
        with _parsing("entries"):
            return [
                ElogRecentEntryDTO(
                    id=item["id"],
                    title=item.get("title", ""),
                    logged_at=_from_elog_datetime(item.get("loggedAt") or item.get("eventAt")),
                    logged_by=item.get("loggedBy", ""),
                )
                for item in items
            ]

    async def upload_attachment(self, filename: str, content_type: str | None, data: bytes) -> ElogAttachmentDTO:
        files = {"uploadFile": (filename, data, content_type or "application/octet-stream")}
        payload = await self._call("POST", "/v1/attachment", files=files)
        if not payload or not isinstance(payload, str | int):
            raise ElogUpstreamError("elog-plus returned no attachment id")
        return ElogAttachmentDTO(id=str(payload))

    async def create_entry(self, entry: ElogEntryCreateDTO) -> ElogEntryResultDTO:
        payload = await self._call("POST", "/v1/entries", json=self._entry_body(entry))
        return ElogEntryResultDTO(id=self._entry_id(payload))

    async def create_follow_up(self, parent_entry_id: str, entry: ElogEntryCreateDTO) -> ElogEntryResultDTO:
        payload = await self._call(
            "POST", f"/v1/entries/{_segment(parent_entry_id)}/follow-ups", json=self._entry_body(entry)
        )
        return ElogEntryResultDTO(id=self._entry_id(payload))

    async def health(self) -> AdapterHealth:
        """Probe with ``GET /v1/users/me``: proves both reachability and that the token is accepted."""
        start = time.monotonic()
        try:
            email = _email_of(await self._call("GET", "/v1/users/me", timeout=_HEALTH_TIMEOUT_S))
            if not email:
                # Recent entries would fail on the same payload, so don't report ok.
                raise ElogUpstreamError(_NO_EMAIL)
        except Exception as exc:
            return AdapterHealth(healthy=False, latency_ms=(time.monotonic() - start) * 1000, last_error=str(exc))
        self._email = email
        return AdapterHealth(healthy=True, identity=email, latency_ms=(time.monotonic() - start) * 1000)

    async def close(self) -> None:
        if not self._client.is_closed:
            await self._client.aclose()
