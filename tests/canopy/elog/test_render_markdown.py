"""`render_markdown`: markdown to the HTML an e-log entry may hold, and nothing more."""

import pytest

from canopy.elog.adapters.elog_plus import render_markdown


@pytest.mark.parametrize(
    "md, never",
    [
        ("hi <script>alert(1)</script>", ["<script", "alert(1)"]),
        ("hi <img src=x onerror=alert(1)>", ["<img", "onerror"]),
        ("[x](javascript:alert(1))", ["javascript:"]),
    ],
)
def test_script_handlers_and_unsafe_urls_are_removed(md, never):
    html = render_markdown(md)

    for text in never:
        assert text not in html


def test_markdown_formatting_survives():
    md = "\n".join(
        [
            "# Title",
            "",
            "Some **bold**, *italic* and `code`, with a [link](https://example.org).",
            "",
            "- one",
            "- two",
            "",
            "| PV | Value |",
            "|----|-------|",
            "| A  | 1     |",
            "",
            "---",
            "",
            '<elog-entry-ref id="abc123"></elog-entry-ref>',
        ]
    )

    html = render_markdown(md)

    for fragment in [
        "<h1>Title</h1>",
        "<strong>bold</strong>",
        "<em>italic</em>",
        "<code>code</code>",
        '<a href="https://example.org"',
        "<li>one</li>",
        "<th>PV</th>",
        "<td>1</td>",
        "<hr>",
        '<elog-entry-ref id="abc123"></elog-entry-ref>',
    ]:
        assert fragment in html


def test_attributes_outside_the_allowlist_are_dropped():
    html = render_markdown('<p class="x" style="color:red" onclick="alert(1)">hi</p>')

    assert html == "<p>hi</p>"
