"""Shared test fixtures for the Canopy backend test suite."""

import os
import asyncio
from unittest.mock import patch

# Prevent pydantic-settings from reading the developer's local .env file.
# Tests must be deterministic regardless of .env contents. Env-var-based tests
# (monkeypatch.setenv) still work because EnvSettingsSource is a separate path.
patch("pydantic_settings.sources.providers.dotenv.dotenv_values", return_value={}).start()

# Seed an API-key registry before canopy.main imports cache settings.
os.environ["CANOPY_API_KEYS"] = "test-read:read@*,test-write:read+write@*,test-admin:admin@*"


READ_HEADERS = {"X-API-Key": "test-read"}
WRITE_HEADERS = {"X-API-Key": "test-write"}
ADMIN_HEADERS = {"X-API-Key": "test-admin"}


async def wait_for(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """Poll ``predicate`` until it is truthy or ``timeout`` elapses.

    Use instead of a fixed ``asyncio.sleep`` when waiting on background tasks,
    so slow CI runners get more time without slowing down fast ones.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False
