"""`/elog/v1/apps/{app}/...` on the composed app, backed by fake adapters."""

import logging

import pytest

from tests.conftest import READ_HEADERS, WRITE_HEADERS
from canopy.elog.app import app as elog_app
from canopy.elog.errors import ElogUpstreamError, ElogUpstreamTimeout
from canopy.elog.dependencies import get_elog_service
from canopy.elog.schemas.elog import MAX_BODY_MARKDOWN_CHARS
from tests.canopy.elog._fakes import FakeElogAdapter
from tests.canopy.elog.conftest import DEFAULT_LOGBOOK_IDS, build_service

ENTRY = {"logbookIds": ["lb-ops"], "title": "Snapshot saved", "bodyMarkdown": "**All** good"}


@pytest.fixture
def use_service(elog_client, monkeypatch):
    """Swap the service behind the running client for this test."""

    def _use(service) -> None:
        monkeypatch.setitem(elog_app.dependency_overrides, get_elog_service, lambda: service)

    return _use


@pytest.fixture
def disabled_client(elog_client, use_service):
    use_service(build_service({}, enabled=False))
    return elog_client


class TestConfig:
    def test_enabled_app_needs_no_api_key(self, elog_client):
        r = elog_client.get("/elog/v1/apps/squirrel/config")

        assert r.status_code == 200, r.text
        assert r.json() == {
            "enabled": True,
            "provider": "fake",
            "defaultLogbookIds": DEFAULT_LOGBOOK_IDS,
            "reason": None,
        }

    def test_app_without_a_token_is_disabled_with_a_reason(self, elog_client):
        r = elog_client.get("/elog/v1/apps/trace/config")

        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False
        assert body["provider"] == "fake"
        assert body["defaultLogbookIds"] == []
        assert "trace" in body["reason"]

    def test_disabled_module_reports_why(self, disabled_client):
        body = disabled_client.get("/elog/v1/apps/squirrel/config").json()

        assert body == {
            "enabled": False,
            "provider": None,
            "defaultLogbookIds": [],
            "reason": "No e-log provider is configured (CANOPY_ELOG__PROVIDER is empty)",
        }

    def test_invalid_app_name_is_400(self, elog_client):
        r = elog_client.get("/elog/v1/apps/Squirrel!/config")

        assert r.status_code == 400
        assert r.json()["code"] == "invalid_app_name"


class TestReadRoutes:
    def test_logbooks(self, elog_client, squirrel_adapter):
        r = elog_client.get("/elog/v1/apps/squirrel/logbooks", headers=READ_HEADERS)

        assert r.status_code == 200, r.text
        assert r.json() == [{"id": "lb-ops", "name": "Operations"}]
        assert squirrel_adapter.calls == [("list_logbooks",)]

    def test_tags(self, elog_client, squirrel_adapter):
        r = elog_client.get("/elog/v1/apps/squirrel/logbooks/lb-ops/tags", headers=READ_HEADERS)

        assert r.status_code == 200
        assert r.json() == [{"id": "tag-1", "name": "routine"}]
        assert squirrel_adapter.calls == [("list_tags", "lb-ops")]

    def test_user_search(self, elog_client, squirrel_adapter):
        r = elog_client.get("/elog/v1/apps/squirrel/users", params={"search": "jane", "limit": 5}, headers=READ_HEADERS)

        assert r.status_code == 200
        assert r.json() == [
            {
                "uid": "jdoe",
                "commonName": "Jane Doe",
                "surname": "Doe",
                "gecos": "Jane Doe",
                "mail": "jdoe@example.org",
            }
        ]
        assert squirrel_adapter.calls == [("search_users", "jane", 5)]

    @pytest.mark.parametrize("params", [{"search": ""}, {"search": "x" * 101}, {"search": "j", "limit": 0}])
    def test_user_search_bounds_are_422(self, elog_client, params):
        r = elog_client.get("/elog/v1/apps/squirrel/users", params=params, headers=READ_HEADERS)

        assert r.status_code == 422

    def test_recent_entries_takes_repeated_logbook_ids(self, elog_client, squirrel_adapter):
        r = elog_client.get(
            "/elog/v1/apps/squirrel/recent-entries?logbookId=a&logbookId=b&limit=3", headers=READ_HEADERS
        )

        assert r.status_code == 200, r.text
        assert r.json() == [
            {"id": "e-1", "title": "Earlier", "loggedAt": "2026-10-20T17:00:00Z", "loggedBy": "squirrel"}
        ]
        assert squirrel_adapter.calls == [("list_recent_entries", ["a", "b"], 3)]

    @pytest.mark.parametrize("query", ["", "?logbookId=a&limit=21", "?logbookId=a&limit=0"])
    def test_recent_entries_bounds_are_422(self, elog_client, query):
        r = elog_client.get(f"/elog/v1/apps/squirrel/recent-entries{query}", headers=READ_HEADERS)

        assert r.status_code == 422

    def test_no_api_key_is_401(self, elog_client):
        r = elog_client.get("/elog/v1/apps/squirrel/logbooks")

        assert r.status_code == 401
        assert r.json()["code"] == "invalid_api_key"

    def test_invalid_app_name_is_400(self, elog_client):
        r = elog_client.get("/elog/v1/apps/BAD/logbooks", headers=READ_HEADERS)

        assert r.status_code == 400
        assert r.json()["code"] == "invalid_app_name"

    def test_unconfigured_app_is_404(self, elog_client):
        r = elog_client.get("/elog/v1/apps/trace/logbooks", headers=READ_HEADERS)

        assert r.status_code == 404
        assert r.json()["code"] == "elog_app_not_found"

    def test_disabled_module_is_503(self, disabled_client):
        r = disabled_client.get("/elog/v1/apps/squirrel/logbooks", headers=READ_HEADERS)

        assert r.status_code == 503
        assert r.json()["code"] == "elog_not_configured"


class TestModuleScope:
    def test_key_scoped_to_squirrel_only_is_403(self, elog_client, bind_env_keys):
        bind_env_keys("sq-only:read+write@squirrel")

        r = elog_client.get("/elog/v1/apps/squirrel/logbooks", headers={"X-API-Key": "sq-only"})

        assert r.status_code == 403
        assert r.json()["code"] == "insufficient_module_access"

    def test_key_scoped_to_squirrel_and_elog_can_post(self, elog_client, bind_env_keys):
        bind_env_keys("sq-elog:read+write@squirrel+elog")

        r = elog_client.post("/elog/v1/apps/squirrel/entries", json=ENTRY, headers={"X-API-Key": "sq-elog"})

        assert r.status_code == 200, r.text

    def test_read_only_key_cannot_write(self, elog_client):
        entries = elog_client.post("/elog/v1/apps/squirrel/entries", json=ENTRY, headers=READ_HEADERS)
        upload = elog_client.post(
            "/elog/v1/apps/squirrel/attachments", files={"file": ("a.txt", b"x")}, headers=READ_HEADERS
        )

        for r in (entries, upload):
            assert r.status_code == 403
            assert r.json()["code"] == "insufficient_write_access"

    def test_write_only_key_cannot_read(self, elog_client, bind_env_keys):
        bind_env_keys("w-only:write@elog")

        r = elog_client.get("/elog/v1/apps/squirrel/logbooks", headers={"X-API-Key": "w-only"})

        assert r.status_code == 403
        assert r.json()["code"] == "insufficient_read_access"


class TestEntries:
    def test_creates_an_entry(self, elog_client, squirrel_adapter):
        r = elog_client.post(
            "/elog/v1/apps/squirrel/entries",
            json={
                **ENTRY,
                "tagIds": ["tag-1"],
                "attachmentIds": ["att-1"],
                "additionalAuthors": ["jdoe@example.org"],
                "important": True,
                "eventAt": "2026-10-20T10:00:00-07:00",
            },
            headers=WRITE_HEADERS,
        )

        assert r.status_code == 200, r.text
        assert r.json() == {"id": "squirrel-entry-1", "url": None}
        ((op, entry),) = squirrel_adapter.calls
        assert op == "create_entry"
        assert entry.logbook_ids == ["lb-ops"]
        assert entry.body_markdown == "**All** good"
        assert entry.tag_ids == ["tag-1"]
        assert entry.attachment_ids == ["att-1"]
        assert entry.additional_authors == ["jdoe@example.org"]
        assert entry.important is True

    def test_a_body_over_the_limit_is_422(self, elog_client, squirrel_adapter):
        r = elog_client.post(
            "/elog/v1/apps/squirrel/entries",
            json={**ENTRY, "bodyMarkdown": "x" * (MAX_BODY_MARKDOWN_CHARS + 1)},
            headers=WRITE_HEADERS,
        )

        assert r.status_code == 422
        assert squirrel_adapter.calls == []

    def test_follows_up_entry_id_posts_a_follow_up(self, elog_client, squirrel_adapter):
        r = elog_client.post(
            "/elog/v1/apps/squirrel/entries", json={**ENTRY, "followsUpEntryId": "parent-1"}, headers=WRITE_HEADERS
        )

        assert r.status_code == 200, r.text
        ((op, parent, _entry),) = squirrel_adapter.calls
        assert (op, parent) == ("create_follow_up", "parent-1")

    def test_follow_up_unsupported_is_501(self, elog_client, squirrel_adapter):
        squirrel_adapter.follow_ups_supported = False

        r = elog_client.post(
            "/elog/v1/apps/squirrel/entries", json={**ENTRY, "followsUpEntryId": "parent-1"}, headers=WRITE_HEADERS
        )

        assert r.status_code == 501
        assert r.json()["code"] == "elog_not_supported"

    @pytest.mark.parametrize(
        "error, status, code",
        [
            (ElogUpstreamError("elog-plus returned 400: Unknown author"), 502, "elog_upstream_error"),
            (ElogUpstreamTimeout("elog-plus POST /v1/entries timed out"), 504, "elog_upstream_timeout"),
        ],
    )
    def test_upstream_failures_map_to_gateway_errors(self, elog_client, squirrel_adapter, error, status, code):
        squirrel_adapter.error = error

        r = elog_client.post("/elog/v1/apps/squirrel/entries", json=ENTRY, headers=WRITE_HEADERS)

        assert r.status_code == status
        assert r.json() == {"code": code, "detail": error.detail}

    @pytest.mark.parametrize(
        "body",
        [
            {**ENTRY, "logbookIds": []},
            {**ENTRY, "logbookIds": [""]},
            {**ENTRY, "title": ""},
            {**ENTRY, "title": "x" * 256},
            {"logbookIds": ["lb-ops"], "title": "no body"},
            {**ENTRY, "followsUpEntryId": ""},
        ],
    )
    def test_invalid_bodies_are_422(self, elog_client, squirrel_adapter, body):
        r = elog_client.post("/elog/v1/apps/squirrel/entries", json=body, headers=WRITE_HEADERS)

        assert r.status_code == 422
        assert squirrel_adapter.calls == []

    def test_logs_who_posted_where_but_not_the_body_or_token(self, elog_client, caplog):
        caplog.set_level(logging.INFO, logger="canopy.elog.services.elog_service")

        r = elog_client.post("/elog/v1/apps/squirrel/entries", json=ENTRY, headers=WRITE_HEADERS)

        assert r.status_code == 200
        (record,) = [rec for rec in caplog.records if rec.name == "canopy.elog.services.elog_service"]
        message = record.getMessage()
        assert "app=squirrel" in message
        # Env keys are named by position: test-write is entry #2 in CANOPY_API_KEYS.
        assert "api_key=env:2" in message
        assert "logbooks=lb-ops" in message
        assert "entry=squirrel-entry-1" in message
        assert "All" not in message
        assert "test-write" not in message


class TestAttachments:
    def test_uploads_the_file_part(self, elog_client, squirrel_adapter):
        r = elog_client.post(
            "/elog/v1/apps/squirrel/attachments",
            files={"file": ("plot.png", b"\x89PNG", "image/png")},
            headers=WRITE_HEADERS,
        )

        assert r.status_code == 200, r.text
        assert r.json() == {"id": "att-1"}
        assert squirrel_adapter.calls == [("upload_attachment", "plot.png", "image/png", b"\x89PNG")]

    def test_over_the_limit_is_413(self, elog_client, squirrel_adapter, use_service):
        use_service(build_service({"squirrel": squirrel_adapter}, max_attachment_bytes=8))

        r = elog_client.post(
            "/elog/v1/apps/squirrel/attachments", files={"file": ("big.bin", b"123456789")}, headers=WRITE_HEADERS
        )

        assert r.status_code == 413
        assert r.json()["code"] == "elog_attachment_too_large"
        assert squirrel_adapter.calls == []

    def test_at_the_limit_is_accepted(self, elog_client, squirrel_adapter, use_service):
        use_service(build_service({"squirrel": squirrel_adapter}, max_attachment_bytes=8))

        r = elog_client.post(
            "/elog/v1/apps/squirrel/attachments", files={"file": ("ok.bin", b"12345678")}, headers=WRITE_HEADERS
        )

        assert r.status_code == 200

    def test_missing_file_part_is_422(self, elog_client):
        r = elog_client.post(
            "/elog/v1/apps/squirrel/attachments", files={"upload": ("a.txt", b"x")}, headers=WRITE_HEADERS
        )

        assert r.status_code == 422


def test_two_apps_post_with_their_own_adapters(elog_client, use_service):
    squirrel, trace = FakeElogAdapter("squirrel"), FakeElogAdapter("trace")
    use_service(build_service({"squirrel": squirrel, "trace": trace}))

    r = elog_client.post("/elog/v1/apps/trace/entries", json=ENTRY, headers=WRITE_HEADERS)

    assert r.json()["id"] == "trace-entry-1"
    assert squirrel.calls == []
    assert [op for op, *_ in trace.calls] == ["create_entry"]
