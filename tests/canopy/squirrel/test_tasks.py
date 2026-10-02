"""Arq task plumbing: the progress sink forwards a restore's phases to the Jobs module."""

from canopy.squirrel import tasks


async def test_progress_sink_forwards_phases(monkeypatch):
    calls = []

    async def fake_report_progress(redis, job_id, percent, message=None, phases=None):
        calls.append((redis, job_id, percent, message, phases))

    monkeypatch.setattr(tasks, "report_progress", fake_report_progress)
    sink = tasks._progress_sink({"redis": "redis", "job_id": "j"})
    phases = [{"name": "Setpoints", "current": 1, "total": 4, "status": "running", "failures": 0}]

    await sink(1, 4, "writing", phases=phases)
    await sink(2, 4, "saving")
    await sink(0, 0, "nothing to do")

    assert calls == [
        ("redis", "j", 25, "writing", phases),
        ("redis", "j", 50, "saving", None),
        ("redis", "j", 0, "nothing to do", None),
    ]
