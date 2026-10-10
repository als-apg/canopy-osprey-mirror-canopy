"""The access credential: one shared secret in front of every route, and the files secrets come from.

A deployment that runs this server behind a proxy hands it a credential in a
file (``CANOPY_ACCESS_TOKEN_FILE``). :class:`AccessGate` then requires that
value in the ``X-Canopy-Access`` header of every HTTP request and WebSocket
handshake, whatever the path or method -- the viewer, ``/views``, the drafts,
the forwards and ``/mcp`` alike. The one exception is ``GET /health``, which a
supervisor has to be able to probe before it holds anything.

The gate sits outside routing, so a path that matches no route is refused like
any other rather than reported as missing, and a WebSocket is refused before
it is accepted. The header is the only carrier: a browser cannot set one on a
WebSocket handshake, so the credential is added by the proxy in front, never
by a page.

:func:`read_secret_file` is the strict reader behind every ``CANOPY_*_FILE``
variable. A deployment that names a file must never run on a secret other than
the one in it, so a file that cannot supply one stops the start.
"""

import hmac
from pathlib import Path

from starlette.types import Send, Scope, ASGIApp, Receive
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket

ACCESS_HEADER = "X-Canopy-Access"
OPEN_ROUTE = ("GET", "/health")

REFUSAL_CODE = "invalid_access_credential"
REFUSAL_DETAIL = f"A valid {ACCESS_HEADER} credential is required"
WS_POLICY_VIOLATION = 1008

_HEADER_NAME = ACCESS_HEADER.lower().encode("latin-1")


class CredentialFileError(RuntimeError):
    """A configured secret file cannot supply a secret. Never carries file content."""


def read_secret_file(variable: str, path: str | Path) -> str:
    """The secret held by ``path``, the file named by the environment variable ``variable``.

    The file holds the secret alone; surrounding whitespace (the usual
    trailing line break) is dropped. A file that is missing, unreadable, not
    text, empty, or that holds anything but one printable ASCII token raises
    :class:`CredentialFileError`, which names the variable and the path.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        raise CredentialFileError(f"{variable} {path}: cannot be read ({reason})") from None
    secret = raw.strip()
    if not secret:
        raise CredentialFileError(f"{variable} {path}: holds no secret")
    # It travels as an HTTP header value, so it has to be one that can.
    if not secret.isascii() or not secret.isprintable() or any(char.isspace() for char in secret):
        raise CredentialFileError(f"{variable} {path}: is not a single printable ASCII token")
    return secret


class AccessGate:
    """Refuse every request that does not carry the access credential, except ``GET /health``."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        if not token:
            raise ValueError("the access gate needs a credential to compare against")
        self.app = app
        self._token = token.encode("latin-1")

    def _admits(self, scope: Scope) -> bool:
        supplied = [value for name, value in scope.get("headers", ()) if name.lower() == _HEADER_NAME]
        # Exactly one value: a second one would mean the proxy in front passed
        # a caller's own header along beside the one it adds.
        return len(supplied) == 1 and hmac.compare_digest(supplied[0], self._token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        is_open = scope["type"] == "http" and (scope["method"], scope["path"]) == OPEN_ROUTE
        if is_open or self._admits(scope):
            await self.app(scope, receive, send)
            return

        refusal = JSONResponse({"code": REFUSAL_CODE, "detail": REFUSAL_DETAIL}, status_code=401)
        if scope["type"] == "http":
            await refusal(scope, receive, send)
            return
        websocket = WebSocket(scope, receive, send)
        if "websocket.http.response" in scope.get("extensions", {}):
            await websocket.send_denial_response(refusal)
        else:
            # A server that cannot answer a handshake with an HTTP response.
            await websocket.close(code=WS_POLICY_VIOLATION, reason=f"{REFUSAL_CODE}: {REFUSAL_DETAIL}")
