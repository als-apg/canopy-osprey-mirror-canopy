"""Per-node dropped legacy properties surface as ``droppedProps`` on a conversion."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from canopy.main import app
from tests.conftest import READ_HEADERS
from canopy.converter.services.conversion_service import DroppedProps, ConversionService

FIXTURES = Path(__file__).parent / "fixtures"


def _display(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<display version="2.0.0">\n  <name>Synthetic</name>\n' + body + "\n</display>\n"
    ).encode()


# An action button carrying a <tooltip>, which the .bob mapping cannot express.
BOB_WITH_DROP = _display(
    """
  <widget type="label" version="2.0.0">
    <name>Title</name>
    <x>10</x>
    <y>10</y>
    <text>Hello</text>
  </widget>
  <widget type="action_button" version="3.0.0">
    <name>Go</name>
    <x>10</x>
    <y>40</y>
    <pv_name>SYN:GO</pv_name>
    <tooltip>Press to go</tooltip>
  </widget>
"""
)

# A static label and a PV readback: everything maps, nothing is dropped.
BOB_NO_DROP = _display(
    """
  <widget type="label" version="2.0.0">
    <name>Title</name>
    <x>10</x>
    <y>10</y>
    <text>Hello</text>
  </widget>
  <widget type="textupdate" version="2.0.0">
    <name>Readback</name>
    <x>10</x>
    <y>40</y>
    <pv_name>SYN:PV</pv_name>
  </widget>
"""
)


def _post(client, *, filename, content):
    return client.post(
        "/converter/v1/conversions",
        headers=READ_HEADERS,
        files={"file": (filename, content, "application/octet-stream")},
    )


def _node_ids(node: dict) -> set[str]:
    ids = {node["id"]}
    for child in node.get("children", []):
        ids |= _node_ids(child)
    return ids


# --- route ---


def test_bob_upload_reports_dropped_props_per_node():
    with TestClient(app) as client:
        resp = _post(client, filename="drop.bob", content=BOB_WITH_DROP)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    dropped = body["droppedProps"]
    assert len(dropped) == 1
    entry = dropped[0]
    assert set(entry) == {"nodeId", "props"}
    assert "tooltip" in entry["props"]
    # The node id names a real node in the returned screen (the button).
    assert entry["nodeId"] in _node_ids(body["screen"]["root"])
    buttons = [c for c in body["screen"]["root"]["children"] if c["id"] == entry["nodeId"]]
    assert buttons and buttons[0]["type"] != "label"


def test_bob_upload_without_drops_returns_empty_list():
    with TestClient(app) as client:
        resp = _post(client, filename="clean.bob", content=BOB_NO_DROP)
    assert resp.status_code == 200, resp.text
    assert resp.json()["droppedProps"] == []


def test_dropped_props_do_not_leak_into_warnings_or_screen():
    with TestClient(app) as client:
        body = _post(client, filename="drop.bob", content=BOB_WITH_DROP).json()
    assert not any("tooltip" in w for w in body["warnings"])
    assert "droppedProps" not in json.dumps(body["screen"])


# --- service ---


def test_service_collects_drops_from_edl_fixture():
    # min.edl's text control marked editable converts to a pv-text-input, which
    # cannot carry its precision/showUnits props; read-only it is a pv-label that can.
    edl = (
        (FIXTURES / "min.edl")
        .read_text()
        .replace('controlPv "$(PREFIX):SETPOINT"\n', 'controlPv "$(PREFIX):SETPOINT"\neditable\n', 1)
    )
    assert "editable" in edl
    result = ConversionService().convert(edl.encode(), kind="edl")
    assert result.dropped_props, "the editable min.edl is expected to drop at least one property"
    for item in result.dropped_props:
        assert isinstance(item, DroppedProps)
        assert item.props, "only nodes with drops are reported"
    ids = [d.node_id for d in result.dropped_props]
    assert len(ids) == len(set(ids))
    assert set(ids) <= _node_ids(result.screen["root"])


def test_service_no_drops_is_empty_list():
    result = ConversionService().convert(BOB_NO_DROP, kind="bob")
    assert result.dropped_props == []
