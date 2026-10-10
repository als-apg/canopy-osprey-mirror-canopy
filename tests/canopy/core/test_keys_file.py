"""The key registry read from a file (``CANOPY_API_KEYS_FILE``).

A set file is the only source of keys, and a file that cannot supply any stops
startup. Every line is ``<token>:<scope>[+<scope>]@<module>[+<module>]`` and
carries ``read``; nothing is defaulted. The startup cases run in a fresh
interpreter, where ``canopy.main`` has not been imported yet, the process-wide
registry is still unbound and ``sys.modules`` shows what a keys file loaded.
"""

import os
import sys
import asyncio
import subprocess

import pytest

from canopy.core.config import Settings
from canopy.core.security import (
    KeysFileError,
    FileKeyRegistry,
    load_keys_file,
    build_key_registry,
)

SECRET = "s3cret-token"
FIVE = "hare,vole,fox,mole,converter"
# The shape of a token minted by the database store (``canopy.core.keys.tokens``),
# spelled out so the subprocesses never import that package.
MINTED = "cnpy_" + "A" * 43


def _get(registry, token):
    return asyncio.run(registry.get(token))


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


def test_without_a_file_the_registry_comes_from_the_environment_value(monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlalchemy", None)  # lean install: env registry alone
    registry = build_key_registry(Settings(api_keys="env-key:read@*", api_keys_file=None))
    assert _get(registry, "env-key").read is True


def test_a_set_file_supplies_the_registry_and_the_environment_value_is_ignored(tmp_path):
    path = _keys_file(tmp_path, "file-read:read@*,file-write:read+write@hare")
    registry = build_key_registry(Settings(api_keys="env-key:read+write@*", api_keys_file=str(path)))

    assert isinstance(registry, FileKeyRegistry)
    assert _get(registry, "env-key") is None
    read, write = _get(registry, "file-read"), _get(registry, "file-write")
    assert (read.read, read.write, read.admin, read.modules) == (True, False, False, frozenset({"*"}))
    assert (write.read, write.write, write.admin, write.modules) == (True, True, False, frozenset({"hare"}))


def test_an_empty_file_setting_counts_as_unset(monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlalchemy", None)
    registry = build_key_registry(Settings(api_keys="env-key:read@*", api_keys_file=""))
    assert _get(registry, "env-key") is not None


def test_a_trailing_newline_in_the_file_is_not_part_of_a_key(tmp_path):
    registry = load_keys_file(_keys_file(tmp_path, "file-read:read@*\n"))
    assert isinstance(registry, FileKeyRegistry)
    assert len(registry) == 1
    assert _get(registry, "file-read").read is True


def test_entries_on_separate_lines_are_separate_keys(tmp_path):
    """A line break must never fold the next entry into the modules of the previous one."""
    registry = load_keys_file(_keys_file(tmp_path, "file-read:read@hare\nfile-write:read+write@hare\r\n"))
    read, write = _get(registry, "file-read"), _get(registry, "file-write")
    assert len(registry) == 2
    assert (read.read, read.write, read.modules) == (True, False, frozenset({"hare"}))
    assert (write.read, write.write, write.modules) == (True, True, frozenset({"hare"}))


# --- The grammar -------------------------------------------------------------------


def test_a_key_is_scoped_to_the_modules_it_names(tmp_path):
    key = _get(load_keys_file(_keys_file(tmp_path, "k:read@hare+mole+fox+converter")), "k")
    assert key.modules == frozenset({"hare", "mole", "fox", "converter"})
    assert key.allows_module("hare") and not key.allows_module("vole")


def test_the_wildcard_grants_every_module(tmp_path):
    key = _get(load_keys_file(_keys_file(tmp_path, "k:read@hare+*")), "k")
    assert key.modules == frozenset({"*"})
    assert key.allows_module("vole")


def test_the_core_scope_is_a_module(tmp_path):
    key = _get(load_keys_file(_keys_file(tmp_path, "k:read@core")), "k")
    assert key.modules == frozenset({"core"})


def test_admin_is_a_scope_alongside_read(tmp_path):
    key = _get(load_keys_file(_keys_file(tmp_path, "k:read+write+admin@core")), "k")
    assert (key.read, key.write, key.admin) == (True, True, True)


def test_scope_order_does_not_matter(tmp_path):
    key = _get(load_keys_file(_keys_file(tmp_path, "k:write+read@hare")), "k")
    assert (key.read, key.write, key.admin) == (True, True, False)


def test_a_file_key_is_named_by_position_and_never_shows_its_token(tmp_path):
    registry = load_keys_file(_keys_file(tmp_path, f"other:read@*\n{SECRET}:read@hare"))
    key = _get(registry, SECRET)
    assert key.name == "file:2"
    assert key.token == SECRET
    assert SECRET not in repr(key)


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
    path = _keys_file(tmp_path, "file-read:read@*")
    path.chmod(0o000)
    try:
        with pytest.raises(KeysFileError, match="data.keys"):
            load_keys_file(path)
    finally:
        path.chmod(0o600)


def test_a_file_that_is_not_text_is_refused(tmp_path):
    path = tmp_path / "data.keys"
    path.write_bytes(b"\xff\xfe\x00:read@*")
    with pytest.raises(KeysFileError):
        load_keys_file(path)


@pytest.mark.parametrize("content", ["", "\n", "  \n\t", ",,"])
def test_a_file_that_holds_no_key_is_refused(tmp_path, content):
    with pytest.raises(KeysFileError, match="data.keys: holds no API key"):
        load_keys_file(_keys_file(tmp_path, content))


# One case per rule; each is the second entry, after a good one, so the refusal
# must name position 2 and the whole file is refused, not just the line.
REFUSALS = {
    "no-separator": ("no-scope-separator", "is not <token>:"),
    "empty-token": (":read@*", "is not <token>:"),
    "missing-suffix": ("k:read", "has no @<module>"),
    "missing-suffix-two-scopes": ("k:read+write", "has no @<module>"),
    "unknown-scope": ("k:read+delete@*", "scope other than"),
    "empty-scope": ("k:@*", "scope other than"),
    "trailing-plus-scope": ("k:read+@*", "scope other than"),
    "no-read-write": ("k:write@*", "grants no read"),
    "no-read-admin": ("k:admin@core", "grants no read"),
    "unknown-module": ("k:read@hare+nosuchmodule", "unknown module"),
    "empty-module-set": ("k:read@", "empty module"),
    "trailing-plus-module": ("k:read@hare+", "empty module"),
    "second-at": ("k:read@hare@fox", "unknown module"),
    "space-in-token": ("k k:read@*", "whitespace"),
    "space-in-scopes": ("k:read write@*", "whitespace"),
    "space-after-colon": ("k: read@*", "whitespace"),
    "space-in-modules": ("k:read@hare fox", "whitespace"),
    "repeated-token": ("file-read:read+write@*", "repeats an earlier token"),
}


@pytest.mark.parametrize("entry, reason", REFUSALS.values(), ids=REFUSALS.keys())
def test_a_file_with_a_malformed_entry_is_refused_as_a_whole(tmp_path, entry, reason):
    with pytest.raises(KeysFileError, match="data.keys: entry 2 ") as excinfo:
        load_keys_file(_keys_file(tmp_path, f"file-read:read@*\n{entry}\n"))
    assert reason in str(excinfo.value)


@pytest.mark.parametrize("entry", [f"{SECRET}", f"{SECRET}:read", f"{SECRET}:write@*", f"{SECRET}:read@nosuch"])
def test_a_refusal_never_quotes_the_file_content(tmp_path, entry):
    with pytest.raises(KeysFileError) as excinfo:
        load_keys_file(_keys_file(tmp_path, entry))
    assert SECRET not in str(excinfo.value)
    assert "nosuch" not in str(excinfo.value)


def test_a_bad_file_is_not_replaced_by_the_environment_value(tmp_path):
    settings = Settings(api_keys="env-key:read+write@*", api_keys_file=str(tmp_path / "absent.keys"))
    with pytest.raises(KeysFileError):
        build_key_registry(settings)


def test_a_suffixless_file_is_not_replaced_by_the_environment_value(tmp_path):
    path = _keys_file(tmp_path, "file-read:read")
    settings = Settings(api_keys="env-key:read+write@*", api_keys_file=str(path))
    with pytest.raises(KeysFileError, match="entry 1 has no @<module>"):
        build_key_registry(settings)


# --- Startup -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "content", [None, "", "\n", "file-read:read\n"], ids=["missing", "empty", "blank", "suffixless"]
)
def test_startup_fails_when_the_set_file_cannot_supply_a_registry(tmp_path, content):
    path = tmp_path / "data.keys"
    if content is not None:
        path.write_text(content)

    proc = _run_python(
        "import canopy.main",
        tmp_path,
        CANOPY_MODULES=FIVE,
        CANOPY_API_KEYS="env-key:read+write@*",
        CANOPY_API_KEYS_FILE=str(path),
    )

    assert proc.returncode != 0
    assert "KeysFileError" in proc.stderr
    assert str(path) in proc.stderr


def test_a_keys_file_loads_neither_sqlalchemy_nor_the_database_chain(tmp_path):
    path = _keys_file(tmp_path, "file-read:read@hare+mole+fox+converter\nfile-write:read+write@hare\n")
    code = (
        "import sys\n"
        "import canopy.main\n"
        "from canopy.core.security import get_registry\n"
        "print('registry', type(get_registry()).__name__)\n"
        "for name in ('sqlalchemy', 'canopy.core.keys', 'canopy.core.keys.registry', 'canopy.core.keys.routes'):\n"
        "    print('loaded', name, name in sys.modules)\n"
        "paths = [getattr(route, 'path', '') for route in canopy.main.app.routes]\n"
        "print('api-keys mounted', any(p.startswith('/v1/api-keys') for p in paths))\n"
    )

    proc = _run_python(
        code,
        tmp_path,
        CANOPY_MODULES=FIVE,
        CANOPY_API_KEYS="env-key:read+write@*",
        CANOPY_API_KEYS_FILE=str(path),
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[-6:] == [
        "registry FileKeyRegistry",
        "loaded sqlalchemy False",
        "loaded canopy.core.keys False",
        "loaded canopy.core.keys.registry False",
        "loaded canopy.core.keys.routes False",
        "api-keys mounted False",
    ]


def test_env_keys_without_sqlalchemy_still_import_the_app(tmp_path):
    code = (
        "import sys\n"
        "sys.modules['sqlalchemy'] = None\n"
        "import canopy.main\n"
        "from fastapi.testclient import TestClient\n"
        "from canopy.core.security import get_registry\n"
        "print('registry', type(get_registry()).__name__)\n"
        "print('chain loaded', 'canopy.core.keys.registry' in sys.modules)\n"
        "with TestClient(canopy.main.app) as client:\n"
        "    resp = client.get('/hare/v1/health/summary', headers={'X-API-Key': 'env-key'})\n"
        "    print('env-key', resp.status_code)\n"
    )

    proc = _run_python(
        code,
        tmp_path,
        CANOPY_MODULES=FIVE,
        CANOPY_API_KEYS="env-key:read+write@*",
        CANOPY_API_KEYS_FILE="",
    )

    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "registry EnvKeyRegistry" in lines
    assert "chain loaded False" in lines
    assert "env-key 200" in lines


def test_the_served_app_accepts_the_file_keys_and_nothing_else(tmp_path):
    path = _keys_file(tmp_path, "file-read:read@hare\n")
    code = (
        "import sys\n"
        "from fastapi.testclient import TestClient\n"
        "import canopy.main\n"
        "with TestClient(canopy.main.app) as client:\n"
        f"    for label, token in (('file-read', 'file-read'), ('env-key', 'env-key'), ('minted', {MINTED!r})):\n"
        "        resp = client.get('/hare/v1/health/summary', headers={'X-API-Key': token})\n"
        "        print(label, resp.status_code)\n"
        "    resp = client.get('/vole/v1/pvs/', headers={'X-API-Key': 'file-read'})\n"
        "    print('file-read-vole', resp.status_code)\n"
        "print('chain loaded', 'canopy.core.keys.registry' in sys.modules)\n"
    )

    proc = _run_python(
        code,
        tmp_path,
        CANOPY_MODULES=FIVE,
        CANOPY_API_KEYS="env-key:read+write@*",
        CANOPY_API_KEYS_FILE=str(path),
    )

    assert proc.returncode == 0, proc.stderr
    answers = [
        line
        for line in proc.stdout.splitlines()
        if line.split(" ")[0] in {"file-read", "file-read-vole", "env-key", "minted", "chain"}
    ]
    assert answers == ["file-read 200", "env-key 401", "minted 401", "file-read-vole 403", "chain loaded False"]
