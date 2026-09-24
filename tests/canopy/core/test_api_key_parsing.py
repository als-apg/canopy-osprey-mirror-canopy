"""EnvKeyRegistry parsing: a typo'd entry must not silently take keys down.

The startup summary line these tests assert on is a documented operator check
(``docs/deployment/api-keys.md`` tells you to grep the container log for it), so
its presence is part of the deployment contract, not an implementation detail.
"""

import logging

import canopy.core.security as security
from canopy.core.config import get_settings
from canopy.core.security import EnvKeyRegistry, get_registry, bind_key_registry


class TestEnvKeyRegistryParsing:
    async def test_scopes_resolve(self):
        registry = EnvKeyRegistry("viewer:read,operator:read+write,ops:admin")

        viewer = await registry.get("viewer")
        assert viewer is not None
        assert (viewer.read, viewer.write, viewer.admin) == (True, False, False)

        operator = await registry.get("operator")
        assert operator is not None
        assert (operator.read, operator.write, operator.admin) == (True, True, False)

        ops = await registry.get("ops")
        assert ops is not None
        assert (ops.read, ops.write, ops.admin) == (False, False, True)

    async def test_env_keys_are_named_by_position_for_logging(self):
        registry = EnvKeyRegistry("viewer:read,operator:read+write")

        assert (await registry.get("viewer")).name == "env:1"
        assert (await registry.get("operator")).name == "env:2"

    async def test_unknown_token_and_empty_registry(self):
        assert await EnvKeyRegistry("viewer:read").get("nope") is None
        assert await EnvKeyRegistry("").get("viewer") is None

    async def test_bad_entries_do_not_take_down_the_good_ones(self, caplog):
        # A missing colon, an empty token, and a misspelled scope — the two
        # well-formed keys around them still authenticate.
        with caplog.at_level(logging.WARNING, logger="canopy.core.security"):
            registry = EnvKeyRegistry("viewer:read,malformed,:read,typo:read+wrote,operator:read+write")

        assert await registry.get("viewer") is not None
        assert await registry.get("operator") is not None
        assert await registry.get("malformed") is None

        # A misspelled scope is skipped entirely, not registered with a subset of scopes.
        assert await registry.get("typo") is None

        warnings = caplog.text
        assert "entry #2" in warnings  # malformed
        assert "entry #3" in warnings  # empty token
        assert "entry #4" in warnings  # unrecognized scope

    def test_warnings_never_contain_the_token(self, caplog):
        with caplog.at_level(logging.WARNING, logger="canopy.core.security"):
            EnvKeyRegistry("s3cret-no-colon,another-s3cret:read+wrote")

        assert "s3cret" not in caplog.text

    async def test_separator_typo_does_not_escalate_scope_or_leak_token(self, caplog):
        # A space instead of a comma folds two entries into one malformed scope
        # string. The bad entry must be dropped outright, not registered with
        # whatever scope substring happened to parse out of it.
        with caplog.at_level(logging.WARNING, logger="canopy.core.security"):
            registry = EnvKeyRegistry("ro-s3cret:read rw-s3cret:read+write")

        assert await registry.get("ro-s3cret") is None
        assert "s3cret" not in caplog.text

    async def test_duplicate_token_warns_and_last_entry_wins(self, caplog):
        with caplog.at_level(logging.WARNING, logger="canopy.core.security"):
            registry = EnvKeyRegistry("tok:read+write,other:read,tok:read")

        tok = await registry.get("tok")
        assert tok is not None
        assert (tok.read, tok.write) == (True, False)

        assert "entry #3" in caplog.text

    def test_position_numbers_match_raw_comma_fields(self, caplog):
        # A doubled comma produces an empty field between "a:read" and
        # "b:read+wrote" — the empty field must still consume a position
        # number, so the misspelled scope is reported as entry #3, not #2.
        with caplog.at_level(logging.WARNING, logger="canopy.core.security"):
            EnvKeyRegistry("a:read,,b:read+wrote")

        assert "entry #3" in caplog.text
        assert "entry #2" not in caplog.text

    def test_startup_summary_is_logged(self, caplog):
        with caplog.at_level(logging.INFO, logger="canopy.core.security"):
            EnvKeyRegistry("viewer:read,operator:read+write,ops:admin")

        assert "Registered 3 API key(s) from CANOPY_API_KEYS (2 read, 1 write, 1 admin)" in caplog.text

    def test_empty_env_registry_is_not_an_error(self, caplog):
        # No longer a warning: with the database registry behind it, an empty env
        # layer is the normal deployed state, not a misconfiguration.
        with caplog.at_level(logging.INFO, logger="canopy.core.security"):
            EnvKeyRegistry("")

        assert "relying on the database registry" in caplog.text
        assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


class TestKeyRegistryBinding:
    def teardown_method(self):
        security._registry = None

    async def test_get_registry_does_not_clobber_a_bound_registry(self):
        fake = EnvKeyRegistry("fake:read")
        bind_key_registry(fake)

        assert get_registry() is fake

    async def test_get_registry_builds_default_when_unbound(self, monkeypatch):
        monkeypatch.setenv("CANOPY_API_KEYS", "viewer:read")
        get_settings.cache_clear()
        security._registry = None

        registry = get_registry()

        assert await registry.get("viewer") is not None

        get_settings.cache_clear()
