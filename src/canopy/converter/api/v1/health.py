from canopy.core.health import make_health_router
from canopy.converter.runtime import uptime_s

router = make_health_router("converter", uptime_s)
