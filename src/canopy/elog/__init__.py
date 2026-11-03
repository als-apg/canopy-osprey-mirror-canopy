"""E-log module entry point.

Importing this package registers the built-in providers so that
``CANOPY_ELOG__PROVIDER=elog_plus`` resolves at startup.
"""

from canopy.elog.adapters import elog_plus as _elog_plus  # noqa: F401  -- registers "elog_plus" provider
