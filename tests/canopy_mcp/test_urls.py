from canopy_mcp.urls import draft_url
from canopy_mcp.config import Settings


def test_draft_url_is_panel_relative_in_osprey_mode():
    settings = Settings(panel_id="canopy")

    url = draft_url(settings, "abc123", host="0.0.0.0", port=9000)

    # OSPREY's terminal adds the per-user prefix; the host/port are ignored.
    assert url == "/panel/canopy/embed/drafts/abc123"


def test_draft_url_is_absolute_standalone():
    settings = Settings(panel_id=None)

    assert draft_url(settings, "abc123", host="10.0.0.5", port=9000) == "http://10.0.0.5:9000/embed/drafts/abc123"
    assert draft_url(settings, "abc123") == "http://127.0.0.1:8765/embed/drafts/abc123"
