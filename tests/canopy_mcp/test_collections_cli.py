"""``python -m canopy_mcp.collections <dir> --check [--library <root>]``.

The image check and the contract lane run this under ``python -I`` on a facility
collection directory; the drive gate greps its summary line.
"""

import sys
import json
import stat
import subprocess
from pathlib import Path

import pytest

from canopy_mcp.collections_cli import EXIT_OK, EXIT_ERROR, EXIT_REFUSED, main

GOOD = {
    "name": "Storage ring",
    "members": [
        {"screen": "library/sr/overview"},
        {"tool": "trace", "query": "pv=SR:DCCT", "name": "Beam current"},
    ],
    "layout": {"split": "row", "items": [{"tabs": [0], "size": 2}, {"tabs": [1]}]},
    "default": True,
}
# The retired slot shape: --check refuses it (only a reader lists it, as refused).
OLD = {
    "name": "Old storage ring",
    "members": [
        {"screen": "library/sr/overview", "slot": "a"},
        {"tool": "trace", "query": "pv=SR:DCCT", "name": "Beam current", "slot": "b"},
    ],
    "layout": {"split": "row", "items": [{"slot": "a"}, {"slot": "b"}]},
}
PLAIN = {"name": "Tools only", "members": [{"tool": "waveform", "name": "Septum pulse"}]}


def write(directory: Path, name: str, doc) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return path


def make_library(root: Path) -> Path:
    for rel in ("screens/sr/overview.screen.json", "screens/logo.png"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    return root


def run(capsys, *argv: str) -> tuple[int, list[str], list[str]]:
    code = main([*argv])
    out, err = capsys.readouterr()
    return code, out.splitlines(), err.splitlines()


def test_a_valid_directory_prints_the_summary_line(tmp_path: Path, capsys) -> None:
    write(tmp_path / "c", "storage-ring.json", GOOD)
    write(tmp_path / "c", "tools.json", PLAIN)
    code, out, err = run(capsys, str(tmp_path / "c"), "--check")
    assert code == EXIT_OK == 0
    assert out == ["collections: 2 checked, 0 refused"]
    assert err == []


def test_an_empty_directory_checks_nothing(tmp_path: Path, capsys) -> None:
    (tmp_path / "c").mkdir()
    code, out, _ = run(capsys, str(tmp_path / "c"), "--check")
    assert code == EXIT_OK
    assert out == ["collections: 0 checked, 0 refused"]


def test_a_missing_directory_checks_nothing(tmp_path: Path, capsys) -> None:
    code, out, _ = run(capsys, str(tmp_path / "absent"), "--check")
    assert code == EXIT_OK
    assert out == ["collections: 0 checked, 0 refused"]


def test_one_line_per_refusal_and_a_non_zero_exit(tmp_path: Path, capsys) -> None:
    c = tmp_path / "c"
    write(c, "good.json", GOOD)
    write(c, "drafts.json", {"name": "x", "members": [{"screen": "drafts/scratch"}]})
    write(c, "broken.json", "{not json")
    write(c, "wrong-id.json", {**PLAIN, "id": "other"})
    code, out, err = run(capsys, str(c), "--check")
    assert code == EXIT_REFUSED != 0
    assert out == ["collections: 4 checked, 3 refused"]
    assert len(err) == 3
    assert all(line.startswith("canopy_mcp.collections: refused: ") for line in err)
    joined = "\n".join(err)
    assert "broken.json" in joined and "JSON" in joined
    assert "drafts.json" in joined and "library/* screens only" in joined
    assert "wrong-id.json" in joined and "must equal the file id" in joined
    assert "good.json" not in joined


def test_the_old_slot_shape_is_refused_by_name(tmp_path: Path, capsys) -> None:
    c = tmp_path / "c"
    write(c, "good.json", GOOD)
    write(c, "old.json", OLD)
    write(c, "tiles.json", {**PLAIN, "layout": {"tabs": [0]}})
    code, out, err = run(capsys, str(c), "--check")
    assert code == EXIT_REFUSED
    assert out == ["collections: 3 checked, 1 refused"]
    assert len(err) == 1
    assert err[0].startswith("canopy_mcp.collections: refused: old.json: layout.items[0]: old_slot_shape: ")


def test_a_broken_layout_names_its_reason(tmp_path: Path, capsys) -> None:
    c = tmp_path / "c"
    write(c, "missing.json", {**GOOD, "layout": {"tabs": [0]}})
    code, out, err = run(capsys, str(c), "--check")
    assert code == EXIT_REFUSED
    assert len(err) == 1 and "missing.json: layout: member_missing" in err[0]


def test_the_files_are_reported_in_name_order(tmp_path: Path, capsys) -> None:
    c = tmp_path / "c"
    for name in ("b.json", "a.json", "c.json"):
        write(c, name, "[]")
    _, _, err = run(capsys, str(c), "--check")
    assert [line.split(": ")[2] for line in err] == ["a.json", "b.json", "c.json"]


@pytest.mark.parametrize("name", [".state.json", "a b.json", "x.y.json", f"{'a' * 65}.json"])
def test_a_file_whose_stem_is_not_a_collection_id_is_refused(tmp_path: Path, capsys, name: str) -> None:
    write(tmp_path / "c", name, PLAIN)
    code, out, err = run(capsys, str(tmp_path / "c"), "--check")
    assert code == EXIT_REFUSED
    assert out == ["collections: 1 checked, 1 refused"]
    assert len(err) == 1 and "not a collection id" in err[0]


def test_a_file_that_is_not_utf8_is_refused(tmp_path: Path, capsys) -> None:
    c = tmp_path / "c"
    c.mkdir()
    (c / "latin.json").write_bytes(b'{"name": "\xff", "members": []}')
    code, _, err = run(capsys, str(c), "--check")
    assert code == EXIT_REFUSED
    assert len(err) == 1 and "latin.json" in err[0]


def test_a_json_directory_is_refused_and_other_files_are_ignored(tmp_path: Path, capsys) -> None:
    c = tmp_path / "c"
    write(c, "ok.json", PLAIN)
    (c / "folder.json").mkdir()
    write(c, "README.md", "not a collection")
    write(c / "nested", "deep.json", "{not json")
    code, out, err = run(capsys, str(c), "--check")
    assert code == EXIT_REFUSED
    assert out == ["collections: 2 checked, 1 refused"]
    assert len(err) == 1 and "folder.json" in err[0] and "not a regular file" in err[0]


def test_a_non_ascii_file_name_is_escaped(tmp_path: Path, capsys) -> None:
    write(tmp_path / "c", "café.json", PLAIN)
    _, _, err = run(capsys, str(tmp_path / "c"), "--check")
    assert len(err) == 1
    assert err[0].isascii() and "caf\\xe9.json" in err[0]


def test_with_a_library_screen_members_must_be_listed(tmp_path: Path, capsys) -> None:
    lib = make_library(tmp_path / "lib")
    c = tmp_path / "c"
    write(c, "good.json", GOOD)
    write(c, "missing.json", {"name": "x", "members": [{"screen": "library/sr/missing"}]})
    write(c, "image.json", {"name": "x", "members": [{"screen": "library/logo"}]})
    code, out, err = run(capsys, str(c), "--check", "--library", str(lib))
    assert code == EXIT_REFUSED
    assert out == ["collections: 3 checked, 2 refused"]
    assert len(err) == 2 and all("library lists" in line for line in err)
    assert {line.split(": ")[2] for line in err} == {"image.json", "missing.json"}


def test_with_a_library_a_valid_directory_passes(tmp_path: Path, capsys) -> None:
    lib = make_library(tmp_path / "lib")
    write(tmp_path / "c", "good.json", GOOD)
    code, out, err = run(capsys, str(tmp_path / "c"), "--check", "--library", str(lib))
    assert code == EXIT_OK
    assert out == ["collections: 1 checked, 0 refused"]
    assert err == []


def test_without_a_library_any_library_screen_passes(tmp_path: Path, capsys) -> None:
    write(tmp_path / "c", "anything.json", {"name": "x", "members": [{"screen": "library/not/there"}]})
    code, out, _ = run(capsys, str(tmp_path / "c"), "--check")
    assert code == EXIT_OK
    assert out == ["collections: 1 checked, 0 refused"]


def test_a_missing_library_lists_no_screens(tmp_path: Path, capsys) -> None:
    c = tmp_path / "c"
    write(c, "tools.json", PLAIN)
    write(c, "screen.json", {"name": "x", "members": [{"screen": "library/sr/overview"}]})
    code, out, err = run(capsys, str(c), "--check", "--library", str(tmp_path / "no-lib"))
    assert code == EXIT_REFUSED
    assert out == ["collections: 2 checked, 1 refused"]
    assert len(err) == 1 and "screen.json" in err[0]


def test_the_library_is_scanned_once(tmp_path: Path, capsys, monkeypatch) -> None:
    import canopy_mcp.collections_cli as cli

    calls = []
    real = cli.scan
    monkeypatch.setattr(cli, "scan", lambda root: calls.append(root) or real(root))
    lib = make_library(tmp_path / "lib")
    for name in ("a.json", "b.json", "c.json"):
        write(tmp_path / "c", name, GOOD)
    assert run(capsys, str(tmp_path / "c"), "--check", "--library", str(lib))[0] == EXIT_OK
    assert len(calls) == 1


def test_a_path_that_is_not_a_directory_is_an_error(tmp_path: Path, capsys) -> None:
    path = write(tmp_path, "file.json", PLAIN)
    code, out, err = run(capsys, str(path), "--check")
    assert code == EXIT_ERROR
    assert code not in (EXIT_OK, EXIT_REFUSED)
    assert out == []
    assert len(err) == 1 and "not a directory" in err[0]


def test_a_library_that_is_not_a_directory_is_an_error(tmp_path: Path, capsys) -> None:
    write(tmp_path / "c", "tools.json", PLAIN)
    lib = write(tmp_path, "lib", "x")
    code, out, err = run(capsys, str(tmp_path / "c"), "--check", "--library", str(lib))
    assert code == EXIT_ERROR
    assert out == []
    assert len(err) == 1 and "library" in err[0]


def test_check_is_required(tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main([str(tmp_path)])
    assert exc.value.code == 2
    assert "--check" in capsys.readouterr().err


def test_runs_as_a_module_under_isolated_python_on_a_read_only_tree(tmp_path: Path) -> None:
    lib = make_library(tmp_path / "lib")
    c = tmp_path / "c"
    write(c, "good.json", GOOD)
    write(c, "bad.json", {"name": "x", "members": [{"screen": "library/sr/missing"}]})
    paths = [c, *c.iterdir(), lib, *lib.rglob("*")]
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    modes = {p: p.stat().st_mode for p in paths}
    try:
        for p in paths:
            p.chmod(0o555 if p.is_dir() else 0o444)
        command = [sys.executable, "-I", "-m", "canopy_mcp.collections", str(c), "--check", "--library", str(lib)]
        refused = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=120)
        (c / "bad.json").chmod(0o644)
        c.chmod(0o755)
        (c / "bad.json").unlink()
        c.chmod(0o555)
        passed = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    finally:
        for p, mode in modes.items():
            if p.exists():
                p.chmod(stat.S_IMODE(mode))
    assert refused.returncode == EXIT_REFUSED, refused.stderr
    assert refused.stdout == "collections: 2 checked, 1 refused\n"
    assert "bad.json" in refused.stderr and "RuntimeWarning" not in refused.stderr
    assert passed.returncode == 0, passed.stderr
    assert passed.stdout == "collections: 1 checked, 0 refused\n"
    assert passed.stderr == ""
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert after == [entry for entry in before if entry != "c/bad.json"]
