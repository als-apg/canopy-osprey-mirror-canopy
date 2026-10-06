"""Persistent API key storage — the ``core`` Postgres schema.

Keys are core infrastructure rather than any one module's data, so they live in
a ``core`` schema and are reached through the ``KeyRegistry`` protocol in
``canopy.core.security``. Nothing outside this package touches the table.
"""
