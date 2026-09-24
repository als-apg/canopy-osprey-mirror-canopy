"""Test-time mocks of ``POST /converter/v1/conversions`` for the ``convert_legacy`` tests.

Nothing converted is checked in: the ``.bob`` documents below are small and
synthetic, and every mocked response is produced at test time by running the
uploaded bytes through the real converter service and the route's own
``_to_dto`` -- so the mock answers exactly what the API would, droppedProps
included, without a server.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
from pytest_httpx import HTTPXMock

from canopy.converter.api.v1.conversions import _to_dto
from canopy.converter.services.conversion_service import ConversionService

CONVERSIONS_PATH = "/converter/v1/conversions"

_service = ConversionService()


def display(body: str, name: str = "Synthetic") -> str:
    """A minimal Phoebus ``.bob`` document around ``body``."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<display version="2.0.0">\n  <name>{name}</name>\n{body}\n</display>\n'
    )


def embedded(name: str, file: str, y: int = 0) -> str:
    return (
        f'  <widget type="embedded" version="2.0.0"><name>{name}</name>'
        f"<x>0</x><y>{y}</y><width>50</width><height>20</height><file>{file}</file></widget>\n"
    )


def label(name: str, text: str, y: int = 0) -> str:
    return (
        f'  <widget type="label" version="2.0.0"><name>{name}</name>'
        f"<x>0</x><y>{y}</y><text>{text}</text></widget>\n"
    )


def open_display_button(name: str, file: str, y: int = 0) -> str:
    return (
        f'  <widget type="action_button" version="3.0.0"><name>{name}</name><x>0</x><y>{y}</y>'
        f'<actions><action type="open_display"><file>{file}</file><target>tab</target></action></actions>'
        "</widget>\n"
    )


# A parent embedding one ref per classification outcome (id ``a/parent``):
# two siblings given in ``siblings``, one found at its default source, and one
# each for absolute, above-root, invalid-id and missing-default-source.
PARENT = display(
    label("Title", "Parent")
    + embedded("Warn", "embedded/_warn.bob", 20)
    + embedded("Drop", "embedded/_drop.bob", 40)
    + embedded("Def", "def.bob", 60)
    + embedded("Abs", "/abs/elsewhere.bob", 80)
    + embedded("Up", "../../up.bob", 100)
    + embedded("Bad", "bad name.bob", 120)
    + embedded("Nosrc", "nosrc.bob", 140),
    name="Parent",
)

# A sibling whose one node carries a converter warning (an execute action).
SIBLING_WARN = display(
    '  <widget type="action_button" version="3.0.0"><name>Run</name><x>0</x><y>0</y>'
    '<actions><action type="execute"><script file="EmbeddedPy"><text>print(1)</text></script></action>'
    "</actions></widget>\n"
    + label("Note", "warn sibling", 40),
    name="Warn",
)

# A sibling whose button carries a <tooltip>, which the .bob mapping drops.
SIBLING_DROP = display(
    '  <widget type="action_button" version="3.0.0"><name>Go</name><x>0</x><y>0</y>'
    "<tooltip>Press</tooltip></widget>\n",
    name="Drop",
)

# A plain sibling found at the default source ``dirname(path)/def.bob``.
SIBLING_DEFAULT = display(label("Plain", "default sibling"), name="Def")

# A sibling with its own embedded ref (never followed by convert_legacy).
SIBLING_WITH_REF = display(label("Plain", "has a ref") + embedded("Deeper", "deeper.bob", 20), name="Nested")


def write_sources(root: Path) -> dict[str, Path]:
    """Lay the synthetic sources out under ``root``; returns name -> absolute path.

    The parent lives in ``root/panel``; the ``siblings``-mapped files live
    elsewhere (``root/common``) so only the explicit map can find them, and
    ``def.bob`` sits beside the parent so the default-source rule finds it.
    ``nosrc.bob`` is deliberately absent.
    """
    files = {
        "parent": (root / "panel" / "parent.bob", PARENT),
        "warn": (root / "common" / "embedded" / "_warn.bob", SIBLING_WARN),
        "drop": (root / "common" / "embedded" / "_drop.bob", SIBLING_DROP),
        "default": (root / "panel" / "def.bob", SIBLING_DEFAULT),
    }
    out = {}
    for name, (path, text) in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        out[name] = path
    return out


def conversion_response(data: bytes, kind: str) -> dict:
    """The JSON body the route returns for ``data``: ``_to_dto(convert(...), warnings)``."""
    result = _service.convert(data, kind=kind)  # type: ignore[arg-type]
    return _to_dto(result, list(result.warnings)).model_dump(mode="json", by_alias=True)


def _multipart(request: httpx.Request) -> tuple[bytes, str | None, str | None]:
    """``(file bytes, filename, kind)`` from a multipart conversion request."""
    ctype = request.headers.get("content-type", "")
    match = re.search(r'boundary="?([^";]+)"?', ctype)
    assert match, f"not a multipart request: {ctype!r}"
    boundary = b"--" + match.group(1).encode()
    file_bytes, filename, kind = b"", None, None
    for part in request.content.split(boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, body = part.split(b"\r\n\r\n", 1)
        body = body[:-2] if body.endswith(b"\r\n") else body
        disposition = head.decode("utf-8", "replace")
        name = re.search(r'name="([^"]*)"', disposition)
        if not name:
            continue
        if name.group(1) == "file":
            file_bytes = body
            fname = re.search(r'filename="([^"]*)"', disposition)
            filename = fname.group(1) if fname else None
        elif name.group(1) == "kind":
            kind = body.decode()
    return file_bytes, filename, kind


def install_converter(httpx_mock: HTTPXMock, base_url: str) -> None:
    """Answer every conversion POST under ``base_url`` by really converting the upload."""

    def respond(request: httpx.Request) -> httpx.Response:
        data, filename, kind = _multipart(request)
        kind = kind or Path(filename or "").suffix.lstrip(".")
        return httpx.Response(200, json=conversion_response(data, kind))

    httpx_mock.add_callback(
        respond, method="POST", url=base_url + CONVERSIONS_PATH, is_optional=True, is_reusable=True
    )


def conversion_requests(httpx_mock: HTTPXMock) -> list[httpx.Request]:
    """Every request the code under test sent to the conversions route."""
    return [r for r in httpx_mock.get_requests() if r.url.path == CONVERSIONS_PATH]


def uploaded_filenames(httpx_mock: HTTPXMock) -> list[str | None]:
    return [_multipart(r)[1] for r in conversion_requests(httpx_mock)]
