"""``check`` on the speed fixture finishes in under 2 s against a running host API.

Live: needs ``CANOPY_LIVE=1`` and a canopy API (``CANOPY_BASE_URL``, default
``http://127.0.0.1:8000``). The key is ``CANOPY_API_KEY`` or, failing that, the
key in the file ``CANOPY_READ_KEY_FILE`` names; the test is skipped without
either. The catalog is built the
way startup builds it -- ``ir-map.json`` from ``CANOPY_VIEWER_DIST``, or the
checked-in fixture copy when that is unset, with the staged prop schemas -- and
loaded before the clock starts, as it is in the server. Raccoon must answer: a
PV pass skipped for a timeout or a connection error would time a round trip that
never happened. An answer that is an error (a local stack without a PV
directory replies ``adapter_misconfigured``) still is a round trip, and counts.
"""

import os
import json
import time
from pathlib import Path

import httpx
import pytest

from canopy_mcp import formulas
from canopy_mcp.store import DraftStore
from canopy_mcp.client import make_client
from canopy_mcp.config import Settings
from canopy_mcp.catalog import load_ir_map, build_catalog, load_prop_schemas
from canopy_mcp.tools.check import run_check
from canopy_mcp.check.schema import SPEC_UNAVAILABLE

pytestmark = pytest.mark.live

HERE = Path(__file__).parent
SPEED_FIXTURE = HERE / "fixtures" / "speed-100-50-20.screen.json"
BUDGET_S = 2.0


def _api_key() -> str:
    """``CANOPY_API_KEY``, else the key in the file ``CANOPY_READ_KEY_FILE`` names; skip without either."""
    key = os.environ.get("CANOPY_API_KEY")
    if key:
        return key
    key_file = os.environ.get("CANOPY_READ_KEY_FILE")
    if not key_file:
        pytest.skip("live test: set CANOPY_API_KEY or CANOPY_READ_KEY_FILE")
    return Path(key_file).read_text().strip()


def _settings() -> Settings:
    settings = Settings()
    if not settings.api_key:
        settings = settings.model_copy(update={"api_key": _api_key()})
    return settings


async def test_check_speed_fixture_under_two_seconds(tmp_path):
    settings = _settings()
    doc = json.loads(SPEED_FIXTURE.read_text())
    ir_map = load_ir_map(settings.viewer_dist or HERE / "fixtures")
    catalog = build_catalog(ir_map, load_prop_schemas(settings.staged_spec_dir))
    async with make_client(settings) as client:
        formulas.bind()

        start = time.perf_counter()
        result = await run_check(doc, store=DraftStore(tmp_path), catalog=catalog, client=client, settings=settings)
        elapsed = time.perf_counter() - start

    unanswered = [s for s in result.skipped if s["reason"] == "timeout" or hasattr(httpx, s["reason"])]
    assert unanswered == [], f"the host API did not answer: {unanswered}"
    # Without a staged spec the schema pass is the one spec_unavailable warning.
    assert [f for f in result.schema if f.code != SPEC_UNAVAILABLE] == [] and result.semantic == []
    assert elapsed < BUDGET_S, f"check took {elapsed:.3f} s (budget {BUDGET_S} s)"
