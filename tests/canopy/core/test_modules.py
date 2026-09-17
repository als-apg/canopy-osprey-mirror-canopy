"""Drift check: ``MODULES`` must track ``canopy.main``'s actual mounted sub-apps.

Hardcoded rather than derived at import time (see ``canopy.core.modules`` for why),
so this test is what keeps an eleventh mount from silently becoming un-scopable.
"""

from starlette.routing import Mount

from canopy.main import app
from canopy.core.modules import MODULES


def test_modules_matches_actual_mounts():
    mounted = {route.path.strip("/") for route in app.routes if isinstance(route, Mount)}

    assert mounted | {"core"} == MODULES
