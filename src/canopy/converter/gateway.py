"""Public surface of the converter module for in-process callers.

Other Canopy modules import only from this file; the rest of
``canopy.converter.*`` is private implementation. ``bind()`` wires the service
singleton at app startup; ``get_service()`` is exposed for ``dependencies.py``.
Tests can re-bind a fake service freely.
"""

from typing import Literal

import anyio

from canopy.converter.services.conversion_service import ConversionService

_service: ConversionService | None = None


def bind(service: ConversionService | None) -> None:
    """Wire (or unwire) the ConversionService singleton."""
    global _service
    _service = service


def get_service() -> ConversionService:
    if _service is None:
        raise RuntimeError("Converter gateway accessed before app startup bound a service")
    return _service


async def convert_screen(source: bytes, *, kind: Literal["edl", "ui"], filename: str | None = None) -> dict:
    """Parse legacy screen bytes -> validated Screen IR (plain dict).

    ``filename`` is the source's original name; the screen id and title derive from it.

    Conversion is CPU-bound and synchronous; it runs in a worker thread so an
    async caller's event loop is not blocked. Raises ``ConverterError``.
    """
    result = await anyio.to_thread.run_sync(_convert_sync, source, kind, filename)
    return result.screen


def _convert_sync(source: bytes, kind: Literal["edl", "ui"], filename: str | None = None):
    return get_service().convert(source, kind=kind, filename=filename)
