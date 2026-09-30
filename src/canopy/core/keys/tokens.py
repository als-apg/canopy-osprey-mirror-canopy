"""Token minting and hashing.

Shared by the service (which mints) and the registry (which verifies), so both
agree on the one hash function. SHA-256 is deliberate: the token carries 256
bits of entropy from ``secrets.token_urlsafe(32)``, so there is nothing to
brute-force, and a fast hash keeps the ``WHERE token_hash = ?`` lookup viable on
the auth hot path. A password KDF (bcrypt/argon2) would put a deliberate work
factor on every authenticated request and buy nothing here.
"""

import hashlib
import secrets

TOKEN_PREFIX = "cnpy_"

# `secrets.token_urlsafe(32)` always renders to 43 base64url characters (32 bytes
# -> ceil(32*8/6) chars, no padding), so a minted token's length is fixed.
MINTED_TOKEN_LEN = len(TOKEN_PREFIX) + 43


def mint_token() -> str:
    """Return a fresh plaintext token. Shown once, never stored."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Return the hex SHA-256 of a plaintext token (64 chars)."""
    return hashlib.sha256(token.encode()).hexdigest()
