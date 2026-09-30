"""Drift check: ``MODULES`` must track ``canopy.main``'s actual mounted sub-apps.

Hardcoded rather than derived at import time (see ``canopy.core.modules`` for why),
so this test is what keeps an eleventh mount from silently becoming un-scopable.
"""

from starlette.routing import Mount

from canopy.main import app
from canopy.core.modules import MODULES, module_for_app


def test_modules_matches_actual_mounts():
    mounted = {route.path.strip("/") for route in app.routes if isinstance(route, Mount)}

    assert mounted | {"core"} == MODULES


def test_every_mount_and_the_parent_app_are_tagged_with_their_module():
    # module_for_app reads app.state.canopy_module, not the URL — an untagged
    # mount would raise AttributeError from the security dependency on its
    # first request rather than failing here at import time.
    assert module_for_app(app) == "core"

    for route in app.routes:
        if isinstance(route, Mount):
            assert module_for_app(route.app) == route.path.strip("/")
