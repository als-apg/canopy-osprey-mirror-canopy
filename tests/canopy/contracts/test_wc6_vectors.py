"""The write-outcome vectors: ``canopy/contracts/wc6_vectors.json``.

The file is the table a client maps an answer to a PV write (or the lack of
one) from: one row per outcome, with its status, code and toast text, plus the
cases that define how a long write detail is cut. canopy-sdk-js vendors a byte
copy and tests its own mapping against it, so these tests pin the file: the
row ids, the shape of each row and of each cut case, and the texts.
"""

from importlib import resources

import pytest

from canopy.contracts import WC6_VECTORS_FILE, load_wc6_vectors

ROW_IDS = {
    "6",
    "6-readonly-run",
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
    "16a",
    "16-coalesced",
}

ROW_FIELDS = {"source", "outcome", "status", "code", "audit", "detail", "toast", "dynamic", "default"}
TOP_LEVEL_KEYS = {"contract", "about", "detail_max_chars", "detail_cut_cases", "rows"}
CUT_CASE_FIELDS = {"case", "text", "limit", "expected"}
SOURCES = {"connector", "front", "client"}
OUTCOMES = {"refused", "target_changed", "confirmed", "mismatch", "unconfirmed", "unknown"}

GENERIC_REFUSAL = "the write was refused; nothing written"
UNKNOWN_TEXT = "outcome unknown — check the readback"
FRONT_TEXT = "the panel server refused this write; nothing written"
ROW_16_TEXT = "the control target or its write state changed; nothing written"
ROW_16A_TEXT = "the write state of <label> changed; nothing written"
ROW_16A_DEFAULT = "the write state of this target changed; nothing written"

CUT_CASES = load_wc6_vectors()["detail_cut_cases"]


@pytest.fixture(scope="module")
def rows() -> dict:
    return load_wc6_vectors()["rows"]


def test_vector_file_is_package_data():
    ref = resources.files("canopy.contracts") / WC6_VECTORS_FILE
    assert ref.is_file()
    assert WC6_VECTORS_FILE == "wc6_vectors.json"


def test_vector_top_level_keys_are_exactly_these():
    assert set(load_wc6_vectors()) == TOP_LEVEL_KEYS


def test_vector_key_set_is_exactly_these_row_ids(rows):
    assert set(rows) == ROW_IDS


def test_every_row_has_the_same_fields(rows):
    for row_id, row in rows.items():
        assert set(row) == ROW_FIELDS, row_id
        assert set(row["audit"]) == {"decision", "reason"}, row_id
        assert isinstance(row["dynamic"], bool), row_id
        assert row["source"] in SOURCES, row_id


def test_every_cut_case_has_the_same_fields():
    for case in CUT_CASES:
        assert set(case) == CUT_CASE_FIELDS, case
        assert isinstance(case["case"], str) and case["case"], case
        assert isinstance(case["text"], str), case["case"]
        assert isinstance(case["expected"], str), case["case"]
        assert isinstance(case["limit"], int) and not isinstance(case["limit"], bool), case["case"]
        assert case["limit"] > 0, case["case"]
    names = [case["case"] for case in CUT_CASES]
    assert len(names) == len(set(names))


def test_cut_cases_stay_within_their_limit():
    for case in CUT_CASES:
        assert len(case["expected"]) <= case["limit"], case["case"]
        if len(case["text"]) <= case["limit"]:
            assert case["expected"] == case["text"], case["case"]


def test_cut_cases_cover_the_cap_and_a_whole_text():
    cap = load_wc6_vectors()["detail_max_chars"]
    assert cap == 512
    assert any(case["limit"] == cap and len(case["text"]) > cap for case in CUT_CASES)
    assert any(case["expected"] == case["text"] for case in CUT_CASES)


def test_outcomes_are_the_closed_set(rows):
    for row_id, row in rows.items():
        if row_id == "16-coalesced":
            assert row["outcome"] is None  # the winner's outcome
        else:
            assert row["outcome"] in OUTCOMES, row_id


def test_every_row_but_coalesced_has_its_detail_as_the_toast(rows):
    for row_id, row in rows.items():
        if row_id == "16-coalesced":
            continue
        assert row["detail"] == row["toast"], row_id
        assert row["detail"], row_id


def test_only_rows_6_to_9_11_and_16a_are_dynamic(rows):
    assert {row_id for row_id, row in rows.items() if row["dynamic"]} == {"6", "7", "8", "9", "11", "16a"}


def test_dynamic_rows_carry_a_placeholder_and_a_default(rows):
    for row_id in ("6", "7", "8", "9"):
        assert rows[row_id]["detail"].startswith("<") and rows[row_id]["detail"].endswith(">")
        assert rows[row_id]["default"] == GENERIC_REFUSAL
    assert rows["11"]["detail"] == "written; the channel holds <observed>"
    assert rows["11"]["default"].startswith("written; the channel holds ")
    assert rows["16a"]["detail"] == ROW_16A_TEXT
    assert rows["16a"]["default"] == ROW_16A_DEFAULT


def test_static_rows_default_is_their_detail(rows):
    for row_id, row in rows.items():
        if not row["dynamic"] and row_id != "16-coalesced":
            assert row["default"] == row["detail"], row_id


@pytest.mark.parametrize(
    ("row_id", "source", "status", "code", "outcome", "decision", "reason", "text"),
    [
        ("6", "connector", 403, "pv_write_refused", "refused", "refused", "channel_write_blocked", "<error_message>"),
        (
            "6-readonly-run",
            "connector",
            403,
            "pv_write_refused",
            "refused",
            "refused",
            "channel_write_blocked",
            "the whole deployment is running read-only; nothing written",
        ),
        (
            "7",
            "connector",
            403,
            "pv_write_refused",
            "refused",
            "refused",
            "channel_limits_violation",
            "<violation_reason>",
        ),
        ("8", "connector", 403, "pv_write_refused", "refused", "refused", "channel_write_blocked", "<error_message>"),
        ("9", "connector", 403, "pv_write_refused", "refused", "refused", "channel_write_blocked", "<error_message>"),
        ("10", "connector", 200, None, "confirmed", "allowed", "write_landed", "written"),
        (
            "11",
            "connector",
            409,
            "pv_write_mismatch",
            "mismatch",
            "allowed",
            "write_unconfirmed",
            "written; the channel holds <observed>",
        ),
        (
            "12",
            "connector",
            502,
            "pv_write_unconfirmed",
            "unconfirmed",
            "allowed",
            "write_unconfirmed",
            "sent, not verified",
        ),
        ("13", "connector", 502, "pv_write_unknown", "unknown", "allowed", "write_unconfirmed", UNKNOWN_TEXT),
        ("14", "connector", 502, "pv_write_unknown", "unknown", "allowed", "write_unconfirmed", UNKNOWN_TEXT),
        ("14a", "client", 405, None, "refused", None, None, "writes are not enabled on this server"),
        ("14b-401", "front", 401, "invalid_access_credential", "refused", None, None, FRONT_TEXT),
        ("14b-403", "front", 403, "pv_write_refused", "refused", None, None, FRONT_TEXT),
        ("14b-404", "front", 404, "pv_write_refused", "refused", None, None, FRONT_TEXT),
        ("14b-413", "front", 413, "pv_write_refused", "refused", None, None, FRONT_TEXT),
        ("15", "client", None, None, "unknown", None, None, UNKNOWN_TEXT),
        ("16", "client", None, None, "target_changed", None, None, ROW_16_TEXT),
        ("16a", "client", None, None, "target_changed", None, None, ROW_16A_TEXT),
    ],
)
def test_row(rows, row_id, source, status, code, outcome, decision, reason, text):
    row = rows[row_id]
    assert row["source"] == source
    assert row["status"] == status
    assert row["code"] == code
    assert row["outcome"] == outcome
    assert row["audit"] == {"decision": decision, "reason": reason}
    assert row["detail"] == text
    assert row["toast"] == text


def test_row_16a_is_a_client_row_with_the_label_token(rows):
    row = rows["16a"]
    assert row["status"] is None and row["code"] is None
    assert row["detail"].count("<label>") == 1


def test_coalesced_row(rows):
    row = rows["16-coalesced"]
    assert row["source"] == "client"
    assert row["status"] is None and row["code"] is None and row["detail"] is None
    assert row["toast"] == "a newer value replaced this one; nothing more sent"
    assert row["audit"] == {"decision": None, "reason": None}


def test_static_texts_are_under_the_cap(rows):
    cap = load_wc6_vectors()["detail_max_chars"]
    for row_id, row in rows.items():
        for key in ("detail", "toast", "default"):
            if row[key] is not None:
                assert len(row[key]) <= cap, (row_id, key)


def test_vector_about_states_the_word_boundary_cut():
    about = load_wc6_vectors()["about"]
    assert "cut at the last whitespace before that limit" in about
