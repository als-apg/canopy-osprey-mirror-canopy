"""The ``table`` formula kind: contract, validation, live-only, error frames.

The JSON under ``fixtures/table/`` is the hand-written contract. Other suites
(the canopy-client SDK and the correlation front end) copy these files and
assert the same sha256 constants, so a change here is a contract change: update
the constants and every copy together.
"""

import json
import asyncio
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from canopy.fox import gateway
from canopy.main import app as parent_app
from canopy.fox.app import app as fox_app
from canopy.fox.config import FoxConfig
from canopy.fox.errors import (
    TableLimit,
    TableLiveOnly,
    FormulaTypeError,
    FormulaEvalTimeout,
    FormulaInputMissing,
    TableUpdateUnsupported,
    FormulaCircularReference,
    is_retryable,
)
from canopy.core.errors import ErrorCode, ModuleUnavailable
from canopy.contracts.sample import Sample, AlarmSeverity
from canopy.fox.dependencies import get_formula_service, get_subscription_manager
from canopy.contracts.formula import TABLE_MAX_LOOKBACK_NS, FormulaSpec, FormulaResult
from tests.canopy.fox.conftest import FakeDataSource
from canopy.fox.schemas.formula import FormulaResultDTO
from canopy.fox.services.parser import Parser
from canopy.fox.services.evaluator import bare_binding
from canopy.vole.services.notifier import ChangeNotifier
from tests.canopy.fox.table_harness import Harness
from canopy.fox.services.formula_service import FormulaService
from canopy.fox.services.subscription_manager import (
    SubscriptionManager,
    _Listener,
    _SharedEval,
)

FIXTURES = Path(__file__).parent / "fixtures" / "table"

# sha256 of each hand-written fixture. The SDK and front-end copies assert these too.
FIXTURE_SHA256 = {
    "result-empty.json": "218b2f06c6f86f91b6fc2fe1ad5c264ad7f5c26c488feaad7cb19d5adbcb6149",
    "result-trigger.json": "db5eae12cac8c9176544b3b6f0abc838a35826dcfd729016e01268a030be6c27",
    "spec-max-points-absent.json": "ededfd70cdb9d34c2b96db79babfdbd18d35dabda2cb51c4f13736c0b0f8a26b",
    "spec-reject-max-points-0.json": "726dc8abdc71806c5e97645e5b5771534e6094dd5a6ae9efd9180e0aa214566b",
    "spec-reject-edge-on-grid.json": "35ec841bdff3d6d0b0c2bd058c2f5a939faf1600e919f7f3b50bb3bf2e1cdc51",
    "spec-reject-max-points-10001.json": "d80a26c31964b81fc53c5cd84acff848ff3e71d11b31b46ebebf9f3182d15977",
    "spec-timer.json": "fc289736da0a01a13a1770c2d3ebf202a0c6d584d6430ef12068cecbd703b6cb",
    "spec-trigger-plotted.json": "b3623eeca40147e1962bb5a803310463931eee48cf41371eede0cbe3a35b2dd3",
    "spec-trigger.json": "6139acb178013631174b975be3136db85e934de17ca8bc9540047b08dbf183fb",
    "spec-trigger-falling.json": "a8d14d97a170238ca51ffedb4ad2dba2df074d94c81d645b70e39b5b03828c35",
    "spec-trigger-rising.json": "e98a02139fa363b96456bef437331b55491785bc0cc72d55bb93b4f6a12a959a",
    "spec-x-equals-y.json": "f002685eb32970e0a2ae86ebaec08ccddea4ed77f77b9058b90309759a12ddf2",
}

HOUR_NS = 3600 * 1_000_000_000
WS_URL = "/fox/v1/ws/formulas?api_key=test-read"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _valid_spec_fixtures() -> list[str]:
    return sorted(n for n in FIXTURE_SHA256 if n.startswith("spec-") and not n.startswith("spec-reject-"))


def _table(**overrides) -> dict:
    """A valid Trigger-PV table spec (wire form), with overrides."""
    spec = {
        "kind": "table",
        "index": "first_input",
        "bindings": {"x": "PV:A", "y": "PV:B"},
        "columns": {"x": "{x}", "y": "{y} * 2"},
        "trigger": "x",
    }
    spec.update(overrides)
    return spec


def _wide_table(n_columns: int, n_bindings: int) -> dict:
    bindings = {f"b{i}": f"PV:W{i}" for i in range(n_bindings)}
    names = list(bindings)
    columns = {f"c{i}": "{" + names[i % n_bindings] + "}" for i in range(n_columns)}
    # Every binding must be used: fold leftovers into the last column.
    leftovers = names[n_columns:]
    if leftovers:
        columns[f"c{n_columns - 1}"] = " + ".join("{" + b + "}" for b in [names[n_columns - 1], *leftovers])
    return _table(bindings=bindings, columns=columns, trigger=names[0])


@pytest.fixture
def service():
    data_source = FakeDataSource(
        latest={"PV:A": Sample(ts_ns=1000, value=5.0, severity=AlarmSeverity.NO_ALARM, source="archive")}
    )
    return FormulaService(data_source, Parser(), FoxConfig())


class _Recorder:
    """Records every subscribe and change-registration a manager makes."""

    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.watched: list[str] = []
        self.notifier = ChangeNotifier()

    async def subscribe(self, pvs: list[str]) -> None:
        self.subscribed.extend(pvs)

    def on_change(self, pv, cb):
        self.watched.append(pv)
        return self.notifier.subscribe(pv, cb)


@pytest.fixture
def recorder():
    return _Recorder()


@pytest.fixture
def manager(service, recorder):
    return SubscriptionManager(
        formula_service=service,
        on_change_fn=recorder.on_change,
        subscribe_live_fn=recorder.subscribe,
        table_port=Harness().port(),
    )


@pytest.fixture
def client(service, manager):
    fox_app.dependency_overrides[get_formula_service] = lambda: service
    fox_app.dependency_overrides[get_subscription_manager] = lambda: manager
    try:
        with TestClient(parent_app) as c:
            yield c
    finally:
        fox_app.dependency_overrides.pop(get_formula_service, None)
        fox_app.dependency_overrides.pop(get_subscription_manager, None)


def _collector():
    frames: list[dict] = []

    async def send(frame: dict) -> None:
        frames.append(frame)

    return frames, send


# --------------------------------------------------------------------------- #
# Contract validator
# --------------------------------------------------------------------------- #


class TestSpecValidator:
    @pytest.mark.parametrize(
        "overrides",
        [
            {},
            {"index": "grid", "trigger": None, "gridDtNs": 100_000_000},
            {"index": "grid", "trigger": None, "gridDtNs": 5_000_000_000},
            {"maxPoints": 1},
            {"maxPoints": 10_000},
            {"trigger": "y"},
            {"triggerEdge": "rising"},
            {"triggerEdge": "falling"},
            {"trigger_edge": "rising"},
        ],
    )
    def test_accepts(self, overrides):
        spec = _table(**overrides)
        if spec.get("trigger") is None:
            spec.pop("trigger", None)
        FormulaSpec(**spec)

    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            ({"columns": None}, "at least one column"),
            ({"columns": {}}, "at least one column"),
            ({"columns": {" ": "{x}"}}, "non-empty"),
            ({"expression": "{x}"}, "columns"),
            ({"mode": "binned"}, "raw"),
            ({"align": "interpolate"}, "last"),
            ({"index": "union"}, "first_input"),
            ({"index": "intersection"}, "first_input"),
            ({"trigger": None}, "trigger"),
            ({"trigger": "nope"}, "not a key"),
            ({"index": "grid", "gridDtNs": 1_000_000_000}, "not allowed"),
            ({"index": "grid", "trigger": None}, "gridDtNs"),
            ({"index": "grid", "trigger": None, "gridDtNs": 99_999_999}, "100 ms"),
            ({"maxPoints": 0}, "greater than 0"),
            ({"maxPoints": 10_001}, "between 1 and 10000"),
            ({"index": "grid", "trigger": None, "gridDtNs": 1_000_000_000, "triggerEdge": "rising"}, "'triggerEdge'"),
            ({"index": "grid", "trigger": None, "gridDtNs": 1_000_000_000, "triggerEdge": "falling"}, "'triggerEdge'"),
            ({"index": "union", "triggerEdge": "rising"}, "'triggerEdge'"),
            ({"index": "intersection", "triggerEdge": "falling"}, "'triggerEdge'"),
            ({"triggerEdge": "both"}, "rising"),
            ({"triggerEdge": "Rising"}, "rising"),
        ],
    )
    def test_rejects(self, overrides, fragment):
        with pytest.raises(ValidationError, match=fragment):
            FormulaSpec(**_table(**overrides))

    def test_absent_trigger_edge_means_every_update(self):
        spec = FormulaSpec(**_table())
        assert spec.trigger_edge is None
        assert "triggerEdge" not in spec.model_dump(by_alias=True, exclude_none=True)

    @pytest.mark.parametrize("edge", ["rising", "falling"])
    def test_trigger_edge_round_trips_on_the_wire(self, edge):
        spec = FormulaSpec(**_table(triggerEdge=edge))
        assert spec.trigger_edge == edge
        assert spec.model_dump(by_alias=True)["triggerEdge"] == edge

    def test_absent_max_points_means_1000(self):
        assert FormulaSpec(**_table()).max_points == 1000

    def test_non_table_kinds_still_need_an_expression(self):
        with pytest.raises(ValidationError, match="expression"):
            FormulaSpec(kind="scalar", bindings={"a": "PV:A"})

    @pytest.mark.parametrize(
        "extra", [{"columns": {"x": "{a}"}}, {"trigger": "a"}, {"trigger_edge": "rising"}, {"triggerEdge": "falling"}]
    )
    def test_non_table_kinds_reject_table_fields(self, extra):
        with pytest.raises(ValidationError, match="only valid for kind 'table'"):
            FormulaSpec(kind="timeseries", expression="{a}", bindings={"a": "PV:A"}, **extra)

    def test_non_table_max_points_is_unchanged(self):
        spec = FormulaSpec(kind="timeseries", expression="{a}", bindings={"a": "PV:A"}, max_points=50_000)
        assert spec.max_points == 50_000
        assert FormulaSpec(kind="timeseries", expression="{a}", bindings={"a": "PV:A"}).max_points is None


# --------------------------------------------------------------------------- #
# Hand-written fixtures
# --------------------------------------------------------------------------- #


class TestFixtures:
    def test_fixture_set_and_hashes_are_pinned(self):
        on_disk = {p.name for p in FIXTURES.glob("*.json")}
        assert on_disk == set(FIXTURE_SHA256)
        for name, digest in FIXTURE_SHA256.items():
            assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest, name

    @pytest.mark.parametrize("name", _valid_spec_fixtures())
    def test_spec_fixtures_validate(self, name, service):
        frame = _load(name)
        assert frame["type"] == "subscribe"
        spec = FormulaSpec(**frame["spec"])
        assert spec.kind == "table"
        service.check_table_window(frame["t0Ns"])
        service.check_table(spec)

    def test_absent_max_points_fixture_resolves_to_1000(self):
        frame = _load("spec-max-points-absent.json")
        assert "maxPoints" not in frame["spec"]
        assert FormulaSpec(**frame["spec"]).max_points == 1000

    @pytest.mark.parametrize(
        ("name", "fragment"),
        [
            ("spec-reject-max-points-0.json", "maxPoints|max_points|greater than 0"),
            ("spec-reject-max-points-10001.json", "maxPoints|max_points|greater than 0"),
            ("spec-reject-edge-on-grid.json", "'triggerEdge'"),
        ],
    )
    def test_reject_fixtures_are_rejected(self, name, fragment):
        with pytest.raises(ValidationError, match=fragment):
            FormulaSpec(**_load(name)["spec"])

    @pytest.mark.parametrize("edge", ["rising", "falling"])
    def test_edge_fixtures_are_the_trigger_fixture_plus_one_edge(self, edge):
        """An edge spec differs from the Every-update spec only by ``triggerEdge``."""
        base = _load("spec-trigger.json")
        frame = _load(f"spec-trigger-{edge}.json")
        assert frame["spec"].pop("triggerEdge") == edge
        assert frame == base
        assert "triggerEdge" not in base["spec"]
        assert FormulaSpec(**base["spec"]).trigger_edge is None

    def test_edge_on_grid_fixture_is_the_timer_fixture_plus_an_edge(self):
        frame = _load("spec-reject-edge-on-grid.json")
        assert frame["spec"].pop("triggerEdge") == "rising"
        assert frame == _load("spec-timer.json")

    def test_trigger_fixture_has_a_trigger_that_is_not_plotted(self):
        spec = FormulaSpec(**_load("spec-trigger.json")["spec"])
        plotted = {t for expr in spec.columns.values() for t in Parser().extract_tokens(expr)}
        assert spec.trigger not in plotted

    def test_x_equals_y_fixture_plots_one_binding_twice(self):
        spec = FormulaSpec(**_load("spec-x-equals-y.json")["spec"])
        assert len(spec.bindings) == 1 and spec.columns["x"] == spec.columns["y"]

    @pytest.mark.parametrize("name", ["result-trigger.json", "result-empty.json"])
    def test_result_fixtures_are_the_live_wire_envelope(self, name):
        frame = _load(name)
        assert set(frame) == {"type", "id", "data"}
        assert frame["type"] == "result"
        assert set(frame["data"]) == {"kind", "table"}
        assert set(frame["data"]["table"]) == {"tsNs", "columns", "inputs", "totalRows"}
        for status in frame["data"]["table"]["inputs"].values():
            assert set(status) == {"history", "status", "lateAfterFinal"}

        dto = FormulaResultDTO.model_validate(frame["data"])
        assert dto.kind == "table" and dto.table is not None
        assert dto.model_dump(by_alias=True, exclude_none=True) == frame["data"]
        contract = FormulaResult.model_validate(frame["data"])
        assert contract.model_dump(by_alias=True, exclude_none=True) == frame["data"]
        for column in frame["data"]["table"]["columns"].values():
            assert len(column) == len(frame["data"]["table"]["tsNs"])

    def test_result_fixture_covers_every_input_field_value(self):
        inputs = _load("result-trigger.json")["data"]["table"]["inputs"].values()
        assert {i["history"] for i in inputs} == {"seeded", "live-only", "not-archived", "truncated"}
        assert {i["status"] for i in inputs} == {"ok", "no-data"}
        assert {i["lateAfterFinal"] for i in inputs} == {True, False}

    def test_result_dto_rejects_unknown_history(self):
        data = _load("result-trigger.json")["data"]
        data["table"]["inputs"]["x"]["history"] = "archived"
        with pytest.raises(ValidationError):
            FormulaResultDTO.model_validate(data)


# --------------------------------------------------------------------------- #
# Service: live-only, validate, synchronous table check
# --------------------------------------------------------------------------- #


class TestService:
    async def test_validate_accepts_a_table(self, service):
        result = await service.validate(FormulaSpec(**_table()))
        assert result.valid and result.tokens == ["x", "y"]

    async def test_evaluate_rejects_a_table_as_live_only(self, service):
        with pytest.raises(TableLiveOnly):
            await service.evaluate(FormulaSpec(**_table()), t0_ns=-HOUR_NS, t1_ns=0)

    async def test_gateway_evaluate_rejects_a_table(self, service):
        previous = gateway._service
        gateway.bind(service)
        try:
            with pytest.raises(TableLiveOnly):
                await gateway.evaluate(FormulaSpec(**_table()), t0_ns=1, t1_ns=2)
            assert (await gateway.validate(FormulaSpec(**_table()))).valid
        finally:
            gateway.bind(previous)

    @pytest.mark.parametrize(
        ("overrides", "code"),
        [
            ({"columns": {"x": "{x}", "y": "{y} +"}}, ErrorCode.FORMULA_PARSE_ERROR),
            ({"columns": {"x": "{x}", "y": "__import__('os')"}}, ErrorCode.FORMULA_PARSE_ERROR),
            ({"columns": {"x": "{x}", "y": "{zz}"}}, ErrorCode.FORMULA_PARSE_ERROR),
            ({"columns": {"x": "{x}", "y": "{y} + {x2}", "x2": "{x}"}}, ErrorCode.FORMULA_CIRCULAR_REFERENCE),
            ({"bindings": {"x": "PV:A", "y": "fox:other"}}, ErrorCode.FORMULA_CIRCULAR_REFERENCE),
            ({"bindings": {"x": "PV:A", "y": ""}}, ErrorCode.INVALID_PV_NAME),
            ({"bindings": {"x": "PV:A", "y": "PV:B", "z": "PV:C"}}, ErrorCode.FORMULA_PARSE_ERROR),
        ],
    )
    def test_check_table_rejects(self, service, overrides, code):
        from canopy.core.errors import CanopyError

        with pytest.raises(CanopyError) as info:
            service.check_table(FormulaSpec(**_table(**overrides)))
        assert info.value.error_code == code

    def test_parse_error_names_the_column(self, service):
        from canopy.fox.errors import FormulaParseError

        with pytest.raises(FormulaParseError, match="column 'y'"):
            service.check_table(FormulaSpec(**_table(columns={"x": "{x}", "y": "{y} +"})))

    @pytest.mark.parametrize(("n_columns", "n_bindings"), [(16, 16), (16, 17)])
    def test_caps_accept_at_the_limit(self, service, n_columns, n_bindings):
        service.check_table(FormulaSpec(**_wide_table(n_columns, n_bindings)))

    @pytest.mark.parametrize(("n_columns", "n_bindings"), [(17, 17), (16, 18)])
    def test_caps_reject_above_the_limit(self, service, n_columns, n_bindings):
        from canopy.fox.errors import InvalidSpec

        with pytest.raises(InvalidSpec):
            service.check_table(FormulaSpec(**_wide_table(n_columns, n_bindings)))

    def test_column_cap_follows_config(self, monkeypatch):
        monkeypatch.setenv("CANOPY_FOX__TABLE_MAX_COLUMNS", "2")
        config = FoxConfig()
        assert config.table_max_columns == 2
        from canopy.fox.errors import InvalidSpec

        svc = FormulaService(FakeDataSource(), Parser(), config)
        spec = _table(columns={"x": "{x}", "y": "{y}", "z": "{x} + {y}"})
        with pytest.raises(InvalidSpec):
            svc.check_table(FormulaSpec(**spec))

    @pytest.mark.parametrize("t0_ns", [0, 1, -TABLE_MAX_LOOKBACK_NS - 1])
    def test_window_rejects(self, service, t0_ns):
        from canopy.fox.errors import InvalidSpec

        with pytest.raises(InvalidSpec):
            service.check_table_window(t0_ns)

    @pytest.mark.parametrize("t0_ns", [-1, -TABLE_MAX_LOOKBACK_NS])
    def test_window_accepts(self, service, t0_ns):
        service.check_table_window(t0_ns)

    def test_bare_binding_helper(self):
        assert bare_binding("{x}", {"x": "PV:A"}) == "x"
        assert bare_binding(" {x} ", {"x": "PV:A"}) == "x"
        assert bare_binding("{x} * 2", {"x": "PV:A"}) is None
        assert bare_binding("{y}", {"x": "PV:A"}) is None

    def test_error_classes_carry_their_status(self):
        assert (TableLiveOnly.status_code, TableLiveOnly.error_code.value) == (400, "table_live_only")
        assert (TableUpdateUnsupported.status_code, TableUpdateUnsupported.error_code.value) == (
            400,
            "table_update_unsupported",
        )
        assert (TableLimit.status_code, TableLimit.error_code.value) == (429, "table_limit")


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #


class TestRest:
    def test_validate_accepts_a_table(self, client):
        from tests.conftest import READ_HEADERS

        resp = client.post("/fox/v1/validate", json=_table(), headers=READ_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["valid"] is True

    def test_evaluate_of_a_table_is_400_table_live_only(self, client):
        from tests.conftest import READ_HEADERS

        body = {"spec": _table(), "t0Ns": -HOUR_NS, "t1Ns": 0}
        resp = client.post("/fox/v1/evaluate", json=body, headers=READ_HEADERS)
        assert resp.status_code == 400
        assert resp.json()["code"] == "table_live_only"


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #


def _first_frames(ws, n: int) -> list[dict]:
    return [ws.receive_json() for _ in range(n)]


class TestWebSocket:
    def test_one_shot_table_is_table_live_only(self, client, recorder):
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json({"type": "subscribe", "spec": _table(), "t0Ns": 1, "t1Ns": 2})
            subscribed, error, completed = _first_frames(ws, 3)
        assert subscribed["type"] == "subscribed"
        assert error["code"] == "table_live_only" and error["retryable"] is False
        assert completed["type"] == "completed"
        assert recorder.subscribed == []

    @pytest.mark.parametrize("t0_ns", [0, 5, -TABLE_MAX_LOOKBACK_NS - 1])
    def test_lookback_is_checked_on_subscribe(self, client, recorder, t0_ns):
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json({"type": "subscribe", "spec": _table(), "t0Ns": t0_ns})
            error = ws.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "invalid_spec" and error["retryable"] is False
        assert recorder.subscribed == [] and recorder.watched == []

    @pytest.mark.parametrize(
        ("spec", "code"),
        [
            (_table(columns={"x": "{x}", "y": "{y} +"}), "formula_parse_error"),
            (_table(columns={"x": "{x}", "y": "{x2}", "x2": "{x} + {y}"}), "formula_circular_reference"),
            (_table(bindings={"x": "PV:A", "y": "fox:abc"}), "formula_circular_reference"),
            (_wide_table(17, 17), "invalid_spec"),
            (_wide_table(16, 18), "invalid_spec"),
        ],
    )
    def test_bad_table_is_rejected_before_subscribed_and_never_subscribes(self, client, recorder, spec, code):
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json({"type": "subscribe", "spec": spec, "t0Ns": -HOUR_NS})
            error = ws.receive_json()
            # The socket stays usable and nothing else was queued before the pong.
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"
        assert error["type"] == "error" and "id" not in error
        assert error["code"] == code and error["retryable"] is False
        assert recorder.subscribed == [] and recorder.watched == []

    def test_contract_reject_frame_is_not_retryable(self, client):
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json({"type": "subscribe", "spec": _table(index="grid", gridDtNs=1_000_000), "t0Ns": -HOUR_NS})
            error = ws.receive_json()
        assert error["code"] == "invalid_spec" and error["retryable"] is False

    def test_invalid_message_frame_is_not_retryable(self, client):
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json({"type": "bogus"})
            error = ws.receive_json()
        assert error["code"] == "invalid_message" and error["retryable"] is False

    def test_valid_live_table_is_subscribed(self, client, recorder):
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json({"type": "subscribe", "spec": _table(), "t0Ns": -TABLE_MAX_LOOKBACK_NS})
            assert ws.receive_json()["type"] == "subscribed"
        assert sorted(recorder.subscribed) == ["PV:A", "PV:B"]

    def test_update_to_a_table_is_rejected_before_any_lookback_check(self, client):
        with client.websocket_connect(WS_URL) as ws:
            # t0Ns would fail both the lookback check and int coercion.
            ws.send_json({"type": "update", "id": "whatever", "spec": _table(), "t0Ns": "soon"})
            error = ws.receive_json()
        assert error["code"] == "table_update_unsupported" and error["retryable"] is False
        assert error["id"] == "whatever"

    def test_update_from_a_table_is_rejected(self, client):
        scalar = {"kind": "scalar", "expression": "{a}", "bindings": {"a": "PV:A"}}
        with client.websocket_connect(WS_URL) as ws:
            ws.send_json({"type": "subscribe", "spec": _table(), "t0Ns": -HOUR_NS})
            sub_id = ws.receive_json()["id"]
            ws.send_json({"type": "update", "id": sub_id, "spec": scalar})
            frame = ws.receive_json()
            while frame.get("type") != "error" or frame.get("code") != "table_update_unsupported":
                frame = ws.receive_json()
        assert frame["retryable"] is False and frame["id"] == sub_id

        with client.websocket_connect(WS_URL) as ws:
            ws.send_json({"type": "subscribe", "spec": _table(), "t0Ns": -HOUR_NS})
            sub_id = ws.receive_json()["id"]
            ws.send_json({"type": "update", "id": sub_id, "t0Ns": -2 * HOUR_NS})
            frame = ws.receive_json()
            while frame.get("type") != "error" or frame.get("code") != "table_update_unsupported":
                frame = ws.receive_json()
        assert frame["retryable"] is False


# --------------------------------------------------------------------------- #
# Manager: pre-subscribe validation, update reject, retryable per code
# --------------------------------------------------------------------------- #


class TestManager:
    async def test_a_manager_without_a_table_port_refuses_tables(self, service, recorder):
        bare = SubscriptionManager(
            formula_service=service, on_change_fn=recorder.on_change, subscribe_live_fn=recorder.subscribe
        )
        _, send = _collector()
        with pytest.raises(ModuleUnavailable):
            await bare.add_subscription("c", "s", FormulaSpec(**_table()), -HOUR_NS, 0, send)
        assert recorder.subscribed == [] and bare._shared_evals == {}

    async def test_bad_table_never_reaches_hare(self, manager, recorder):
        frames, send = _collector()
        spec = FormulaSpec(**_table(bindings={"x": "PV:A", "y": "fox:abc"}))
        with pytest.raises(FormulaCircularReference):
            await manager.add_subscription("c", "s", spec, -HOUR_NS, 0, send)
        assert recorder.subscribed == [] and recorder.watched == [] and frames == []
        assert manager._shared_evals == {}

    async def test_update_to_a_table_raises_even_for_an_unknown_id(self, manager):
        _, send = _collector()
        with pytest.raises(TableUpdateUnsupported):
            await manager.update_subscription("c", "nope", send, spec=FormulaSpec(**_table()), t0_ns=12345)

    async def test_update_from_a_table_raises(self, manager):
        _, send = _collector()
        await manager.add_subscription("c", "s", FormulaSpec(**_table()), -HOUR_NS, 0, send)
        with pytest.raises(TableUpdateUnsupported):
            await manager.update_subscription("c", "s", send, t0_ns=-2 * HOUR_NS)
        await manager.shutdown()

    async def test_one_shot_frames_are_not_retryable(self, manager, service, monkeypatch):
        for exc in (FormulaInputMissing("x"), RuntimeError("boom")):

            async def fail(*a, _exc=exc, **k):
                raise _exc

            monkeypatch.setattr(service, "evaluate", fail)
            frames, send = _collector()
            await manager.add_subscription("c", "s", FormulaSpec(**_table()), 1, 2, send)
            assert frames[0]["type"] == "error" and frames[0]["retryable"] is False
            assert frames[-1]["type"] == "completed"

    @pytest.mark.parametrize(
        ("code", "retryable"),
        [
            (ErrorCode.FORMULA_INPUT_MISSING, True),
            (ErrorCode.PV_NOT_BUFFERED, True),
            (ErrorCode.FORMULA_EVAL_TIMEOUT, True),
            (ErrorCode.ARCHIVER_UNAVAILABLE, True),
            (ErrorCode.UNHANDLED_EXCEPTION, True),
            (ErrorCode.FORMULA_PARSE_ERROR, False),
            (ErrorCode.FORMULA_UNSAFE, False),
            (ErrorCode.FORMULA_TYPE_ERROR, False),
            (ErrorCode.FORMULA_DIMENSION_MISMATCH, False),
            (ErrorCode.FORMULA_CIRCULAR_REFERENCE, False),
            (ErrorCode.TABLE_LIVE_ONLY, False),
        ],
    )
    async def test_fan_out_error_is_retryable_per_code(self, manager, code, retryable):
        assert is_retryable(code.value) is retryable
        frames, send = _collector()
        shared = _SharedEval(key="k", spec=FormulaSpec(**_table()), t0_ns=-1, interval_ms=0)
        shared.listeners.append(_Listener(client_id="c", sub_id="s", send_fn=send))
        await manager._fan_out(shared, {"type": "error", "code": code.value, "message": "msg"})
        assert frames == [{"type": "error", "code": code.value, "message": "msg", "retryable": retryable, "id": "s"}]

    @pytest.mark.parametrize(
        ("exc", "code", "retryable"),
        [
            (FormulaEvalTimeout("slow"), "formula_eval_timeout", True),
            (FormulaTypeError("bad"), "formula_type_error", False),
            (RuntimeError("boom"), "unhandled_exception", True),
        ],
    )
    async def test_poll_loop_error_frames_carry_retryable(self, manager, service, monkeypatch, exc, code, retryable):
        async def fail(*a, **k):
            raise exc

        monkeypatch.setattr(service, "evaluate", fail)
        frames, send = _collector()
        spec = FormulaSpec(kind="scalar", expression="{a}", bindings={"a": "PV:A"})
        await manager.add_subscription("c", "s", spec, 0, 0, send, interval_ms=50)
        for _ in range(100):
            if frames:
                break
            await asyncio.sleep(0.01)
        await manager.shutdown()
        assert frames[0]["code"] == code and frames[0]["retryable"] is retryable
