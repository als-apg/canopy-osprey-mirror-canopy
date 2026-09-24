"""EnvKeyRegistry parsing: a typo'd entry must not silently take keys down.

The startup summary line these tests assert on is a documented operator check
(``docs/deployment/api-keys.md`` tells you to grep the container log for it), so
its presence is part of the deployment contract, not an implementation detail.
"""

import logging

from canopy.core.security import EnvKeyRegistry


class TestEnvKeyRegistryParsing:
    def test_scopes_resolve(self):
        registry = EnvKeyRegistry("viewer:read,operator:read+write")

        viewer = registry.get("viewer")
        assert viewer is not None
        assert (viewer.read, viewer.write) == (True, False)

        operator = registry.get("operator")
        assert operator is not None
        assert (operator.read, operator.write) == (True, True)

    def test_unknown_token_and_empty_registry(self):
        assert EnvKeyRegistry("viewer:read").get("nope") is None
        assert EnvKeyRegistry("").get("viewer") is None

    def test_bad_entries_do_not_take_down_the_good_ones(self, caplog):
        # A missing colon, an empty token, and a misspelled scope — the two
        # well-formed keys around them still authenticate.
        with caplog.at_level(logging.WARNING, logger="canopy.core.security"):
            registry = EnvKeyRegistry("viewer:read,malformed,:read,typo:read+wrote,operator:read+write")

        assert registry.get("viewer") is not None
        assert registry.get("operator") is not None
        assert registry.get("malformed") is None

        # A misspelled scope is skipped entirely, not registered with a subset of scopes.
        assert registry.get("typo") is None

        warnings = caplog.text
        assert "entry #2" in warnings  # malformed
        assert "entry #3" in warnings  # empty token
        assert "entry #4" in warnings  # unrecognized scope

    def test_warnings_never_contain_the_token(self, caplog):
        with caplog.at_level(logging.WARNING, logger="canopy.core.security"):
            EnvKeyRegistry("s3cret-no-colon,another-s3cret:read+wrote")

        assert "s3cret" not in caplog.text

    def test_separator_typo_does_not_escalate_scope_or_leak_token(self, caplog):
        # A space instead of a comma folds two entries into one malformed scope
        # string. The bad entry must be dropped outright, not registered with
        # whatever scope substring happened to parse out of it.
        with caplog.at_level(logging.WARNING, logger="canopy.core.security"):
            registry = EnvKeyRegistry("ro-s3cret:read rw-s3cret:read+write")

        assert registry.get("ro-s3cret") is None
        assert "s3cret" not in caplog.text

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
            EnvKeyRegistry("viewer:read,operator:read+write")

        assert "Registered 2 API key(s) from CANOPY_API_KEYS (2 read, 1 write)" in caplog.text

    def test_empty_registry_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="canopy.core.security"):
            EnvKeyRegistry("")

        assert "every gated endpoint will return 401" in caplog.text
