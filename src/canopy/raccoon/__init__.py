"""Raccoon module entry point.

Importing this package registers all built-in adapter types so that config
strings like ``type: meme`` resolve at startup.
"""

from canopy.raccoon.adapters import meme as _meme  # noqa: F401  -- registers "meme" adapter type
