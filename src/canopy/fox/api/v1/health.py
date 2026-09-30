from canopy.core.health import make_health_router
from canopy.fox.runtime import uptime_s

router = make_health_router("fox", uptime_s)
