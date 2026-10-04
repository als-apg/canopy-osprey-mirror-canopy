"""The key registry read from a file (``CANOPY_API_KEYS_FILE``).

A set file is the only source of keys, and a file that cannot supply any stops
startup. The startup cases run in a fresh interpreter, where ``canopy.main`` has
not been imported yet and the process-wide registry is still unbound.
"""

import os
import sys
import subprocess

import pytest

from canopy.core.config import Settings
from canopy.core.security import (
    KeysFileError,
    EnvKeyRegistry,
    load_keys_file,
    build_key_registry,
)

SECRET = "s3cret-token"


def _run_python(code: str, tmp_path, **env) -> subprocess.CompletedProcess:
    """Run ``code`` in a fresh interpreter, where ``canopy.main`` has not been imported yet."""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=120,
    )


def _keys_file(tmp_path, content: str):
    path = tmp_path / "data.keys"
    path.write_text(content)
    path.chmod(0o600)
    return path


# --- The settings ------------------------------------------------------------------


def test_the_keys_file_setting_is_unset_by_default(monkeypatch):
    monkeypatch.delenv("CANOPY_API_KEYS_FILE", raising=False)
    assert Settings().api_keys_file is None


def test_the_keys_file_setting_is_read_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CANOPY_API_KEYS_FILE", str(tmp_path / "data.keys"))
    assert Settings().api_keys_file == str(tmp_path / "data.keys")


# --- Choosing the source -----------------------------------------------------------


def test_without_a_file_the_registry_comes_from_the_environment_value():
    registry = build_key_registry(Settings(api_keys="env-key:read", api_keys_file=None))
    assert registry.get("env-key").read is True


def test_a_set_file_supplies_the_registry_and_the_environment_value_is_ignored(tmp_path):
    path = _keys_file(tmp_path, "file-read:read,file-write:read+write")
    registry = build_key_registry(Settings(api_keys="env-key:read+write", api_keys_file=str(path)))

    assert registry.get("env-key") is None
    read, write = registry.get("file-read"), registry.get("file-write")
    assert (read.read, read.write) == (True, False)
    assert (write.read, write.write) == (True, True)


def test_an_empty_file_setting_counts_as_unset():
    registry = build_key_registry(Settings(api_keys="env-key:read", api_keys_file=""))
    assert registry.get("env-key") is not None


def test_a_trailing_newline_in_the_file_is_not_part_of_a_key(tmp_path):
    registry = load_keys_file(_keys_file(tmp_path, "file-read:read\n"))
    assert isinstance(registry, EnvKeyRegistry)
    assert len(registry) == 1
    assert registry.get("file-read").read is True


def test_entries_on_separate_lines_are_separate_keys(tmp_path):
    """A line break must never fold the next entry into the scopes of the previous one."""
    registry = load_keys_file(_keys_file(tmp_path, "file-read:read\nfile-write:read+write\r\n"))
    read, write = registry.get("file-read"), registry.get("file-write")
    assert len(registry) == 2
    assert (read.read, read.write) == (True, False)
    assert (write.read, write.write) == (True, True)


# --- Files that cannot supply a registry -------------------------------------------


def test_a_missing_file_is_refused_and_named(tmp_path):
    path = tmp_path / "absent.keys"
    with pytest.raises(KeysFileError, match="absent.keys"):
        load_keys_file(path)


def test_a_path_that_is_a_directory_is_refused(tmp_path):
    with pytest.raises(KeysFileError):
        load_keys_file(tmp_path)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a file mode the user cannot read")
def test_an_unreadable_file_is_refused(tmp_path):
    path = _keys_file(tmp_path, "file-read:read")
    path.chmod(0o000)
    try:
        with pytest.raises(KeysFileError, match="data.keys"):
            load_keys_file(path)
    finally:
        path.chmod(0o600)


def test_a_file_that_is_not_text_is_refused(tmp_path):
    path = tmp_path / "data.keys"
    path.write_bytes(b"\xff\xfe\x00:read")
    with pytest.raises(KeysFileError):
        load_keys_file(path)


@pytest.mark.parametrize("content", ["", "\n", "  \n\t", ",,", "no-scope-separator"])
def test_a_file_that_holds_no_key_is_refused(tmp_path, content):
    with pytest.raises(KeysFileError, match="data.keys"):
        load_keys_file(_keys_file(tmp_path, content))


@pytest.mark.parametrize(
    "content",
    [
        "file-read:read,no-scope-separator",
        "file-read:admin",
        "file-read:read+",
        "file-read:",
        ":read",
        "file read:read",
        "file-read:read write",
        "file-read: read",
    ],
)
def test_a_file_with_a_malformed_entry_is_refused_as_a_whole(tmp_path, content):
    with pytest.raises(KeysFileError, match="data.keys"):
        load_keys_file(_keys_file(tmp_path, content))


def test_a_refusal_never_quotes_the_file_content(tmp_path):
    with pytest.raises(KeysFileError) as excinfo:
        load_keys_file(_keys_file(tmp_path, SECRET))
    assert SECRET not in str(excinfo.value)


def test_a_bad_file_is_not_replaced_by_the_environment_value(tmp_path):
    settings = Settings(api_keys="env-key:read+write", api_keys_file=str(tmp_path / "absent.keys"))
    with pytest.raises(KeysFileError):
        build_key_registry(settings)


# --- Startup -----------------------------------------------------------------------


@pytest.mark.parametrize("content", [None, "", "\n"], ids=["missing", "empty", "blank"])
def test_startup_fails_when_the_set_file_cannot_supply_a_registry(tmp_path, content):
    path = tmp_path / "data.keys"
    if content is not None:
        path.write_text(content)

    proc = _run_python(
        "import canopy.main",
        tmp_path,
        CANOPY_MODULES="hare",
        CANOPY_API_KEYS="env-key:read+write",
        CANOPY_API_KEYS_FILE=str(path),
    )

    assert proc.returncode != 0
    assert "KeysFileError" in proc.stderr
    assert str(path) in proc.stderr


def test_the_served_app_accepts_the_file_keys_and_not_the_environment_ones(tmp_path):
    path = _keys_file(tmp_path, "file-read:read\n")
    code = (
        "from fastapi.testclient import TestClient\n"
        "import canopy.main\n"
        "with TestClient(canopy.main.app) as client:\n"
        "    for token in ('file-read', 'env-key'):\n"
        "        resp = client.get('/hare/v1/health/summary', headers={'X-API-Key': token})\n"
        "        print(token, resp.status_code)\n"
    )

    proc = _run_python(
        code,
        tmp_path,
        CANOPY_MODULES="hare",
        CANOPY_API_KEYS="env-key:read+write",
        CANOPY_API_KEYS_FILE=str(path),
    )

    assert proc.returncode == 0, proc.stderr
    answers = [line for line in proc.stdout.splitlines() if line.startswith(("file-read ", "env-key "))]
    assert answers == ["file-read 200", "env-key 401"]
