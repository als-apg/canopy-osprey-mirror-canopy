"""Minted keys authenticate real requests, and revoked ones stop.

Integration tests against Postgres (skipped when it is unreachable). These cover
the promises the provisioning runbook makes to an operator: a key minted by the CLI
works, a deactivated key stops working, two keys for one app overlap during a
rotation, and the plaintext token is never persisted.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from canopy.core.errors import ApiKeyNotFound
from canopy.core.keys.models import ApiKey
from canopy.core.keys.tokens import hash_token
from canopy.core.keys.service import ApiKeyService
from canopy.core.keys.registry import DbKeyRegistry
from canopy.core.keys.repositories import ApiKeyRepository
from tests.canopy.core.keys.conftest import db_backed_registry


async def mint(session, app_name="trace", *, read=True, write=False, admin=False):
    key = await ApiKeyService(session).create_key(app_name, read=read, write=write, admin=admin)
    await session.commit()
    return key


class TestMintAndResolve:
    async def test_minted_key_resolves_with_its_scopes_and_name(self, session, session_maker):
        created = await mint(session, "operator-tools", read=True, write=True)

        resolved = await DbKeyRegistry().get(created.token)

        assert resolved is not None
        assert (resolved.read, resolved.write, resolved.admin) == (True, True, False)
        assert resolved.name == "operator-tools"
        assert resolved.id == created.id
        assert resolved.token == "", "a resolved DB key must not carry the plaintext token"
        assert created.token not in repr(resolved)

    async def test_admin_only_key_grants_no_read_or_write(self, session, session_maker):
        created = await mint(session, "ops", read=False, write=False, admin=True)

        resolved = await DbKeyRegistry().get(created.token)

        assert resolved is not None
        assert (resolved.read, resolved.write, resolved.admin) == (False, False, True)

    async def test_unknown_token_resolves_to_none(self, session, session_maker):
        await mint(session)
        assert await DbKeyRegistry().get("cnpy_not-a-real-token") is None

    async def test_deactivated_key_stops_resolving(self, session, session_maker):
        created = await mint(session)
        assert await DbKeyRegistry().get(created.token) is not None

        await ApiKeyService(session).deactivate_key(created.id)
        await session.commit()

        assert await DbKeyRegistry().get(created.token) is None

    async def test_two_active_keys_for_one_app_both_work(self, session, session_maker):
        """The rotation window: the outgoing and incoming key are valid at once.

        Guards against reintroducing the reference design's unique-active-app_name
        index, which would force you to break the client before minting its new key.
        """
        outgoing = await mint(session, "trace")
        incoming = await mint(session, "trace")

        assert outgoing.token != incoming.token
        assert await DbKeyRegistry().get(outgoing.token) is not None
        assert await DbKeyRegistry().get(incoming.token) is not None


class TestLookupsByID:
    async def test_unknown_id_is_reported_cleanly(self, session, session_maker):
        with pytest.raises(ApiKeyNotFound):
            await ApiKeyService(session).deactivate_key("11111111-1111-1111-1111-111111111111")

    async def test_malformed_id_is_reported_cleanly(self, session, session_maker):
        """A mistyped id must not reach asyncpg as an invalid UUID comparison — that
        raises DataError, which is a 500 through a route and a traceback from the CLI."""
        with pytest.raises(ApiKeyNotFound):
            await ApiKeyService(session).deactivate_key("not-a-uuid")

        assert await ApiKeyService(session).get_by_id("not-a-uuid") is None

    async def test_deactivating_twice_is_not_an_error(self, session, session_maker):
        created = await mint(session)
        service = ApiKeyService(session)

        await service.deactivate_key(created.id)
        await session.commit()
        again = await service.deactivate_key(created.id)

        assert again.is_active is False


class TestTokenHandling:
    async def test_plaintext_token_is_never_stored(self, session, session_maker):
        created = await mint(session, "trace")

        row = (await session.execute(select(ApiKey).where(ApiKey.id == created.id))).scalar_one()

        assert row.token_hash == hash_token(created.token)
        assert created.token not in str(row.__dict__.values())
        assert created.token.startswith("cnpy_")

    async def test_listing_keys_never_exposes_tokens(self, session, session_maker):
        created = await mint(session, "trace")

        listed = await ApiKeyService(session).list_keys()

        assert [key.app_name for key in listed] == ["trace"]
        assert not any(created.token in str(key.model_dump()) for key in listed)


class TestLastUsed:
    async def test_last_used_is_stamped_on_lookup(self, session, session_maker):
        created = await mint(session, "trace")
        assert created.last_used_at is None

        await DbKeyRegistry().get(created.token)

        session.expire_all()
        row = (await session.execute(select(ApiKey).where(ApiKey.id == created.id))).scalar_one()
        assert row.last_used_at is not None, "rotation needs this to answer 'is the old key still in use?'"

    async def test_a_failed_stamp_does_not_fail_the_lookup(self, session, session_maker, monkeypatch):
        """Attribution is a nicety; a key that resolves must keep resolving even if
        the ``last_used_at`` write itself fails."""
        created = await mint(session, "trace")

        async def broken_touch(self, key_id: str) -> None:
            raise OperationalError("UPDATE core.api_key", {}, Exception("connection refused"))

        monkeypatch.setattr(ApiKeyRepository, "touch_last_used", broken_touch)

        resolved = await DbKeyRegistry().get(created.token)
        assert resolved is not None
        assert resolved.name == "trace"


class TestThroughAGatedRoute:
    """The end-to-end promise: mint a key, send the token, get served.

    Runs against a throwaway app carrying the real `require_read_access` /
    `require_write_access` guards, so the whole chain under test is the production
    one — registry, dependency, error handler — without pulling a module's runtime
    (brokers, EPICS connectors) into a database test.
    """

    async def test_minted_key_authenticates_and_scopes_are_enforced(self, session, session_maker, gated_client):
        created = await mint(session, "trace", read=True, write=False)

        with db_backed_registry():
            ok = gated_client.get("/read", headers={"X-API-Key": created.token})
            no_write = gated_client.get("/write", headers={"X-API-Key": created.token})
            unknown = gated_client.get("/read", headers={"X-API-Key": "cnpy_wrong"})
            missing = gated_client.get("/read")

        assert ok.status_code == 200, ok.text
        assert ok.json() == {"name": "trace"}, "the app name is what logging may attribute a request to"

        assert no_write.status_code == 403
        assert no_write.json()["code"] == "insufficient_write_access"

        assert unknown.status_code == 401
        assert unknown.json()["code"] == "invalid_api_key"

        assert missing.status_code == 401

    async def test_revoked_key_is_refused_once_the_cache_expires(self, session, session_maker, gated_client):
        created = await mint(session, "trace", read=True)

        # ttl_s=0 stands in for "the cached entry has expired", so the test pins the
        # revocation path rather than sleeping out a real TTL.
        with db_backed_registry(ttl_s=0):
            assert gated_client.get("/read", headers={"X-API-Key": created.token}).status_code == 200

            await ApiKeyService(session).deactivate_key(created.id)
            await session.commit()

            refused = gated_client.get("/read", headers={"X-API-Key": created.token})

        assert refused.status_code == 401
        assert refused.json()["code"] == "invalid_api_key"
