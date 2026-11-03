from canopy.core.health import make_health_router
from canopy.formula_engine.runtime import uptime_s

router = make_health_router("formula-engine", uptime_s, display_name="Formula Engine")
