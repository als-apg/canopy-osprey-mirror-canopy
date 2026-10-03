"""Entry point: ``python -m canopy_mcp --http [--host H] [--port P]``.

Only the streamable-HTTP transport is supported: the server also serves the
embedded viewer and draft screens over the same port, which stdio cannot do,
so a launch without ``--http`` is refused with a clear message.
"""

import sys
import logging
import argparse
from collections.abc import Sequence

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

STDIO_REFUSED = (
    "canopy_mcp does not support the stdio transport; "
    "run it as an HTTP server: python -m canopy_mcp --http --host <host> --port <port>"
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m canopy_mcp", description="Canopy panel-builder MCP server.")
    parser.add_argument("--http", action="store_true", help="serve over streamable HTTP (required)")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind address (default {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"bind port (default {DEFAULT_PORT})")
    return parser.parse_args(argv)


def configure_logging() -> None:
    """Give the server's log records a level and logger name, on stderr.

    Without a root handler, records fall through to ``logging.lastResort`` and
    print only the message. This uses canopy-api's formatter and level
    (``CANOPY_LOG_FORMAT`` / ``CANOPY_LOG_LEVEL``) but writes to stderr, never
    stdout. A root logger that already has handlers (configured by the host, or
    by an earlier call) is left alone, so handlers are never duplicated.
    """
    root = logging.getLogger()
    if root.handlers:
        return

    from canopy.core.logging import make_formatter, configured_level

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(make_formatter())
    root.addHandler(handler)
    root.setLevel(configured_level())


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.http:
        print(STDIO_REFUSED, file=sys.stderr)
        return 2

    configure_logging()

    import uvicorn

    from canopy_mcp import app as app_module

    # The full app, not bare FastMCP: /mcp plus the viewer, drafts, read-only
    # forward routes, /health and the proxy guard all share this one port.
    app = app_module.create_app(self_url=f"http://127.0.0.1:{args.port}")
    # No permessage-deflate on the browser's WebSocket hop: the relayed frames
    # are small and frequent, and compressing them costs more than it saves.
    uvicorn.run(app, host=args.host, port=args.port, ws_per_message_deflate=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
