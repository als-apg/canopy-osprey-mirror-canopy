"""Evaluation cost of a formula with a vector token, at 100, 500 and 1,000 inputs.

One evaluation reads every member from Vole's rings (a real ``BufferService``,
no connector) and reduces them. The bound is on the median of the whole
evaluation, on the hardware CANOPY is operated on. Run with ``-s`` to see the
numbers::

    pytest tests/canopy/formula_engine/test_vector_perf.py -m perf -s
"""

from __future__ import annotations

import time
import statistics

import orjson
import pytest

from canopy.core.clock import FakeClock
from canopy.vole.config import VoleConfig
from canopy.vole.adapter import VoleDataSource
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.contracts.formula import FormulaSpec
from canopy.vole.buffer.store import PVStore
from canopy.formula_engine.config import FormulaEngineConfig
from canopy.vole.services.buffer_service import BufferService
from canopy.formula_engine.services.parser import Parser
from canopy.vole.services.eviction_service import EvictionService
from canopy.vole.services.alignment_service import AlignmentService
from canopy.formula_engine.services.formula_service import FormulaService

pytestmark = pytest.mark.perf

NOW = 1_000_000_000_000
ROUNDS = 30
# Median of one evaluation, per number of inputs.
BOUND_MS = {100: 10.0, 500: 25.0, 1000: 50.0}
EXPRESSIONS = ["max(severity({*}))", "count_nonzero({*} == 2)", "sum(connected({*}))"]


async def _service(n: int) -> tuple[FormulaService, dict[str, str]]:
    config = VoleConfig(max_points=4, global_bytes_max=50_000_000)
    store = PVStore(config)
    buffer = BufferService(store, AlignmentService(), EvictionService(store, config), FakeClock(start_ns=NOW), config)
    bindings = {str(i): f"DEV:{i:04d}:STATE" for i in range(n)}
    for i, pv in enumerate(bindings.values()):
        await buffer.seed(pv, [], max_points=1)
        sample = Sample(ts_ns=NOW + i, value=float(i % 3), severity=AlarmSeverity.NO_ALARM, source="live")
        await buffer.append(pv, sample, recv_ns=NOW + i)
    source = VoleDataSource(buffer, is_live_fed=lambda name: True)
    return FormulaService(source, Parser(), FormulaEngineConfig()), bindings


@pytest.mark.parametrize("n", [100, 500, 1000])
async def test_evaluation_cost(n: int) -> None:
    service, bindings = await _service(n)
    for expression in EXPRESSIONS:
        spec = FormulaSpec(kind="scalar", expression=expression, bindings=bindings)
        await service.evaluate(spec, t0_ns=0, t1_ns=0)  # parse once, outside the timing
        times_ms = []
        for _ in range(ROUNDS):
            start = time.perf_counter()
            result = await service.evaluate(spec, t0_ns=0, t1_ns=0)
            times_ms.append((time.perf_counter() - start) * 1000.0)
        frame = orjson.dumps({"type": "result", "id": "x", "data": result.model_dump(by_alias=True)})
        median, worst = statistics.median(times_ms), max(times_ms)
        print(
            f"\n{n:5d} inputs  {expression:28s} median {median:6.2f} ms  max {worst:6.2f} ms  frame {len(frame):6d} bytes"
        )
        assert result.scalar.vector_inputs.count == n
        # The members are counted, not listed: the frame does not grow with them.
        assert len(frame) < 400
        assert median < BOUND_MS[n]
