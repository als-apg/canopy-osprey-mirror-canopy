"""Tests for Squirrel's dependency aliases."""

from canopy.core import db
from canopy.squirrel import dependencies


def test_session_dep_is_the_shared_core_alias():
    """Squirrel takes its request session through ``canopy.core.db.SessionDep``.

    A module-local alias could drop the function scope. It would then commit after the
    response and, next to the core alias, give the request a second session.
    """
    assert dependencies.SessionDep is db.SessionDep
