"""A throwaway MongoDB for the Mongo archiver adapter tests.

``mongo_server`` (session) starts one ``mongo:7`` container through the docker
CLI and removes it again at the end of the session; ``mongo_url`` (per test)
hands out ``mongodb://osprey@127.0.0.1:<port>/<fresh db>?authSource=admin`` with
the password in ``CANOPY_TEST_MONGO_PW``, the variable tests name through
``api_key_env``. Neither fixture is autouse, so only tests that ask for one need
Docker; those modules set ``pytestmark = pytest.mark.mongo`` so the default run
(``-m 'not live and not mongo'``) leaves them out.

**Leftover containers.** Every container carries ``canopy-test=mongo`` and
``canopy-test-owner=<pid of the pytest process>``. A session killed before its
finalizer runs leaves its container behind; the next session removes every
labelled container whose owner pid no longer exists, and never one whose owner
is alive, so concurrent sessions do not reap each other.

**Readiness.** ``mongo`` boots twice when it has a root user to create, and the
second boot can lose a port race and exit. The fixture therefore waits on an
authenticated ``ping`` (not a log line), and a container that disappears before
answering (``--rm``) is replaced, a bounded number of times.

Docker unavailable is a failure, not a skip: a ``mongo`` test that skipped would
hide that the adapter was never exercised.
"""

from __future__ import annotations

import os
import time
import uuid
import shutil
import secrets
import subprocess
from dataclasses import dataclass
from collections.abc import Iterator

import pytest

MONGO_IMAGE = "mongo:7"
MONGO_USER = "osprey"
MONGO_AUTH_DB = "admin"
PASSWORD_ENV = "CANOPY_TEST_MONGO_PW"
LABEL = "canopy-test=mongo"
OWNER_LABEL = "canopy-test-owner"

START_ATTEMPTS = 3
READY_TIMEOUT_S = 90.0
DOCKER_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class MongoServer:
    container_id: str
    host: str
    port: int
    password: str

    def url(self, database: str) -> str:
        return f"mongodb://{MONGO_USER}@{self.host}:{self.port}/{database}?authSource={MONGO_AUTH_DB}"

    def client(self, **kwargs):
        from pymongo import MongoClient

        kwargs.setdefault("tz_aware", True)
        kwargs.setdefault("serverSelectionTimeoutMS", 5000)
        return MongoClient(
            self.host,
            self.port,
            username=MONGO_USER,
            password=self.password,
            authSource=MONGO_AUTH_DB,
            **kwargs,
        )


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one docker CLI command, capturing text output."""
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=DOCKER_TIMEOUT_S,
        check=check,
    )


def docker_unavailable_reason() -> str | None:
    if shutil.which("docker") is None:
        return "the docker CLI is not on PATH"
    try:
        result = docker("info", "--format", "{{.ServerVersion}}", check=False)
    except subprocess.TimeoutExpired:
        return "`docker info` timed out; is the Docker daemon running?"
    if result.returncode != 0:
        return f"`docker info` failed: {result.stderr.strip() or result.stdout.strip()}"
    return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def reap_stale_mongo_containers() -> list[str]:
    """Remove labelled containers whose owner pid is dead; return their ids."""
    listing = docker(
        "ps",
        "-a",
        "--filter",
        f"label={LABEL}",
        "--format",
        '{{.ID}} {{.Label "' + OWNER_LABEL + '"}}',
    ).stdout
    reaped: list[str] = []
    for line in listing.splitlines():
        parts = line.split()
        if not parts:
            continue
        container_id = parts[0]
        owner = parts[1] if len(parts) > 1 else ""
        if owner.isdigit() and pid_alive(int(owner)):
            continue
        docker("rm", "-f", container_id, check=False)
        reaped.append(container_id)
    return reaped


def _container_running(container_id: str) -> bool:
    result = docker("inspect", "-f", "{{.State.Running}}", container_id, check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def _mapped_port(container_id: str) -> int:
    out = docker("port", container_id, "27017/tcp").stdout.strip().splitlines()
    if not out:
        raise RuntimeError(f"container {container_id} published no port for 27017/tcp")
    return int(out[0].rsplit(":", 1)[1])


def _ping(host: str, port: int, password: str) -> None:
    from pymongo import MongoClient

    client = MongoClient(
        host,
        port,
        username=MONGO_USER,
        password=password,
        authSource=MONGO_AUTH_DB,
        serverSelectionTimeoutMS=1000,
        connectTimeoutMS=1000,
    )
    try:
        client.admin.command("ping")
    finally:
        client.close()


class _ContainerExited(Exception):
    pass


def _wait_ready(container_id: str, host: str, port: int, password: str) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            _ping(host, port, password)
            return
        except Exception as exc:  # noqa: BLE001 - any failure means not ready yet
            last_error = exc
        if not _container_running(container_id):
            raise _ContainerExited(f"container {container_id} exited before answering")
        time.sleep(0.5)
    raise AssertionError(
        f"mongo container {container_id} stayed up but did not answer an authenticated "
        f"ping within {READY_TIMEOUT_S:.0f}s: {last_error}"
    )


def start_mongo_container(password: str) -> MongoServer:
    """Start a labelled ``mongo:7`` that has answered an authenticated ping."""
    exits: list[str] = []
    for _ in range(START_ATTEMPTS):
        container_id = docker(
            "run",
            "-d",
            "--rm",
            "--pull",
            "never",
            "--label",
            LABEL,
            "--label",
            f"{OWNER_LABEL}={os.getpid()}",
            "-e",
            f"MONGO_INITDB_ROOT_USERNAME={MONGO_USER}",
            "-e",
            f"MONGO_INITDB_ROOT_PASSWORD={password}",
            "-p",
            "127.0.0.1::27017",
            MONGO_IMAGE,
        ).stdout.strip()
        try:
            port = _mapped_port(container_id)
            _wait_ready(container_id, "127.0.0.1", port, password)
        except _ContainerExited as exc:
            exits.append(str(exc))
            docker("rm", "-f", container_id, check=False)
            continue
        except BaseException:
            docker("rm", "-f", container_id, check=False)
            raise
        return MongoServer(container_id, "127.0.0.1", port, password)
    raise AssertionError(f"mongo container exited before answering on all {START_ATTEMPTS} starts: " + "; ".join(exits))


@pytest.fixture(scope="session")
def mongo_server() -> Iterator[MongoServer]:
    """One throwaway authenticated MongoDB for the session; password in the env."""
    reason = docker_unavailable_reason()
    if reason is not None:
        pytest.fail(f"mongo tests need Docker and the local {MONGO_IMAGE} image: {reason}")
    reap_stale_mongo_containers()
    password = secrets.token_urlsafe(24)
    try:
        server = start_mongo_container(password)
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"`docker run {MONGO_IMAGE}` failed (is the image pulled? --pull never): {exc.stderr.strip()}")
    previous = os.environ.get(PASSWORD_ENV)
    os.environ[PASSWORD_ENV] = password
    try:
        yield server
    finally:
        if previous is None:
            os.environ.pop(PASSWORD_ENV, None)
        else:
            os.environ[PASSWORD_ENV] = previous
        docker("rm", "-f", server.container_id, check=False)


@pytest.fixture
def mongo_db_name() -> str:
    return f"canopy_test_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def mongo_url(mongo_server: MongoServer, mongo_db_name: str) -> Iterator[str]:
    """``mongodb://osprey@127.0.0.1:<port>/<fresh db>?authSource=admin``; db dropped after."""
    yield mongo_server.url(mongo_db_name)
    client = mongo_server.client()
    try:
        client.drop_database(mongo_db_name)
    finally:
        client.close()


@pytest.fixture
def mongo_client(mongo_server: MongoServer):
    """An authenticated, tz-aware pymongo client to the session server."""
    client = mongo_server.client()
    try:
        yield client
    finally:
        client.close()
