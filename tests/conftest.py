"""Shared test fixtures for the Canopy backend test suite."""

import os
from unittest.mock import patch

# Prevent pydantic-settings from reading the developer's local .env file.
# Tests must be deterministic regardless of .env contents. Env-var-based tests
# (monkeypatch.setenv) still work because EnvSettingsSource is a separate path.
patch("pydantic_settings.sources.providers.dotenv.dotenv_values", return_value={}).start()

# Seed an API-key registry before canopy.main imports cache settings.
os.environ["CANOPY_API_KEYS"] = "test-read:read,test-write:read+write"


READ_HEADERS = {"X-API-Key": "test-read"}
WRITE_HEADERS = {"X-API-Key": "test-write"}
