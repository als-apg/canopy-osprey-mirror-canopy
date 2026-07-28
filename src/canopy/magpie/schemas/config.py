"""Config store DTOs.

All subclass ``CanopyModel`` so snake_case attrs serialize as camelCase on the
wire and accept either form. ``app`` is carried in the URL path (not the create
body); it appears on responses. ``data`` is an opaque JSON object — magpie does
not define its internal shape.
"""

from datetime import datetime

from pydantic import Field

from canopy.contracts.base import CanopyModel

# Generous cap on the serialized config blob — the consuming app owns semantics,
# magpie just refuses to store something unreasonably large. Mirrors converter's
# upload-size guard in spirit.
MAX_DATA_BYTES = 256 * 1024


class ConfigBase(CanopyModel):
    name: str | None = Field(None, max_length=255)
    description: str | None = None
    data: dict
    created_by: str | None = Field(None, max_length=255)


class NewConfigDTO(ConfigBase):
    """Body for creating a config. ``app`` comes from the path, not here."""


class UpdateConfigDTO(CanopyModel):
    """Partial update. ``app`` and ``slug`` are immutable."""

    name: str | None = Field(None, max_length=255)
    description: str | None = None
    data: dict | None = None


class ConfigDTO(ConfigBase):
    """Full config response DTO."""

    id: str
    app: str
    slug: str
    created_date: datetime
    last_modified_date: datetime
