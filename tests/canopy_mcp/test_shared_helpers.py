"""The helpers every route and reader shares: ``canopy_mcp.http``, ``canopy_mcp.text`` and ``canopy_mcp.validate``."""

import asyncio
from urllib.parse import quote

import pytest
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import Response, PlainTextResponse
from starlette.testclient import TestClient
from starlette.applications import Starlette

from canopy_mcp import http, text, validate

# --- http ----------------------------------------------------------------------------------


def _echo_app(cap: int) -> Starlette:
    async def echo(request: Request) -> Response:
        body = await http.read_capped_body(request, cap)
        return PlainTextResponse("over" if body is None else body.decode(), status_code=413 if body is None else 200)

    return Starlette(routes=[Route("/", echo, methods=["POST"])])


def test_a_body_at_the_cap_is_read_and_one_over_it_is_refused() -> None:
    client = TestClient(_echo_app(4))
    assert client.post("/", content=b"abcd").text == "abcd"
    assert client.post("/", content=b"abcde").status_code == 413


def test_a_streamed_body_over_the_cap_is_refused_whatever_the_header_said() -> None:
    def chunks():
        yield b"ab"
        yield b"cde"

    assert TestClient(_echo_app(4)).post("/", content=chunks()).status_code == 413


@pytest.mark.parametrize("declared", ["5", "not-a-number", "-x"])
def test_a_bad_or_oversized_content_length_is_refused_before_reading(declared: str) -> None:
    # One rule everywhere: a Content-Length that is over the cap or not an integer is refused.
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-length", declared.encode())]}

    async def never() -> dict:
        raise AssertionError("the body was read")

    assert asyncio.run(http.read_capped_body(Request(scope, never), 4)) is None


@pytest.mark.parametrize(
    ("site", "cross"),
    [
        (None, False),
        ("same-origin", False),
        ("none", False),
        (" Same-Origin ", False),
        ("cross-site", True),
        ("same-site", True),
    ],
)
def test_the_cross_site_guard(site: str | None, cross: bool) -> None:
    headers = [] if site is None else [(b"sec-fetch-site", site.encode())]
    request = Request({"type": "http", "method": "PUT", "path": "/", "headers": headers})
    assert http.is_cross_site(request) is cross
    refused = http.refuse_cross_site(request, lambda: PlainTextResponse("no", status_code=403))
    assert (refused is not None) is cross


def test_the_error_envelopes_keep_their_shapes() -> None:
    response = http.error_response(409, "stale", "changed")
    assert (response.status_code, response.body) == (409, b'{"error":"stale","detail":"changed"}')
    assert response.headers["cache-control"] == "no-cache"
    assert http.error_response(404, "not_found").body == b'{"error":"not_found"}'
    coded = http.code_detail_response(502, "unreachable", "down")
    assert coded.body == b'{"code":"unreachable","detail":"down"}' and "cache-control" not in coded.headers


@pytest.mark.parametrize(
    ("header", "matches"),
    [(None, False), ('"abc"', True), ('W/"abc"', True), ('"x", "abc"', True), ("*", True), ('"abd"', False)],
)
def test_an_etag_matches_its_if_none_match_forms(header: str | None, matches: bool) -> None:
    headers = [] if header is None else [(b"if-none-match", header.encode())]
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": headers})
    assert http.etag("abc") == '"abc"'
    assert http.not_modified(request, http.etag("abc")) is matches


# --- text ----------------------------------------------------------------------------------


@pytest.mark.parametrize("char", ["\t", "\n", "\r"])
def test_free_text_may_hold_tab_and_line_breaks_but_a_name_may_not(char: str) -> None:
    assert not text.is_forbidden_char(char)
    assert text.is_forbidden_char(char, in_name=True)


@pytest.mark.parametrize("char", ["\x00", "\x7f", "\x85", "‎", "‮", "⁦", "‍", "﻿", "\ud800"])
def test_controls_format_characters_and_lone_surrogates_are_forbidden_everywhere(char: str) -> None:
    assert text.is_forbidden_char(char) and text.is_forbidden_char(char, in_name=True)


def test_strip_forbidden_keeps_the_rest_of_the_text() -> None:
    assert text.strip_forbidden("a‮b\tc\ud800d") == "abcd"
    assert text.strip_forbidden("a\tb", in_name=False) == "a\tb"


def test_a_listed_directory_counts_like_a_prefix_in_casefold_collisions() -> None:
    assert text.casefold_collisions(["screens/a/x.png"], directories=["screens/A"]) == [["screens/A", "screens/a"]]
    # A directory that is also a member's prefix is one name, not a collision.
    assert text.casefold_collisions(["screens/a/x.png"], directories=["screens/a"]) == []


def test_fully_unquoted_settles_or_refuses() -> None:
    assert text.fully_unquoted("/a%252e%252e/b") == "/a../b"
    deep = "/x/.."
    for _ in range(text.MAX_UNQUOTE_ROUNDS + 1):
        deep = quote(deep, safe="")
    assert text.fully_unquoted(deep) is None


# --- validate ------------------------------------------------------------------------------


def test_clean_name_strips_and_refuses_what_a_name_cannot_hold() -> None:
    assert validate.clean_name("  Beam current  ") == "Beam current"
    for bad in ("", " " * 3, "x" * (validate.MAX_NAME_CHARS + 1), "a\nb", "a‮b", "a\ud800", 3):
        with pytest.raises(validate.InvalidValue):
            validate.clean_name(bad)


def test_a_query_is_bounded_in_utf8_bytes() -> None:
    assert validate.check_query("") == ""
    assert validate.check_query("x" * validate.MAX_QUERY_BYTES)
    assert validate.query_problem("é" * (validate.MAX_QUERY_BYTES // 2 + 1)) is not None
    assert validate.query_problem("\ud800") == "query must be valid UTF-8"
    with pytest.raises(validate.InvalidValue, match="string"):
        validate.check_query(None)


def test_compact_size_measures_compact_utf8_json() -> None:
    assert validate.compact_size({"a": [1, 2]}) == len('{"a":[1,2]}')
    assert validate.compact_size("é") == 4
    assert validate.compact_size("\ud800") == 5
