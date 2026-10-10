from canopy.core.health import make_health_router
from canopy.jobs.runtime import uptime_s

router = make_health_router("jobs", uptime_s)
