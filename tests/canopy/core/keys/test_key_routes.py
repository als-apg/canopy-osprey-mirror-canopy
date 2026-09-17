"""The ``/v1/api-keys`` HTTP surface: mint, list, count, deactivate.

Integration tests against Postgres (skipped when it is unreachable). Every route
is gated on the ``admin`` scope — never ``write`` — so these also pin that a
write-only key cannot reach any of them.
"""

import pytest

from canopy.core.keys.service import ApiKeyService
from tests.canopy.core.keys.conftest import db_backed_registry
from tests.canopy.core.keys.test_key_store import mint

ADMIN_HEADERS = {"X-API-Key": "admin-env-key"}
WRITE_HEADERS = {"X-API-Key": "write-env-key"}


class TestCreateKey:
    async def test_admin_can_mint_a_key(self, session, session_maker, keys_client):
        with db_backed_registry(env="admin-env-key:admin@*"):
            resp = keys_client.post("/v1/api-keys", json={"appName": "trace", "read": True}, headers=ADMIN_HEADERS)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["appName"] == "trace"
        assert body["token"].startswith("cnpy_")
        assert body["readAccess"] is True
        assert body["writeAccess"] is False

    async def test_write_key_cannot_mint(self, session, session_maker, keys_client):
        with db_backed_registry(env="admin-env-key:admin@*,write-env-key:read+write@*"):
            resp = keys_client.post("/v1/api-keys", json={"appName": "trace", "read": True}, headers=WRITE_HEADERS)

        assert resp.status_code == 403
        assert resp.json()["code"] == "insufficient_admin_access"

    async def test_no_scope_is_rejected(self, session, session_maker, keys_client):
        with db_backed_registry(env="admin-env-key:admin@*"):
            resp = keys_client.post("/v1/api-keys", json={"appName": "trace"}, headers=ADMIN_HEADERS)

        assert resp.status_code == 400
        assert resp.json()["code"] == "bad_request"

    async def test_empty_app_name_is_rejected(self, session, session_maker, keys_client):
        with db_backed_registry(env="admin-env-key:admin@*"):
            resp = keys_client.post("/v1/api-keys", json={"appName": "  ", "read": True}, headers=ADMIN_HEADERS)

        assert resp.status_code == 400
        assert resp.json()["code"] == "bad_request"

    async def test_overlong_app_name_is_a_422_not_a_500(self, session, session_maker, keys_client):
        # The column is String(255); without a schema-level cap this fails at flush
        # with StringDataRightTruncation instead of a clean validation error.
        with db_backed_registry(env="admin-env-key:admin@*"):
            resp = keys_client.post("/v1/api-keys", json={"appName": "x" * 256, "read": True}, headers=ADMIN_HEADERS)

        assert resp.status_code == 422

    async def test_omitted_modules_defaults_to_every_module(self, session, session_maker, keys_client):
        with db_backed_registry(env="admin-env-key:admin@*"):
            resp = keys_client.post("/v1/api-keys", json={"appName": "trace", "read": True}, headers=ADMIN_HEADERS)

        assert resp.status_code == 200, resp.text
        assert resp.json()["modules"] == ["*"]

    async def test_modules_are_scoped_and_returned(self, session, session_maker, keys_client):
        with db_backed_registry(env="admin-env-key:admin@*"):
            resp = keys_client.post(
                "/v1/api-keys",
                json={"appName": "trace", "read": True, "modules": ["fox", "vole"]},
                headers=ADMIN_HEADERS,
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["modules"] == ["fox", "vole"]

    async def test_unknown_module_is_rejected(self, session, session_maker, keys_client):
        with db_backed_registry(env="admin-env-key:admin@*"):
            resp = keys_client.post(
                "/v1/api-keys",
                json={"appName": "trace", "read": True, "modules": ["not-a-module"]},
                headers=ADMIN_HEADERS,
            )

        assert resp.status_code == 400
        assert resp.json()["code"] == "bad_request"

    async def test_empty_module_list_is_rejected(self, session, session_maker, keys_client):
        with db_backed_registry(env="admin-env-key:admin@*"):
            resp = keys_client.post(
                "/v1/api-keys",
                json={"appName": "trace", "read": True, "modules": []},
                headers=ADMIN_HEADERS,
            )

        assert resp.status_code == 400
        assert resp.json()["code"] == "bad_request"

    async def test_admin_key_scoped_to_another_module_cannot_mint(self, session, session_maker, keys_client):
        # /v1/api-keys lives on the parent app (module "core"); an admin key
        # scoped to squirrel is refused before its admin scope is even checked.
        with db_backed_registry(env="squirrel-admin:admin@squirrel"):
            resp = keys_client.post(
                "/v1/api-keys",
                json={"appName": "trace", "read": True},
                headers={"X-API-Key": "squirrel-admin"},
            )

        assert resp.status_code == 403
        assert resp.json()["code"] == "insufficient_module_access"


class TestListAndCount:
    async def test_list_never_exposes_a_token(self, session, session_maker, keys_client):
        created = await mint(session, "trace", read=True)

        with db_backed_registry(env="admin-env-key:admin@*"):
            resp = keys_client.get("/v1/api-keys", headers=ADMIN_HEADERS)

        assert resp.status_code == 200
        app_names = [key["appName"] for key in resp.json()]
        assert "trace" in app_names
        assert created.token not in resp.text

    async def test_count_reflects_active_only(self, session, session_maker, keys_client):
        active = await mint(session, "trace", read=True)
        await mint(session, "old-trace", read=True)
        await ApiKeyService(session).deactivate_key(active.id)
        await session.commit()

        with db_backed_registry(env="admin-env-key:admin@*"):
            all_count = keys_client.get("/v1/api-keys/count", headers=ADMIN_HEADERS)
            active_count = keys_client.get("/v1/api-keys/count?activeOnly=true", headers=ADMIN_HEADERS)

        assert all_count.json() == 2
        assert active_count.json() == 1

    async def test_list_active_only_excludes_deactivated_keys(self, session, session_maker, keys_client):
        deactivated = await mint(session, "old-trace", read=True)
        await mint(session, "trace", read=True)
        await ApiKeyService(session).deactivate_key(deactivated.id)
        await session.commit()

        with db_backed_registry(env="admin-env-key:admin@*"):
            resp = keys_client.get("/v1/api-keys?activeOnly=true", headers=ADMIN_HEADERS)

        assert resp.status_code == 200
        app_names = [key["appName"] for key in resp.json()]
        assert app_names == ["trace"]

    async def test_list_filters_by_app_name(self, session, session_maker, keys_client):
        await mint(session, "trace", read=True)
        outgoing = await mint(session, "other-app", read=True)
        incoming = await mint(session, "other-app", read=True)

        with db_backed_registry(env="admin-env-key:admin@*"):
            resp = keys_client.get("/v1/api-keys?appName=other-app", headers=ADMIN_HEADERS)

        assert resp.status_code == 200
        ids = {key["id"] for key in resp.json()}
        assert ids == {outgoing.id, incoming.id}

    async def test_list_filters_by_app_name_and_active_only(self, session, session_maker, keys_client):
        outgoing = await mint(session, "other-app", read=True)
        incoming = await mint(session, "other-app", read=True)
        await ApiKeyService(session).deactivate_key(outgoing.id)
        await session.commit()

        with db_backed_registry(env="admin-env-key:admin@*"):
            resp = keys_client.get("/v1/api-keys?appName=other-app&activeOnly=true", headers=ADMIN_HEADERS)

        assert resp.status_code == 200
        ids = {key["id"] for key in resp.json()}
        assert ids == {incoming.id}


class TestDeactivate:
    async def test_admin_can_deactivate_another_key(self, session, session_maker, keys_client):
        target = await mint(session, "trace", read=True)

        with db_backed_registry(env="admin-env-key:admin@*"):
            resp = keys_client.delete(f"/v1/api-keys/{target.id}", headers=ADMIN_HEADERS)

        assert resp.status_code == 200
        assert resp.json()["isActive"] is False

    async def test_unknown_id_is_a_clean_404(self, session, session_maker, keys_client):
        with db_backed_registry(env="admin-env-key:admin@*"):
            resp = keys_client.delete("/v1/api-keys/11111111-1111-1111-1111-111111111111", headers=ADMIN_HEADERS)

        assert resp.status_code == 404
        assert resp.json()["code"] == "api_key_not_found"

    async def test_self_deactivation_is_refused_without_force(self, session, session_maker, keys_client):
        created = await mint(session, "ops-tool", admin=True)

        with db_backed_registry(env="", ttl_s=0):
            self_headers = {"X-API-Key": created.token}
            refused = keys_client.delete(f"/v1/api-keys/{created.id}", headers=self_headers)
            forced = keys_client.delete(f"/v1/api-keys/{created.id}?force=true", headers=self_headers)

        assert refused.status_code == 400
        assert refused.json()["code"] == "api_key_self_deactivation"

        assert forced.status_code == 200
        assert forced.json()["isActive"] is False

    async def test_self_deactivation_is_refused_for_an_uppercase_id(self, session, session_maker, keys_client):
        """`_find` normalizes any UUID form Postgres accepts, so a request against
        the caller's own id in a different case must still trip the guard."""
        created = await mint(session, "ops-tool", admin=True)

        with db_backed_registry(env="", ttl_s=0):
            self_headers = {"X-API-Key": created.token}
            refused = keys_client.delete(f"/v1/api-keys/{created.id.upper()}", headers=self_headers)

        assert refused.status_code == 400
        assert refused.json()["code"] == "api_key_self_deactivation"

    @pytest.mark.parametrize("target_scope", ["read"])
    async def test_deactivating_a_different_key_is_unaffected_by_the_guard(
        self, session, session_maker, keys_client, target_scope
    ):
        actor = await mint(session, "ops-tool", admin=True)
        other = await mint(session, "trace", read=True)

        with db_backed_registry(env="", ttl_s=0):
            resp = keys_client.delete(f"/v1/api-keys/{other.id}", headers={"X-API-Key": actor.token})

        assert resp.status_code == 200
        assert resp.json()["isActive"] is False
