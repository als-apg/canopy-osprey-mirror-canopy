"""DTOs for Beaver's Screen IR JSON Schema API.

The schema documents themselves are served as raw vendored bytes (see
``canopy.beaver.api.v1.screen_schema``), not modeled here — only the version
listing has a wire shape.
"""

from canopy.contracts.base import CanopyModel


class ScreenSchemaVersionListDTO(CanopyModel):
    versions: list[str]
    total: int
