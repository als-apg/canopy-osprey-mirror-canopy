"""canopy's host-link template must match the shell's, link for link.

``tests/canopy_mcp/fixtures/host_links.html`` is the head tail ``canopy_mcp``
is tested to emit; the shell homes its mirror at
``packages/shell/fixtures/host/links.html``, the template the viewer and Trace
e2e fixtures render. The two are produced differently (Python escapes, the JS
side fills a template), so attribute order, quoting and whitespace may differ:
the comparison is of the parsed, ordered ``(href, marker)`` pairs.

The sdk checkout only exists on a developer machine, so the test reads its root
from ``CANOPY_SDK_DIR`` and is skipped when the variable is unset.
"""

import os
from pathlib import Path
from html.parser import HTMLParser

import pytest

SDK_ENV = "CANOPY_SDK_DIR"
SDK_COPY = "packages/shell/fixtures/host/links.html"
CANOPY_COPY = Path(__file__).resolve().parent / "fixtures" / "host_links.html"
MARKER = "data-canopy-host-sheet"


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str | None, bool]] = []
        self.other: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        named = dict(attrs)
        if tag == "link" and (named.get("rel") or "").lower() == "stylesheet":
            self.links.append((named.get("href"), MARKER in named))
        else:
            self.other.append(tag)


def _parse(path: Path) -> _Links:
    parser = _Links()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser


def _sdk_copy() -> Path:
    raw = os.environ.get(SDK_ENV, "").strip()
    if not raw:
        pytest.skip(f"{SDK_ENV} not set; sibling sdk checkout unavailable")
    path = Path(raw) / SDK_COPY
    assert path.is_file(), f"{SDK_COPY} not found under {SDK_ENV}={raw}"
    return path


def test_host_links_match_the_shell():
    shell = _parse(_sdk_copy())
    local = _parse(CANOPY_COPY)
    assert local.links, f"{CANOPY_COPY} holds no stylesheet link"
    assert local.other == [] and shell.other == []
    assert all(marked for _, marked in local.links)
    assert all(href and href.startswith("{prefix}/") for href, _ in local.links)
    assert local.links == shell.links
