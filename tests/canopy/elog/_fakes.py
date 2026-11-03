"""In-memory adapter for route and service tests.

Records every call; test code sets ``.error`` (raised by every operation) or
``.follow_ups_supported`` / ``.health_result`` / ``.health_delay_s`` to drive
the path under test.
"""

import asyncio

from canopy.elog.config import ElogConfig, ElogAppConfig
from canopy.elog.errors import ElogNotSupported
from canopy.elog.schemas.elog import (
    ElogTagDTO,
    ElogUserDTO,
    ElogLogbookDTO,
    ElogAttachmentDTO,
    ElogEntryCreateDTO,
    ElogEntryResultDTO,
    ElogRecentEntryDTO,
)
from canopy.elog.adapters.base import AdapterHealth

LOGBOOK = ElogLogbookDTO(id="lb-ops", name="Operations")
TAG = ElogTagDTO(id="tag-1", name="routine")
USER = ElogUserDTO(uid="jdoe", common_name="Jane Doe", surname="Doe", gecos="Jane Doe", mail="jdoe@example.org")


class FakeElogAdapter:
    provider_name: str = "fake"

    def __init__(self, app: str = "squirrel") -> None:
        self.app = app
        self.calls: list[tuple] = []
        self.error: Exception | None = None
        self.follow_ups_supported = True
        self.health_result = AdapterHealth(healthy=True, identity=f"{app}@app.elog", latency_ms=1.0)
        self.health_delay_s = 0.0
        self.closed = False
        self._next_id = 1

    @classmethod
    def from_config(cls, app: str, app_config: ElogAppConfig, config: ElogConfig) -> "FakeElogAdapter":
        return cls(app)

    def _record(self, *call) -> None:
        self.calls.append(call)
        if self.error is not None:
            raise self.error

    def _new_id(self) -> str:
        entry_id = f"{self.app}-entry-{self._next_id}"
        self._next_id += 1
        return entry_id

    async def list_logbooks(self) -> list[ElogLogbookDTO]:
        self._record("list_logbooks")
        return [LOGBOOK]

    async def list_tags(self, logbook_id: str) -> list[ElogTagDTO]:
        self._record("list_tags", logbook_id)
        return [TAG] if logbook_id == LOGBOOK.id else []

    async def search_users(self, search: str, limit: int) -> list[ElogUserDTO]:
        self._record("search_users", search, limit)
        return [USER]

    async def list_recent_entries(self, logbook_ids: list[str], limit: int) -> list[ElogRecentEntryDTO]:
        self._record("list_recent_entries", logbook_ids, limit)
        return [ElogRecentEntryDTO(id="e-1", title="Earlier", logged_at="2026-10-20T17:00:00Z", logged_by="squirrel")]

    async def upload_attachment(self, filename: str, content_type: str | None, data: bytes) -> ElogAttachmentDTO:
        self._record("upload_attachment", filename, content_type, data)
        return ElogAttachmentDTO(id="att-1")

    async def create_entry(self, entry: ElogEntryCreateDTO) -> ElogEntryResultDTO:
        self._record("create_entry", entry)
        return ElogEntryResultDTO(id=self._new_id())

    async def create_follow_up(self, parent_entry_id: str, entry: ElogEntryCreateDTO) -> ElogEntryResultDTO:
        self._record("create_follow_up", parent_entry_id, entry)
        if not self.follow_ups_supported:
            raise ElogNotSupported("This e-log provider does not support follow-ups")
        return ElogEntryResultDTO(id=self._new_id())

    async def health(self) -> AdapterHealth:
        if self.health_delay_s:
            await asyncio.sleep(self.health_delay_s)
        return self.health_result

    async def close(self) -> None:
        self.closed = True


class ExplodingElogAdapter(FakeElogAdapter):
    """An out-of-tree adapter whose factory fails with something other than a config error."""

    @classmethod
    def from_config(cls, app: str, app_config: ElogAppConfig, config: ElogConfig) -> "ExplodingElogAdapter":
        raise RuntimeError("adapter factory blew up")
