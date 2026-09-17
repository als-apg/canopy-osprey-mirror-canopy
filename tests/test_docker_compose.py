"""Compose regression: every container's json-file logs stay bounded."""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_DOCKER_DIR = Path(__file__).resolve().parents[1] / "docker"
COMPOSE_FILES = [_DOCKER_DIR / "docker-compose.yml", _DOCKER_DIR / "docker-compose.prod.yml"]


def _load(path: Path) -> dict:
    """Parse a compose file, resolving its YAML anchors.

    Parameters
    ----------
    path : Path
        Compose file to read.

    Returns
    -------
    dict
        The parsed document.
    """
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_every_service_caps_its_logs(path: Path):
    services = _load(path)["services"]
    assert services
    for name, service in services.items():
        options = service.get("logging", {}).get("options", {})
        assert options.get("max-size"), f"{name} in {path.name} has no logging max-size"
        assert options.get("max-file"), f"{name} in {path.name} has no logging max-file"


def test_log_level_reaches_the_dev_app_services():
    services = _load(_DOCKER_DIR / "docker-compose.yml")["services"]
    for name in ("api", "worker"):
        assert "CANOPY_LOG_LEVEL" in services[name]["environment"]
