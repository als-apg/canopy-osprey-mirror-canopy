"""Unit tests for the batch-conversion Arq task (no HTTP, no real worker)."""

import io
import json
import tarfile
import zipfile
from pathlib import Path

import fakeredis.aioredis

from canopy.converter.tasks import input_key, convert_batch

FIXTURES = Path(__file__).parent / "fixtures"
GOOD_EDL = (FIXTURES / "min.edl").read_bytes()


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _targz(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _run(archive: bytes, token: str = "tok", job_id: str = "job1"):
    redis = fakeredis.aioredis.FakeRedis()
    await redis.set(input_key(token), archive)
    manifest = await convert_batch({"redis": redis, "job_id": job_id}, token)
    return manifest, redis


async def test_batch_converts_each_entry_and_classifies_outcomes():
    archive = _zip(
        {
            "screens/a.edl": GOOD_EDL,
            "screens/b.edl": GOOD_EDL,
            "screens/broken.ui": b"<not-a-ui/>",  # known kind, unparseable -> failed
            "notes.txt": b"ignore me",  # not a screen -> skipped
        }
    )
    manifest, redis = await _run(archive)

    assert manifest["total"] == 4
    assert manifest["succeeded"] == 2
    assert manifest["failed"] == 1
    assert manifest["skipped"] == 1

    # Each succeeded row points at a result key holding the screen IR.
    succeeded = [r for r in manifest["rows"] if r["status"] == "succeeded"]
    for row in succeeded:
        raw = await redis.get(row["resultKey"])
        assert raw is not None
        assert json.loads(raw)["schemaVersion"] == "1.0"

    skipped = [r for r in manifest["rows"] if r["status"] == "skipped"]
    assert skipped[0]["path"] == "notes.txt"


async def test_batch_names_each_screen_after_its_entry():
    manifest, redis = await _run(_zip({"screens/a.edl": GOOD_EDL, "screens/b.edl": GOOD_EDL}))

    ids = {r["path"]: json.loads(await redis.get(r["resultKey"]))["id"] for r in manifest["rows"]}
    assert ids == {"screens/a.edl": "a", "screens/b.edl": "b"}


async def test_batch_deletes_staged_input_when_done():
    manifest, redis = await _run(_zip({"a.edl": GOOD_EDL}), token="cleanup")
    assert manifest["succeeded"] == 1
    assert await redis.get(input_key("cleanup")) is None


async def test_batch_supports_tar_gz():
    manifest, _ = await _run(_targz({"a.edl": GOOD_EDL, "b.ui": b"<bad/>"}))
    assert manifest["total"] == 2
    assert manifest["succeeded"] == 1  # a.edl converts; b.ui fails to parse
    assert manifest["failed"] == 1


async def test_batch_missing_input_returns_empty_manifest():
    redis = fakeredis.aioredis.FakeRedis()  # nothing staged
    manifest = await convert_batch({"redis": redis, "job_id": "j"}, "missing-token")
    assert manifest == {"total": 0, "succeeded": 0, "failed": 0, "skipped": 0, "rows": []}


async def test_batch_skips_zip_slip_entries():
    archive = _zip({"../evil.edl": GOOD_EDL, "ok.edl": GOOD_EDL})
    manifest, _ = await _run(archive, token="slip")
    # The traversal entry is dropped before conversion; only ok.edl is seen.
    assert manifest["total"] == 1
    assert manifest["rows"][0]["path"] == "ok.edl"


async def test_batch_reports_progress(monkeypatch):
    seen = []

    async def fake_report(redis, job_id, percent, message):
        seen.append((percent, message))

    # tasks._report imports jobs.gateway lazily; patch the function it calls.
    from canopy.jobs import gateway as jobs_gateway

    monkeypatch.setattr(jobs_gateway, "report_progress", fake_report)
    await _run(_zip({"a.edl": GOOD_EDL}))
    assert seen and seen[-1][0] == 100


def test_worker_service_uses_the_configured_edm_lists(monkeypatch, tmp_path):
    """The worker's service carries the configured ``colors.list`` and ``calc.list`` and is memoized.

    ``canopy-worker`` does not run ``converter_lifespan``, so ``_get_service``
    resolves the lists itself on its first call.
    """
    from canopy.converter import tasks
    from canopy.converter.config import ConverterConfig

    calc = tmp_path / "site-calcs.list"
    calc.write_text("CALC1\n\nsum\nA+B\n", encoding="utf-8")
    config = ConverterConfig(calc_list_path=calc)
    monkeypatch.setattr(tasks, "_service", None)
    monkeypatch.setattr(tasks, "get_converter_config", lambda: config)

    first = tasks._get_service()
    second = tasks._get_service()

    assert first._colors_list_path == config.colors_list_path
    assert first._calc_list_path == calc
    assert first is second
