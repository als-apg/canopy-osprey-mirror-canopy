"""Entry point: ``python -m canopy_mcp --http [--host H] [--port P | --fd N]``.

Only the streamable-HTTP transport is supported: the server also serves the
embedded viewer and draft screens over the same port, which stdio cannot do,
so a launch without ``--http`` is refused with a clear message.

``--fd N`` serves on a listening socket the parent process already holds and
passed down as descriptor ``N``, instead of binding ``--host``/``--port``. A
supervisor that keeps the socket across restarts loses no connection attempt
while the server is down.

A secret file named in the environment that cannot supply its secret ends the
launch with a non-zero status before any socket is bound or served.
"""

import sys
import socket
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
    parser.add_argument(
        "--fd",
        type=int,
        default=None,
        metavar="N",
        help="serve on the inherited listening socket with this descriptor instead of binding --host/--port",
    )
    return parser.parse_args(argv)


def _fail(message: str) -> int:
    print(f"canopy_mcp: {message}", file=sys.stderr)
    return 1


def _self_url(configured: str | None, port: int) -> str:
    """``CANOPY_SELF_URL`` when set, else loopback on the port being served."""
    return configured or f"http://127.0.0.1:{port}"


def _inherited_port(listener: socket.socket, fallback: int) -> int:
    """The TCP port of an inherited listener; ``fallback`` for a socket that has none."""
    if listener.family in (socket.AF_INET, socket.AF_INET6):
        return listener.getsockname()[1]
    return fallback


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

    from canopy_mcp.access import CredentialFileError
    from canopy_mcp.config import get_settings

    try:
        settings = get_settings()
        # Importing the app module builds its module-level app, which reads
        # the access-token file; both reads have to fail here, not mid-serve.
        from canopy_mcp import app as app_module
    except CredentialFileError as exc:
        return _fail(str(exc))

    if args.fd is None:
        # The full app, not bare FastMCP: /mcp plus the viewer, drafts, read-only
        # forward routes, /health and the proxy guard all share this one port.
        try:
            app = app_module.create_app(self_url=_self_url(settings.self_url, args.port))
        except CredentialFileError as exc:
            return _fail(str(exc))
        # No permessage-deflate on the browser's WebSocket hop: the relayed frames
        # are small and frequent, and compressing them costs more than it saves.
        uvicorn.run(app, host=args.host, port=args.port, ws_per_message_deflate=False)
        return 0

    # The descriptor's own family and type are kept (uvicorn's ``fd`` option
    # would treat a TCP listener as a Unix socket).
    try:
        listener = socket.socket(fileno=args.fd)
    except OSError as exc:
        return _fail(f"--fd {args.fd}: not an inherited socket ({exc.strerror or type(exc).__name__})")
    try:
        app = app_module.create_app(self_url=_self_url(settings.self_url, _inherited_port(listener, args.port)))
    except CredentialFileError as exc:
        listener.close()
        return _fail(str(exc))
    server = uvicorn.Server(uvicorn.Config(app, ws_per_message_deflate=False))
    server.run(sockets=[listener])
    # Same verdict as ``uvicorn.run``: a server that never started has failed.
    return 0 if server.started else 3


if __name__ == "__main__":
    sys.exit(main())
