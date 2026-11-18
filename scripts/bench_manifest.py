"""Time the panel server's screen manifest over a large generated library.

The viewer revalidates ``GET /drafts/manifest.json`` every few seconds while a
Screens tab is visible, so what matters is the cost of a request for a library
that has not changed. This script builds a synthetic read-only library of N
screens in nested folders, then reports, per size:

* ``first``: the first ``DraftStore.manifest()`` of a new store (every screen read once);
* ``manifest``: the median of the following calls on the unchanged library;
* ``GET 304``: the median of ``GET /drafts/manifest.json`` with the ETag of the
  previous answer, through the real route.

Usage::

    uv run --extra mcp python scripts/bench_manifest.py 2500 6000 16000
    uv run --extra mcp python scripts/bench_manifest.py --bytes 20000 --calls 15 2500

The library is written under ``--dir`` (a fresh temporary directory by default)
and removed afterwards; ``--bytes`` screens of about 110 kB need about 1.8 GB
for 16000 screens.
"""

import json
import time
import shutil
import argparse
import tempfile
import statistics
from pathlib import Path

from starlette.testclient import TestClient
from starlette.applications import Starlette

from canopy_mcp.store import SCREEN_SUFFIX, open_stores
from canopy_mcp.config import Settings
from canopy_mcp.static import build_routes

PER_FOLDER = 40
FOLDERS_PER_AREA = 10


def screen_bytes(index: int, size: int) -> bytes:
    """One screen document of about ``size`` bytes whose title names ``index``."""
    widget = {"type": "label", "x": 0, "y": 0, "width": 120, "height": 24, "text": "generated", "channel": "SIM:CH"}
    doc = {
        "schemaVersion": "1.0",
        "kind": "screen",
        "metadata": {"title": f"Generated screen {index}", "description": "A screen of the generated library."},
        "widgets": [],
    }
    one = len(json.dumps(widget, indent=2)) + 8
    doc["widgets"] = [widget] * max(1, size // one)
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def build_library(root: Path, count: int, size: int) -> None:
    """Write ``count`` screens under ``root/screens``, ``PER_FOLDER`` to a folder, two folder levels deep."""
    for index in range(count):
        folder = index // PER_FOLDER
        target = root / "screens" / f"area{folder // FOLDERS_PER_AREA:03d}" / f"group{folder:04d}"
        if index % PER_FOLDER == 0:
            target.mkdir(parents=True, exist_ok=True)
        (target / f"screen{index:06d}{SCREEN_SUFFIX}").write_bytes(screen_bytes(index, size))


def measure(base: Path, count: int, size: int, calls: int) -> dict[str, float]:
    library_dir = base / f"library-{count}"
    drafts_dir = base / f"drafts-{count}"
    drafts_dir.mkdir()
    build_library(library_dir, count, size)
    try:
        store, library = open_stores(drafts_dir, library_dir)
        assert library is not None
        start = time.perf_counter()
        listed = len(library.manifest()["screens"])
        first = time.perf_counter() - start
        assert listed == count, (listed, count)
        repeat = []
        for _ in range(calls):
            start = time.perf_counter()
            library.manifest()
            repeat.append(time.perf_counter() - start)

        settings = Settings(drafts_dir=drafts_dir, library_dir=library_dir)
        app = Starlette(routes=build_routes(settings, *open_stores(drafts_dir, library_dir)))
        revalidate = []
        with TestClient(app) as client:
            tag = client.get("/drafts/manifest.json").headers["etag"]
            for _ in range(calls):
                start = time.perf_counter()
                response = client.get("/drafts/manifest.json", headers={"if-none-match": tag})
                revalidate.append(time.perf_counter() - start)
                assert response.status_code == 304, response.status_code
        return {
            "first": first,
            "manifest": statistics.median(repeat),
            "manifest_min": min(repeat),
            "get304": statistics.median(revalidate),
            "get304_min": min(revalidate),
        }
    finally:
        shutil.rmtree(library_dir, ignore_errors=True)
        shutil.rmtree(drafts_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("screens", nargs="+", type=int, help="library sizes to measure")
    parser.add_argument("--bytes", type=int, default=110_000, help="approximate size of one screen file")
    parser.add_argument("--calls", type=int, default=9, help="repeated calls the medians are taken over")
    parser.add_argument("--dir", type=Path, default=None, help="where to build the libraries (default: a temp dir)")
    args = parser.parse_args()

    base = Path(tempfile.mkdtemp(prefix="bench-manifest-", dir=args.dir))
    try:
        print(f"screens of about {args.bytes} bytes; medians over {args.calls} calls (min in brackets)")
        print(f"{'screens':>8}  {'first':>9}  {'manifest':>20}  {'GET 304':>20}")
        for count in args.screens:
            r = measure(base, count, args.bytes, args.calls)
            print(
                f"{count:>8}  {r['first']:>8.3f}s"
                f"  {r['manifest'] * 1e3:>8.1f} ms [{r['manifest_min'] * 1e3:>6.1f}]"
                f"  {r['get304'] * 1e3:>8.1f} ms [{r['get304_min'] * 1e3:>6.1f}]",
                flush=True,
            )
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
