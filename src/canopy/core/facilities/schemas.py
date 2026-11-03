"""Facility DTOs."""

from pydantic import Field, field_validator

from canopy.contracts.base import CanopyModel


class FacilityDTO(CanopyModel):
    """A facility node. Flat, with ``parent_id`` — the client assembles the tree."""

    id: str
    slug: str
    name: str
    description: str | None = None
    parent_id: str | None = None


# Lowercase alphanumerics and single internal hyphens, not leading or trailing with
# a hyphen, and no doubled hyphen. Rejected rather than normalized: silently
# lowercasing `LCLS` would hand back a slug that doesn't match what the caller then
# types into a key's facility grant.
SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"

# Slugs react-squirrel's router already claims as static `/{facility}/...` page
# segments (slaclab/react-squirrel#208) — a facility with one of these names would
# be unreachable through the frontend, shadowed by the page route of the same name.
RESERVED_SLUGS = frozenset(
    {"snapshots", "tags", "pv-browser", "api-keys", "meta-pvs", "comparison", "snapshot-details"}
)


class FacilityCreate(CanopyModel):
    slug: str = Field(..., min_length=1, max_length=64, pattern=SLUG_PATTERN)
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    # A parent *slug*, not an id, matching the slug-keyed routes. ``FacilityDTO``
    # still returns ``parent_id``, since that's what a client walks to build the tree.
    parent: str | None = None

    @field_validator("slug")
    @classmethod
    def _reject_reserved_slug(cls, slug: str) -> str:
        if slug in RESERVED_SLUGS:
            raise ValueError(f"'{slug}' is a reserved slug and cannot be used for a facility")
        return slug


class FacilityUpdate(CanopyModel):
    """Partial update. ``slug`` is not editable — URLs and key grants reference it."""

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    parent: str | None = None
