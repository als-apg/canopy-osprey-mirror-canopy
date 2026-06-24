"""Unit tests for core.db session helpers — the commit/rollback policy split.

A fake session maker stands in for SQLAlchemy, so these need no database. They
pin the contract the worker relies on: ``session_scope`` never auto-commits,
while ``get_session`` (the request dependency) commits once on success.
"""

import pytest

from canopy.core import db


class FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

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
