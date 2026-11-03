"""`canopy.elog.gateway`: the in-process surface other modules post through."""

import pytest

from canopy.elog import gateway
from canopy.elog.errors import ElogAttachmentTooLarge
from canopy.elog.schemas.elog import ElogEntryCreateDTO
from tests.canopy.elog._fakes import FakeElogAdapter
from tests.canopy.elog.conftest import build_service


@pytest.fixture
def bound_adapter():
    adapter = FakeElogAdapter("squirrel")
    gateway.bind(build_service({"squirrel": adapter}))
    yield adapter
    gateway.bind(None)


async def test_create_entry_posts_as_the_named_app(bound_adapter):
    entry = ElogEntryCreateDTO(logbook_ids=["lb-ops"], title="Restore done", body_markdown="ok")

    result = await gateway.create_entry("squirrel", entry, requested_by="worker")

    assert result.id == "squirrel-entry-1"
    assert [op for op, *_ in bound_adapter.calls] == ["create_entry"]


async def test_upload_attachment(bound_adapter):
    result = await gateway.upload_attachment("squirrel", filename="a.txt", content_type="text/plain", data=b"x")

    assert result.id == "att-1"


async def test_upload_over_the_limit_is_refused_before_the_provider():
    adapter = FakeElogAdapter("squirrel")
    gateway.bind(build_service({"squirrel": adapter}, max_attachment_bytes=8))
    try:
        with pytest.raises(ElogAttachmentTooLarge, match="9 bytes; limit is 8 bytes"):
            await gateway.upload_attachment("squirrel", filename="big.bin", content_type=None, data=b"123456789")
        await gateway.upload_attachment("squirrel", filename="ok.bin", content_type=None, data=b"12345678")
    finally:
        gateway.bind(None)

    assert [op for op, *_ in adapter.calls] == ["upload_attachment"]


def test_app_config(bound_adapter):
    assert gateway.app_config("squirrel").enabled is True


def test_unbound_gateway_raises():
    gateway.bind(None)

    with pytest.raises(RuntimeError, match="before app startup"):
        gateway.get_service()
