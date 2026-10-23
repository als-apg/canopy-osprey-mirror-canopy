"""Compose regression: every container's json-file logs stay bounded."""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_DOCKER_DIR = Path(__file__).resolve().parents[1] / "docker"
COMPOSE_FILES = sorted(_DOCKER_DIR.rglob("docker-compose*.yml"))
LOG_CAP = {"driver": "json-file", "options": {"max-size": "50m", "max-file": "3"}}


def _load(path: Path) -> dict:
    """Parse a YAML file, resolving its anchors.

    Parameters
    ----------
    path : Path
        YAML file to read.

    Returns
    -------
    dict
        The parsed document.
    """
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_every_compose_file_is_covered():
    names = {path.relative_to(_DOCKER_DIR).as_posix() for path in COMPOSE_FILES}
    assert {
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "observability/docker-compose.observability.yml",
    } <= names


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.relative_to(_DOCKER_DIR).as_posix())
def test_every_service_caps_its_logs(path: Path):
    services = _load(path)["services"]
    assert services
    for name, service in services.items():
        assert service.get("logging") == LOG_CAP, f"{name} in {path.name} lacks the json-file log cap"


def test_log_level_reaches_the_dev_app_services():
    services = _load(_DOCKER_DIR / "docker-compose.yml")["services"]
    for name in ("api", "worker"):
        assert "CANOPY_LOG_LEVEL" in services[name]["environment"]
