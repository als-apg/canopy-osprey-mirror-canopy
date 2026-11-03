from typing import Annotated
from datetime import datetime

from pydantic import Field

from canopy.contracts.base import CanopyModel

NonEmptyStr = Annotated[str, Field(min_length=1)]

# Far above any entry a person writes, low enough that rendering it can't hold
# up the process for long.
MAX_BODY_MARKDOWN_CHARS = 1_000_000


class ElogConfigDTO(CanopyModel):
    """Whether an app can post, so its client can show or hide "Post to e-log".

    ``provider`` is the active provider's name, or ``None`` when the module is
    disabled. ``reason`` says why ``enabled`` is false and is ``None`` otherwise.
    """

    enabled: bool
    provider: str | None = None
    default_logbook_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class ElogLogbookDTO(CanopyModel):
    id: str
    name: str


class ElogTagDTO(CanopyModel):
    id: str
    name: str


class ElogUserDTO(CanopyModel):
    """A person known to the e-log backend (LDAP-backed at SLAC)."""

    uid: str
    common_name: str
    surname: str
    gecos: str
    mail: str


class ElogRecentEntryDTO(CanopyModel):
    """An entry this app's token posted, offered as a follow-up parent."""

    id: str
    title: str
    # UTC, always offset-aware on the wire.
    logged_at: datetime | None = None
    logged_by: str


class ElogAttachmentDTO(CanopyModel):
    """An uploaded attachment; pass ``id`` in ``attachmentIds`` when posting."""

    id: str


class ElogEntryCreateDTO(CanopyModel):
    """A new entry, or a follow-up of ``follows_up_entry_id`` when that is set.

    There is no author field: the e-log records the app's own token identity.
    ``additional_authors`` must be emails the e-log backend knows; elog-plus
    rejects the whole post if any one of them is not.
    """

    logbook_ids: list[NonEmptyStr] = Field(min_length=1)
    title: str = Field(min_length=1, max_length=255)
    body_markdown: str = Field(max_length=MAX_BODY_MARKDOWN_CHARS)
    tag_ids: list[NonEmptyStr] = Field(default_factory=list)
    attachment_ids: list[NonEmptyStr] = Field(default_factory=list)
    additional_authors: list[NonEmptyStr] = Field(default_factory=list)
    important: bool = False
    # When the event happened. An offset-aware value is converted to UTC; a
    # naive one is taken as UTC already.
    event_at: datetime | None = None
    follows_up_entry_id: NonEmptyStr | None = None


class ElogEntryResultDTO(CanopyModel):
    id: str
    url: str | None = None
