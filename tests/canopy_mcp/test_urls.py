from canopy_mcp.urls import draft_url, server_url
from canopy_mcp.config import Settings


def test_behind_a_url_prefix_the_url_is_the_prefix_plus_the_path():
    settings = Settings(url_prefix="/panel/canopy", self_url="http://127.0.0.1:9000")

    # The embedding host adds whatever lies in front of the prefix; the address is not used.
    assert draft_url(settings, "abc123") == "/panel/canopy/embed/drafts/abc123"
    assert server_url(settings, "/embed/") == "/panel/canopy/embed/"


def test_standalone_the_url_is_absolute_on_the_address_the_server_serves():
    assert draft_url(Settings(), "abc123") == "http://127.0.0.1:8765/embed/drafts/abc123"
    # The launcher fills in self_url from the port it serves, so a non-default port is named.
    assert (
        draft_url(Settings(self_url="http://127.0.0.1:9000/"), "abc123") == "http://127.0.0.1:9000/embed/drafts/abc123"
    )


def test_a_public_url_wins_over_the_server_s_own_address():
    settings = Settings(self_url="http://127.0.0.1:9000", public_url="http://panel.example:8443")
    assert draft_url(settings, "abc123") == "http://panel.example:8443/embed/drafts/abc123"
