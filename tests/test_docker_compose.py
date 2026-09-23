"""Compose and data-layer regression: every container's json-file logs stay bounded."""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_DOCKER_DIR = Path(__file__).resolve().parents[1] / "docker"
COMPOSE_FILES = sorted(_DOCKER_DIR.rglob("docker-compose*.yml"))
DATA_LAYER_PLAYBOOK = _DOCKER_DIR / "data-layer" / "init.yml"


def _load(path: Path) -> dict | list:
    """Parse a YAML file, resolving its anchors.

    Parameters
    ----------
    path : Path
        YAML file to read.

    Returns
    -------
    dict | list
        The parsed document.
    """
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _tasks(tasks: list) -> list[dict]:
    """Flatten a task list, descending into ``block`` / ``rescue`` / ``always`` sections.

    Parameters
    ----------
    tasks : list
        An Ansible task list.

    Returns
    -------
    list[dict]
        Every leaf task, in order.
    """
    flat = []
    for task in tasks:
        nested = [child for key in ("block", "rescue", "always") for child in task.get(key, [])]
        flat.extend(_tasks(nested) if nested else [task])
    return flat


def _docker_run_argvs(playbook: list) -> dict[str, list[str]]:
    """Collect the argv of every ``docker run`` command task in an Ansible playbook.

    Parameters
    ----------
    playbook : list
        Parsed playbook: a list of plays, each with a ``tasks`` list.

    Returns
    -------
    dict[str, list[str]]
        Task name to its argv, for tasks whose argv starts with ``docker run``.
    """
    runs = {}
    for play in playbook:
        for task in _tasks(play.get("tasks", [])):
            command = task.get("command")
            argv = command.get("argv", []) if isinstance(command, dict) else []
            if argv[:2] == ["docker", "run"]:
                runs[task["name"]] = [str(arg) for arg in argv]
    return runs


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
        options = service.get("logging", {}).get("options", {})
        assert options.get("max-size"), f"{name} in {path.name} has no logging max-size"
        assert options.get("max-file"), f"{name} in {path.name} has no logging max-file"


def test_log_level_reaches_the_dev_app_services():
    services = _load(_DOCKER_DIR / "docker-compose.yml")["services"]
    for name in ("api", "worker"):
        assert "CANOPY_LOG_LEVEL" in services[name]["environment"]


def test_data_layer_containers_cap_their_logs():
    runs = _docker_run_argvs(_load(DATA_LAYER_PLAYBOOK))
    assert set(runs) == {"Run postgres", "Run redis"}
    for name, argv in runs.items():
        log_opts = {argv[i + 1].split("=", 1)[0] for i, arg in enumerate(argv[:-1]) if arg == "--log-opt"}
        assert {"max-size", "max-file"} <= log_opts, f"{name} runs without a json-file log cap"
