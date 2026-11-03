"""`ElogPlusAdapter` against a mocked elog-plus (pytest-httpx)."""

import ssl
import json
import logging
import traceback
from datetime import UTC, datetime, timezone, timedelta

import httpx
import pytest
import certifi
from pytest_httpx import HTTPXMock

from canopy.core.errors import BadRequest
from canopy.elog.config import ElogConfig, ElogAppConfig, ElogPlusConfig
from canopy.elog.errors import ElogNotConfigured, ElogUpstreamError, ElogUpstreamTimeout
from canopy.elog.schemas.elog import ElogEntryCreateDTO
from canopy.elog.adapters.elog_plus import ElogPlusAdapter, _ssl_verify

BASE = "http://elog.test"


def _envelope(payload) -> dict:
    return {"errorCode": 0, "payload": payload}


@pytest.fixture
async def adapter():
    a = ElogPlusAdapter("squirrel", base_url=BASE, token="squirrel-token")
    yield a
    await a.close()


def _entry(**overrides) -> ElogEntryCreateDTO:
    fields = {"logbookIds": ["lb1"], "title": "Snapshot: foo", "bodyMarkdown": "# hello", "tagIds": ["t1"]}
    fields.update(overrides)
    return ElogEntryCreateDTO.model_validate(fields)


class TestRequests:
    async def test_sends_the_app_token_in_the_default_header(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=f"{BASE}/v1/logbooks?filterForAuthorizationTypes=Write", json=_envelope([]))

        await adapter.list_logbooks()

        assert httpx_mock.get_request().headers["x-vouch-idp-accesstoken"] == "squirrel-token"

    async def test_custom_auth_header(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=_envelope([]))
        adapter = ElogPlusAdapter("trace", base_url=BASE, token="trace-token", auth_header="x-app-token")
        try:
            await adapter.list_logbooks()
        finally:
            await adapter.close()

        request = httpx_mock.get_request()
        assert request.headers["x-app-token"] == "trace-token"
        assert "x-vouch-idp-accesstoken" not in request.headers

    def test_client_ignores_process_proxy_env(self, adapter):
        assert adapter._client.trust_env is False


class TestSSLVerify:
    """trust_env=False also stops httpx reading the CA variables, so the adapter reads them itself."""

    @pytest.fixture(autouse=True)
    def _no_ca_env(self, monkeypatch):
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    def test_default_is_httpx_own_bundle(self):
        assert _ssl_verify() is True

    @pytest.mark.parametrize(
        "variable, keyword",
        [("SSL_CERT_FILE", "cafile"), ("SSL_CERT_DIR", "capath")],
    )
    def test_ca_variables_build_the_context(self, monkeypatch, variable, keyword):
        calls = []
        monkeypatch.setattr(ssl, "create_default_context", lambda **kwargs: calls.append(kwargs) or "ctx")
        monkeypatch.setenv(variable, "/etc/ca")

        assert _ssl_verify() == "ctx"
        assert calls == [{keyword: "/etc/ca"}]

    async def test_a_real_ca_file_builds_an_adapter(self, monkeypatch):
        monkeypatch.setenv("SSL_CERT_FILE", certifi.where())

        assert isinstance(_ssl_verify(), ssl.SSLContext)
        await ElogPlusAdapter("squirrel", base_url=BASE, token="t").close()

    def test_a_missing_ca_file_fails_adapter_construction(self, monkeypatch, tmp_path):
        # The registry turns this into a disabled module rather than a failed startup.
        monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))

        with pytest.raises(OSError):
            ElogPlusAdapter("squirrel", base_url=BASE, token="t")

    async def test_proxy_url_is_accepted(self):
        adapter = ElogPlusAdapter("squirrel", base_url=BASE, token="t", proxy_url="http://proxy.example.test:3128")
        await adapter.close()


class TestListing:
    async def test_strips_the_payload_envelope(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{BASE}/v1/logbooks?filterForAuthorizationTypes=Write",
            json=_envelope([{"id": "lb1", "name": "Ops", "tags": []}, {"id": "lb2", "name": "Commissioning"}]),
        )

        logbooks = await adapter.list_logbooks()

        assert [(lb.id, lb.name) for lb in logbooks] == [("lb1", "Ops"), ("lb2", "Commissioning")]

    async def test_lists_only_logbooks_the_token_can_post_to(self, adapter, httpx_mock: HTTPXMock):
        # elog-plus defaults to Read, which adds every publicly readable logbook.
        httpx_mock.add_response(json=_envelope([]))

        await adapter.list_logbooks()

        assert httpx_mock.get_request().url.params["filterForAuthorizationTypes"] == "Write"

    async def test_tolerates_a_missing_envelope(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=[{"id": "lb1", "name": "Ops"}])

        assert [lb.id for lb in await adapter.list_logbooks()] == ["lb1"]

    async def test_tags_for_a_logbook(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=f"{BASE}/v1/logbooks/lb1/tags", json=_envelope([{"id": "t1", "name": "Routine"}]))

        tags = await adapter.list_tags("lb1")

        assert [(t.id, t.name) for t in tags] == [("t1", "Routine")]

    async def test_user_search_maps_ldap_fields(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{BASE}/v1/users?search=jane&limit=7",
            json=_envelope(
                [{"uid": "jdoe", "commonName": "Jane Doe", "surname": "Doe", "gecos": "Jane Doe", "mail": "jdoe@slac"}]
            ),
        )

        (user,) = await adapter.search_users("jane", 7)

        assert user.model_dump(by_alias=True) == {
            "uid": "jdoe",
            "commonName": "Jane Doe",
            "surname": "Doe",
            "gecos": "Jane Doe",
            "mail": "jdoe@slac",
        }

    async def test_unexpected_payload_shape_is_an_upstream_error(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(json=_envelope([{"name": "no id"}]))

        with pytest.raises(ElogUpstreamError, match="unexpected logbooks payload"):
            await adapter.list_logbooks()


class TestCreateEntry:
    async def test_posts_v1_json_with_markdown_rendered(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(method="POST", url=f"{BASE}/v1/entries", json=_envelope("entry-abc"))

        result = await adapter.create_entry(_entry())

        assert result.id == "entry-abc"
        assert result.url is None
        request = httpx_mock.get_request()
        assert request.headers["content-type"].startswith("application/json")
        body = json.loads(request.content)
        assert body["logbooks"] == ["lb1"]
        assert body["tags"] == ["t1"]
        assert body["title"] == "Snapshot: foo"
        assert body["text"] == "<h1>hello</h1>"
        # The app's token is the author now; no "Posted by" attribution prefix.
        assert "Posted by" not in body["text"]
        for absent in ("additionalAuthors", "important", "eventAt", "attachments"):
            assert absent not in body

    async def test_serializes_optional_fields(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(method="POST", json=_envelope("entry-xyz"))

        await adapter.create_entry(
            _entry(
                additionalAuthors=["bob@example.org"],
                attachmentIds=["att-1", "att-2"],
                important=True,
                eventAt="2026-04-29T09:30:15",
            )
        )

        body = json.loads(httpx_mock.get_request().content)
        assert body["additionalAuthors"] == ["bob@example.org"]
        assert body["attachments"] == ["att-1", "att-2"]
        assert body["important"] is True
        # A naive eventAt is already UTC: sent as-is, with no offset.
        assert body["eventAt"] == "2026-04-29T09:30:15"

    async def test_aware_event_at_is_converted_to_utc_without_offset(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(method="POST", json=_envelope("entry-1"))
        pacific = timezone(timedelta(hours=-7))

        await adapter.create_entry(_entry(eventAt=datetime(2026, 4, 29, 9, 30, 15, tzinfo=pacific)))

        assert json.loads(httpx_mock.get_request().content)["eventAt"] == "2026-04-29T16:30:15"

    async def test_follow_up_posts_to_the_parent_follow_ups_path(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(method="POST", url=f"{BASE}/v1/entries/parent-1/follow-ups", json=_envelope("child-1"))

        result = await adapter.create_follow_up("parent-1", _entry(important=True))

        assert result.id == "child-1"
        body = json.loads(httpx_mock.get_request().content)
        assert body["title"] == "Snapshot: foo"
        assert body["important"] is True

    async def test_follow_up_parent_id_is_one_quoted_path_segment(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(method="POST", json=_envelope("child-1"))

        await adapter.create_follow_up("a/b?c", _entry())

        assert httpx_mock.get_request().url.raw_path == b"/v1/entries/a%2Fb%3Fc/follow-ups"

    async def test_dot_segment_ids_are_rejected(self, adapter):
        with pytest.raises(BadRequest):
            await adapter.create_follow_up("..", _entry())

    async def test_missing_entry_id_is_an_upstream_error(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(method="POST", json=_envelope(None))

        with pytest.raises(ElogUpstreamError, match="no entry id"):
            await adapter.create_entry(_entry())


class TestRecentEntries:
    async def test_filters_by_the_tokens_own_email_and_caches_it(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=f"{BASE}/v1/users/me", json=_envelope({"mail": "squirrel@app.elog"}))
        entries_url = f"{BASE}/v1/entries?logbooks=lb1&logbooks=lb2&authors=squirrel%40app.elog&limit=3"
        httpx_mock.add_response(
            url=entries_url,
            json=_envelope(
                [{"id": "e1", "title": "Hourly", "loggedAt": "2026-10-20T17:00:00", "loggedBy": "Squirrel"}]
            ),
            is_reusable=True,
        )

        first = await adapter.list_recent_entries(["lb1", "lb2"], 3)
        await adapter.list_recent_entries(["lb1", "lb2"], 3)

        assert [r.url.path for r in httpx_mock.get_requests()] == ["/v1/users/me", "/v1/entries", "/v1/entries"]
        (entry,) = first
        assert entry.id == "e1"
        assert entry.title == "Hourly"
        assert entry.logged_by == "Squirrel"
        # elog-plus' LocalDateTime is UTC; it goes out offset-aware.
        assert entry.logged_at == datetime(2026, 10, 20, 17, 0, tzinfo=UTC)

    async def test_no_email_refuses_rather_than_listing_everyones_entries(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=f"{BASE}/v1/users/me", json=_envelope({"uid": "squirrel"}))

        with pytest.raises(ElogUpstreamError, match="no email"):
            await adapter.list_recent_entries(["lb1"], 5)

        assert [r.url.path for r in httpx_mock.get_requests()] == ["/v1/users/me"]


class TestAttachments:
    async def test_uploads_one_multipart_part_named_upload_file(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(method="POST", url=f"{BASE}/v1/attachment", status_code=201, json=_envelope("att-9"))

        result = await adapter.upload_attachment("plot.png", "image/png", b"\x89PNG data")

        assert result.id == "att-9"
        request = httpx_mock.get_request()
        assert request.headers["content-type"].startswith("multipart/form-data")
        content = request.content
        assert b'name="uploadFile"; filename="plot.png"' in content
        assert b"Content-Type: image/png" in content
        assert b"\x89PNG data" in content


class TestErrors:
    async def test_upstream_status_carries_the_error_message(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            method="POST", status_code=400, json={"errorCode": -1, "errorMessage": "Unknown author nobody@x"}
        )

        with pytest.raises(ElogUpstreamError, match="returned 400: Unknown author nobody@x"):
            await adapter.create_entry(_entry(additionalAuthors=["nobody@x"]))

    async def test_login_redirect_hints_at_the_wrong_ingress(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(status_code=302, headers={"location": "https://vouch.example.org/login"})

        with pytest.raises(ElogUpstreamError, match="application-token ingress"):
            await adapter.list_logbooks()

    async def test_timeout_maps_to_upstream_timeout(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_exception(httpx.ReadTimeout("slow"))

        with pytest.raises(ElogUpstreamTimeout):
            await adapter.list_logbooks()

    async def test_connection_failure_maps_to_upstream_error(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_exception(httpx.ConnectError("refused"))

        with pytest.raises(ElogUpstreamError, match="unreachable"):
            await adapter.list_logbooks()

    async def test_transport_error_text_is_never_echoed(self, adapter, httpx_mock: HTTPXMock, caplog):
        # What h11 raises for a token with a trailing newline: it quotes the header value.
        httpx_mock.add_exception(httpx.LocalProtocolError("Illegal header value b'squirrel-token\\n'"))
        caplog.set_level(logging.WARNING, logger="canopy.elog.adapters.elog_plus")

        with pytest.raises(ElogUpstreamError, match=r"unreachable \(LocalProtocolError\)") as excinfo:
            await adapter.list_logbooks()

        assert "LocalProtocolError" in caplog.text
        assert "squirrel-token" not in caplog.text
        assert "squirrel-token" not in "".join(traceback.format_exception(excinfo.value))

    async def test_non_json_body_is_an_upstream_error(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(text="<html>login</html>")

        with pytest.raises(ElogUpstreamError, match="non-JSON"):
            await adapter.list_logbooks()


class TestHealth:
    async def test_healthy_probe_reports_the_token_identity(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(url=f"{BASE}/v1/users/me", json=_envelope({"mail": "squirrel@app.elog"}))

        health = await adapter.health()

        assert health.healthy is True
        assert health.identity == "squirrel@app.elog"
        assert health.last_error is None
        assert health.latency_ms is not None

    @pytest.mark.parametrize("payload", [None, {}, {"uid": "squirrel"}])
    async def test_identity_without_an_email_is_unhealthy(self, adapter, httpx_mock: HTTPXMock, payload):
        # Recent entries refuse the same payload, so the probe must not report ok.
        httpx_mock.add_response(url=f"{BASE}/v1/users/me", json=_envelope(payload))

        health = await adapter.health()

        assert health.healthy is False
        assert "no email" in health.last_error

    async def test_rejected_token_is_unhealthy(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_response(status_code=401, json={"errorCode": -1, "errorMessage": "Not authenticated"})

        health = await adapter.health()

        assert health.healthy is False
        assert "401" in health.last_error

    async def test_transport_error_text_stays_out_of_last_error(self, adapter, httpx_mock: HTTPXMock):
        httpx_mock.add_exception(httpx.LocalProtocolError("Illegal header value b'squirrel-token\\n'"))

        health = await adapter.health()

        assert health.healthy is False
        assert health.last_error == "elog-plus is unreachable (LocalProtocolError)"


class TestFromConfig:
    def _config(self, **elog_plus) -> ElogConfig:
        return ElogConfig(provider="elog_plus", elog_plus=ElogPlusConfig(**elog_plus))

    async def test_builds_from_config(self):
        config = self._config(base_url=f"{BASE}/", auth_header="x-app-token")

        adapter = ElogPlusAdapter.from_config("squirrel", ElogAppConfig(token="tok"), config)
        try:
            assert adapter.app == "squirrel"
            assert adapter._client.headers["x-app-token"] == "tok"
            # httpx may or may not normalize a trailing slash onto base_url, depending on version.
            assert str(adapter._client.base_url).rstrip("/") == BASE
        finally:
            await adapter.close()

    def test_missing_base_url_is_not_configured(self):
        with pytest.raises(ElogNotConfigured, match="BASE_URL"):
            ElogPlusAdapter.from_config("squirrel", ElogAppConfig(token="tok"), self._config())
