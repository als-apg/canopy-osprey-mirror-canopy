"""Ordering test for Squirrel's request session: the commit lands before the response.

No database is needed. ``get_session`` is overridden with a generator that records the
commit, and an ASGI wrapper records when the response starts.
"""

from collections.abc import AsyncGenerator

import httpx
from fastapi import FastAPI
from starlette.types import Send, Scope, Message, Receive

from canopy.core.db import get_session
from canopy.squirrel.dependencies import SessionDep


async def test_session_commits_before_response_starts():
    """A route using ``SessionDep`` commits before ``http.response.start`` is sent.

    With FastAPI's default ``"request"`` scope the commit would run after the response,
    so the recorded order would be handler, response, commit.
    """
    events: list[str] = []
    app = FastAPI()

    async def recording_get_session() -> AsyncGenerator[object, None]:
        """Yield a placeholder session and record the commit after the route returns.

        Yields
        ------
        object
            A placeholder standing in for the ``AsyncSession``.
        """
        yield object()
        events.append("commit")

    @app.get("/probe")
    async def probe(session: SessionDep) -> dict[str, bool]:
        """Record that the handler ran.

        Parameters
        ----------
        session : AsyncSession
            The request session under test.

        Returns
        -------
        dict[str, bool]
            A trivial body.
        """
        events.append("handler")
        return {"ok": True}

    app.dependency_overrides[get_session] = recording_get_session

    async def recording_app(scope: Scope, receive: Receive, send: Send) -> None:
        """Forward to ``app`` and record when the response starts.

        Parameters
        ----------
        scope : Scope
            The ASGI connection scope.
        receive : Receive
            The ASGI receive channel.
        send : Send
            The ASGI send channel.
        """

        async def recording_send(message: Message) -> None:
            """Record ``http.response.start`` and pass every message through."""
            if message["type"] == "http.response.start":
                events.append("response")
            await send(message)

        await app(scope, receive, recording_send)

    transport = httpx.ASGITransport(app=recording_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/probe")

    assert r.status_code == 200, r.text
    assert events == ["handler", "commit", "response"]
