"""WC6 write outcomes: the vector file, the write codes, and the binding/origin pass-through.

`canopy/contracts/wc6_vectors.json` is the one table every side (canopy, the
OSPREY adapter, the SDK) maps write outcomes from, so these tests pin it: the
exact row ids, the shape of each row, and that each write-code exception
class answers with the status, code and default detail the table names.

The sync tests drive async code with `asyncio.run` so this file runs the same
under the appliance pins (`ci/run-under-pins.sh`) as under the uv lock.
"""

import asyncio
import inspect
from importlib import resources

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from canopy.core import errors
from canopy.contracts import WC6_VECTORS_FILE, load_wc6_vectors
from canopy.hare.config import HareConfig
from canopy.hare.schemas.pv import PVWriteDTO, PVWriteOrigin
from canopy.hare.services.broker import PVBroker

ROW_IDS = {
    "1-off-disabled",
    "1-off-origin",
    "2",
    "3",
    "3a",
    "4",
    "4b",
    "5",
    "5a",
    "5b",
    "5c",
    "6",
    "7",
    "8",
    "9",
    "10",
    "11",
    "12",
    "13",
    "14",
    "14a",
    "14b-401",
    "14b-403",
    "14b-404",
    "14b-413",
    "15",
    "16",
    "16-coalesced",
}

ROW_FIELDS = {"source", "outcome", "status", "code", "audit", "detail", "toast", "dynamic", "default"}
OUTCOMES = {"refused", "target_changed", "switch_in_progress", "confirmed", "mismatch", "unconfirmed", "unknown"}

# code -> (exception class, status)
WRITE_CLASSES = {
    "pv_write_refused": (errors.PVWriteRefused, 403),
    "pv_target_changed": (errors.PVTargetChanged, 409),
    "pv_switch_in_progress": (errors.PVSwitchInProgress, 409),
    "pv_write_mismatch": (errors.PVWriteMismatch, 409),
    "pv_write_unconfirmed": (errors.PVWriteUnconfirmed, 502),
    "pv_write_unknown": (errors.PVWriteUnknown, 502),
}

GENERIC_REFUSAL = "the write was refused; nothing written"
UNKNOWN_TEXT = "outcome unknown — check the readback"
FRONT_TEXT = "the panel server refused this write; nothing written"


@pytest.fixture(scope="module")
def rows() -> dict:
    return load_wc6_vectors()["rows"]


# --- the vector file --------------------------------------------------------- #


def test_vector_file_is_package_data():
    # Read through importlib.resources, as the OSPREY adapter tests read it from the
    # installed wheel.
    ref = resources.files("canopy.contracts") / WC6_VECTORS_FILE
    assert ref.is_file()
    assert WC6_VECTORS_FILE == "wc6_vectors.json"


def test_vector_key_set_is_exactly_the_wc6_row_ids(rows):
    assert set(rows) == ROW_IDS


def test_every_row_has_the_same_fields(rows):
    for row_id, row in rows.items():
        assert set(row) == ROW_FIELDS, row_id
        assert set(row["audit"]) == {"decision", "reason"}, row_id
        assert isinstance(row["dynamic"], bool), row_id


def test_outcomes_are_the_closed_set(rows):
    for row_id, row in rows.items():
        if row_id == "16-coalesced":
            assert row["outcome"] is None  # the winner's outcome
        else:
            assert row["outcome"] in OUTCOMES, row_id


def test_server_rows_detail_is_the_toast(rows):
    # The envelope detail is the toast text for every row (slice 4).
    for row_id, row in rows.items():
        if row_id == "16-coalesced":
            continue
        assert row["detail"] == row["toast"], row_id
        assert row["detail"], row_id


def test_only_rows_6_to_9_and_11_are_dynamic(rows):
    assert {row_id for row_id, row in rows.items() if row["dynamic"]} == {"6", "7", "8", "9", "11"}


def test_dynamic_rows_carry_a_placeholder_and_a_default(rows):
    for row_id in ("6", "7", "8", "9"):
        assert rows[row_id]["detail"].startswith("<") and rows[row_id]["detail"].endswith(">")
        assert rows[row_id]["default"] == GENERIC_REFUSAL
    assert rows["11"]["detail"] == "written; the channel holds <observed>"
    assert rows["11"]["default"].startswith("written; the channel holds ")


def test_static_rows_default_is_their_detail(rows):
    for row_id, row in rows.items():
        if not row["dynamic"] and row_id != "16-coalesced":
            assert row["default"] == row["detail"], row_id


@pytest.mark.parametrize(
    ("row_id", "status", "code", "outcome", "decision", "reason", "text"),
    [
        ("1-off-disabled", 403, "pv_write_refused", "refused", "refused", "writes_disabled", "writes are off here"),
        (
            "1-off-origin",
            403,
            "pv_write_refused",
            "refused",
            "refused",
            "own_origin_missing",
            "writes are off here until agent pages have their own origin",
        ),
        (
            "2",
            409,
            "pv_target_changed",
            "target_changed",
            "refused",
            "control_target_changed",
            "the control target changed; nothing written",
        ),
        (
            "3",
            409,
            "pv_target_changed",
            "target_changed",
            "refused",
            "control_target_changed",
            "the control target changed; nothing written",
        ),
        (
            "3a",
            403,
            "pv_write_refused",
            "refused",
            "refused",
            "posture_changed",
            "the write state of this target changed; the panel is reconnecting - retry",
        ),
        (
            "4",
            403,
            "pv_write_refused",
            "refused",
            "refused",
            "control_context_unavailable",
            "no control target record; nothing written",
        ),
        (
            "4b",
            403,
            "pv_write_refused",
            "refused",
            "refused",
            "control_context_unavailable",
            "this control target cannot be served here; nothing written",
        ),
        (
            "5",
            409,
            "pv_switch_in_progress",
            "switch_in_progress",
            "refused",
            "switch_in_progress",
            "a target switch is in progress; nothing written",
        ),
        (
            "5a",
            403,
            "pv_write_refused",
            "refused",
            "refused",
            "audit_unavailable",
            "the audit ledger is unavailable; nothing written",
        ),
        (
            "5b",
            403,
            "pv_write_refused",
            "refused",
            "refused",
            "write_in_progress",
            "a write to this channel is still in progress; nothing written",
        ),
        (
            "5c",
            403,
            "pv_write_refused",
            "refused",
            "refused",
            "switch_state_unavailable",
            "the switch state could not be checked; nothing written",
        ),
        ("6", 403, "pv_write_refused", "refused", "refused", "channel_write_blocked", "<error_message>"),
        ("7", 403, "pv_write_refused", "refused", "refused", "channel_limits_violation", "<violation_reason>"),
        ("8", 403, "pv_write_refused", "refused", "refused", "channel_write_blocked", "<error_message>"),
        ("9", 403, "pv_write_refused", "refused", "refused", "channel_write_blocked", "<error_message>"),
        ("10", 200, None, "confirmed", "allowed", "write_landed", "written"),
        (
            "11",
            409,
            "pv_write_mismatch",
            "mismatch",
            "allowed",
            "write_unconfirmed",
            "written; the channel holds <observed>",
        ),
        ("12", 502, "pv_write_unconfirmed", "unconfirmed", "allowed", "write_unconfirmed", "sent, not verified"),
        ("13", 502, "pv_write_unknown", "unknown", "allowed", "write_unconfirmed", UNKNOWN_TEXT),
        ("14", 502, "pv_write_unknown", "unknown", "allowed", "write_unconfirmed", UNKNOWN_TEXT),
        ("14a", 405, None, "refused", None, None, "writes are not enabled on this server"),
        ("14b-401", 401, "invalid_access_credential", "refused", None, None, FRONT_TEXT),
        ("14b-403", 403, "pv_write_refused", "refused", None, None, FRONT_TEXT),
        ("14b-404", 404, "pv_write_refused", "refused", None, None, FRONT_TEXT),
        ("14b-413", 413, "pv_write_refused", "refused", None, None, FRONT_TEXT),
        ("15", None, None, "unknown", None, None, UNKNOWN_TEXT),
        ("16", None, None, "target_changed", None, None, "the control target changed; nothing written"),
    ],
)
def test_row_matches_wc6(rows, row_id, status, code, outcome, decision, reason, text):
    # Texts verbatim from CONTRACTS WC6 (rows 5b and 14b from PROPOSAL FR2).
    row = rows[row_id]
    assert row["status"] == status
    assert row["code"] == code
    assert row["outcome"] == outcome
    assert row["audit"] == {"decision": decision, "reason": reason}
    assert row["detail"] == text
    assert row["toast"] == text


def test_coalesced_row(rows):
    row = rows["16-coalesced"]
    assert row["status"] is None and row["code"] is None and row["detail"] is None
    assert row["toast"] == "a newer value replaced this one; nothing more sent"
    assert row["audit"] == {"decision": None, "reason": None}


def test_access_credential_code_matches_front(rows):
    from canopy_mcp.access import REFUSAL_CODE

    assert rows["14b-401"]["code"] == REFUSAL_CODE


def test_detail_cut_is_256(rows):
    assert load_wc6_vectors()["detail_max_chars"] == 256 == errors.WRITE_DETAIL_MAX_CHARS


# --- the write codes --------------------------------------------------------- #


@pytest.mark.parametrize("code", sorted(WRITE_CLASSES))
def test_write_code_is_an_error_code(code):
    assert errors.ErrorCode(code).value == code


@pytest.mark.parametrize("code", sorted(WRITE_CLASSES))
def test_class_status_and_code_match_every_row(rows, code):
    cls, status = WRITE_CLASSES[code]
    assert issubclass(cls, errors.CanopyError)
    assert cls.status_code == status
    assert cls.error_code.value == code
    # FRONT's own refusals (14b) reuse pv_write_refused with their own status
    # (404, 413); every DATA row of a code answers with the class's status.
    for row_id, row in rows.items():
        if row["code"] == code and row["source"] != "front":
            assert row["status"] == cls.status_code, row_id


def test_every_server_code_has_a_class(rows):
    server_codes = {row["code"] for row in rows.values() if row["source"] not in {"client", "front"}} - {None}
    assert server_codes == set(WRITE_CLASSES)


@pytest.mark.parametrize("code", sorted(WRITE_CLASSES))
def test_class_default_detail_matches_the_vectors(rows, code):
    cls, _ = WRITE_CLASSES[code]
    assert cls.default_detail
    assert cls().detail == cls.default_detail
    if code == "pv_write_refused":
        # Many rows: the generic text, which is also the dynamic rows' default.
        assert cls.default_detail == GENERIC_REFUSAL
        assert {rows[r]["default"] for r in ("6", "7", "8", "9")} == {GENERIC_REFUSAL}
        return
    # A single-row class takes its row's text (rows sharing a code share the text).
    defaults = {row["default"] for row in rows.values() if row["code"] == code and row["source"] != "front"}
    assert defaults == {cls.default_detail}


def test_raiser_detail_overrides_the_default(rows):
    exc = errors.PVWriteRefused(rows["5b"]["detail"])
    assert exc.detail == "a write to this channel is still in progress; nothing written"
    assert str(exc) == exc.detail


def test_existing_errors_keep_the_class_name_fallback():
    assert errors.BadRequest().detail == "BadRequest"


def test_mismatch_detail_names_the_observed_value():
    exc = errors.PVWriteMismatch.observed(3.5)
    assert exc.detail == "written; the channel holds 3.5"


def test_mismatch_detail_is_cut_at_256():
    exc = errors.PVWriteMismatch.observed("x" * 1000)
    assert len(exc.detail) == 256
    assert exc.detail.startswith("written; the channel holds xxx")


@pytest.mark.parametrize("code", sorted(WRITE_CLASSES))
def test_envelope_detail_is_the_toast(rows, code):
    # What the client sees: {code, detail}, detail = the row's text.
    cls, status = WRITE_CLASSES[code]
    app = FastAPI()
    errors.register_canopy_error_handler(app)

    @app.get("/raise")
    async def _raise():
        raise cls()

    with TestClient(app) as client:
        resp = client.get("/raise")
    assert resp.status_code == status
    assert resp.json() == {"code": code, "detail": cls.default_detail}


# --- PVWriteDTO -------------------------------------------------------------- #


def test_dto_binding_and_origin_are_optional():
    dto = PVWriteDTO.model_validate({"value": 1.0})
    assert dto.binding is None and dto.origin is None


def test_dto_parses_binding_and_origin():
    dto = PVWriteDTO.model_validate({"value": 1.0, "binding": "b-7", "origin": {"screen": "sr/overview", "rev": "abc"}})
    assert dto.binding == "b-7"
    assert dto.origin == PVWriteOrigin(screen="sr/overview", rev="abc")


def test_dto_origin_rev_is_optional():
    dto = PVWriteDTO.model_validate({"value": 1.0, "origin": {"screen": "sr/overview"}})
    assert dto.origin is not None and dto.origin.rev is None


def test_dto_origin_needs_a_screen():
    with pytest.raises(ValueError):
        PVWriteDTO.model_validate({"value": 1.0, "origin": {"rev": "abc"}})


# --- PVBroker.put pass-through over three connector shapes ------------------- #


class _PlainConnector:
    """Today's connectors (simulation, EPICS): no binding, no origin."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def put(self, pv, value, *, timeout=None, wait=True):
        self.calls.append({"pv": pv, "value": value, "timeout": timeout, "wait": wait})


class _ExplicitConnector:
    """The OSPREY adapter's shape: binding and origin as named keywords."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def put(self, pv, value, *, timeout=None, wait=True, origin=None, binding=None):
        self.calls.append(
            {"pv": pv, "value": value, "timeout": timeout, "wait": wait, "origin": origin, "binding": binding}
        )


class _KwargsConnector:
    """A connector taking `**kwargs`: counts as accepting both."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def put(self, pv, value, **kwargs):
        self.calls.append({"pv": pv, "value": value, **kwargs})


def _broker(connector) -> PVBroker:
    return PVBroker(connector, HareConfig(heartbeat_interval_s=100.0, monitor_grace_seconds=0))


ORIGIN = {"screen": "sr/overview", "rev": "r1"}


def test_plain_connector_receives_neither():
    conn = _PlainConnector()
    asyncio.run(_broker(conn).put("SIM:A", 1.0, wait=True, timeout=2.0, binding="b-1", origin=ORIGIN))
    assert conn.calls == [{"pv": "SIM:A", "value": 1.0, "timeout": 2.0, "wait": True}]


def test_explicit_connector_receives_both():
    conn = _ExplicitConnector()
    asyncio.run(_broker(conn).put("SIM:A", 1.0, binding="b-1", origin=ORIGIN))
    assert conn.calls == [
        {"pv": "SIM:A", "value": 1.0, "timeout": None, "wait": True, "origin": ORIGIN, "binding": "b-1"}
    ]


def test_kwargs_connector_receives_both():
    conn = _KwargsConnector()
    asyncio.run(_broker(conn).put("SIM:A", 1.0, binding="b-1", origin=ORIGIN))
    assert conn.calls == [
        {"pv": "SIM:A", "value": 1.0, "timeout": None, "wait": True, "origin": ORIGIN, "binding": "b-1"}
    ]


def test_accepting_connector_receives_none_when_the_body_named_neither():
    conn = _ExplicitConnector()
    asyncio.run(_broker(conn).put("SIM:A", 1.0))
    assert conn.calls[0]["origin"] is None and conn.calls[0]["binding"] is None


def test_origin_reaches_the_connector_as_a_plain_dict():
    conn = _ExplicitConnector()
    origin = PVWriteOrigin(screen="sr/overview")
    asyncio.run(_broker(conn).put("SIM:A", 1.0, origin=origin))
    passed = conn.calls[0]["origin"]
    assert type(passed) is dict
    assert passed == {"screen": "sr/overview", "rev": None}


def test_connector_signature_is_inspected_once(monkeypatch):
    conn = _ExplicitConnector()
    broker = _broker(conn)
    seen = []
    real = inspect.signature

    def counting(obj, *args, **kwargs):
        seen.append(obj)
        return real(obj, *args, **kwargs)

    monkeypatch.setattr(inspect, "signature", counting)
    for _ in range(3):
        asyncio.run(broker.put("SIM:A", 1.0, binding="b"))
    assert seen == []
    assert len(conn.calls) == 3
