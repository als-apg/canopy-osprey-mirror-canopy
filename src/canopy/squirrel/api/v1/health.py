from canopy.core.health import make_health_router
from canopy.squirrel.runtime import uptime_s

router = make_health_router("squirrel", uptime_s)
