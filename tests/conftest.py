"""Shared test fixtures for the Canopy backend test suite."""

import os

# Seed an API-key registry before canopy.main imports cache settings.
os.environ["CANOPY_API_KEYS"] = "test-read:read,test-write:read+write"


READ_HEADERS = {"X-API-Key": "test-read"}
WRITE_HEADERS = {"X-API-Key": "test-write"}
