"""Unit tests for core.db session helpers — the commit/rollback policy split.

A fake session maker stands in for SQLAlchemy, so these need no database. They
pin the contract the worker relies on: ``session_scope`` never auto-commits,
while ``get_session`` (the request dependency) commits once on success. The
``SessionDep`` tests pin that the commit lands before the response starts and
that one request shares one session.
"""

from typing import Any, Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI
from starlette.types import Send, Scope, ASGIApp, Message, Receive
from sqlalchemy.ext.asyncio import AsyncSession

from canopy.core import db


class FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0
        self.events: list[str] = []

    async def commit(self):
        self.commits += 1
        self.events.append("commit")

    async def rollback(self):
        self.rollbacks += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def fake_session(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(db, "get_session_maker", lambda: lambda: session)
    return session


@pytest.fixture
def made_sessions(monkeypatch) -> list[FakeSession]:
    """Swap in a session maker that builds a new ``FakeSession`` per call.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Replaces ``db.get_session_maker``.

    Returns
    -------
    list[FakeSession]
        Every session made, in order.
    """
    made: list[FakeSession] = []

    def make_session() -> FakeSession:
        session = FakeSession()
        made.append(session)
        return session

    monkeypatch.setattr(db, "get_session_maker", lambda: make_session)
    return made


async def test_session_scope_does_not_auto_commit(fake_session):
    async with db.session_scope() as s:
        assert s is fake_session
    assert fake_session.commits == 0
    assert fake_session.rollbacks == 0


async def test_session_scope_rolls_back_on_error(fake_session):
    with pytest.raises(ValueError):
        async with db.session_scope():
            raise ValueError("boom")
    assert fake_session.commits == 0
    assert fake_session.rollbacks == 1


async def test_get_session_commits_on_success(fake_session):
    agen = db.get_session()
    s = await anext(agen)
    assert s is fake_session
    with pytest.raises(StopAsyncIteration):
        await anext(agen)  # resume past the yield -> commit -> generator ends
    assert fake_session.commits == 1
    assert fake_session.rollbacks == 0


async def test_get_session_rolls_back_on_error(fake_session):
    agen = db.get_session()
    await anext(agen)
    with pytest.raises(ValueError):
        await agen.athrow(ValueError("boom"))
    assert fake_session.commits == 0
    assert fake_session.rollbacks == 1


def recording_app(app: FastAPI, events: list[str]) -> ASGIApp:
    """Wrap ``app`` in an ASGI callable that records when the response starts.

    Parameters
    ----------
    app : FastAPI
        The application to forward every request to.
    events : list[str]
        Receives ``"response"`` when ``http.response.start`` is sent.

    Returns
    -------
    ASGIApp
        The recording wrapper.
    """

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        """Forward to ``app``, recording ``http.response.start`` on the way out."""

        async def recording_send(message: Message) -> None:
            """Record ``http.response.start`` and pass every message through."""
            if message["type"] == "http.response.start":
                events.append("response")
            await send(message)

        await app(scope, receive, recording_send)

    return asgi


async def test_session_dep_commits_before_response_starts(fake_session):
    """A route taking ``SessionDep`` commits before ``http.response.start`` is sent.

    The real ``get_session`` runs against the fake session maker. With FastAPI's
    default ``"request"`` scope the commit would run after the response, so the
    recorded order would be handler, response, commit.

    Parameters
    ----------
    fake_session : FakeSession
        The session ``get_session`` yields; it records the commit.
    """
    app = FastAPI()

    @app.get("/probe")
    async def probe(session: db.SessionDep) -> dict[str, bool]:
        """Record that the handler ran with the fake session.

        Parameters
        ----------
        session : AsyncSession
            The request session under test, ``fake_session`` here.

        Returns
        -------
        dict[str, bool]
            Whether the handler got the fake session.
        """
        fake_session.events.append("handler")
        return {"ok": session is fake_session}

    transport = httpx.ASGITransport(app=recording_app(app, fake_session.events))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/probe")

    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    assert fake_session.events == ["handler", "commit", "response"]


async def _route_and_service_share_a_session(service_session: Any) -> bool:
    """Request a route taking ``SessionDep`` whose service provider takes ``service_session``.

    Parameters
    ----------
    service_session : Any
        The annotation the service provider declares its session parameter with.

    Returns
    -------
    bool
        Whether the route and the service provider got the same session.
    """
    app = FastAPI()

    def get_service(session: service_session) -> object:
        """Stand in for a module service provider, returning its session for comparison."""
        return session

    @app.get("/probe")
    async def probe(
        session: db.SessionDep, service_session: Annotated[object, Depends(get_service)]
    ) -> dict[str, bool]:
        """Report whether the route and the service provider got the same session."""
        return {"shared": session is service_session}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/probe")
    assert r.status_code == 200, r.text
    return r.json()["shared"]


async def test_session_dep_shares_one_session_per_request(made_sessions):
    """A route and its service provider that both take ``SessionDep`` share one session.

    Parameters
    ----------
    made_sessions : list[FakeSession]
        Every session the request made.
    """
    assert await _route_and_service_share_a_session(db.SessionDep)
    assert len(made_sessions) == 1
    assert made_sessions[0].commits == 1


async def test_get_session_declared_without_session_dep_scope_makes_a_second_session(made_sessions):
    """A provider declaring ``Depends(get_session)`` itself gets a second session and transaction.

    FastAPI reuses a dependency within a request only when it is declared the same
    way, scope included, so this pins why routes and services take ``SessionDep``.

    Parameters
    ----------
    made_sessions : list[FakeSession]
        Every session the request made.
    """
    assert not await _route_and_service_share_a_session(Annotated[AsyncSession, Depends(db.get_session)])
    assert len(made_sessions) == 2
