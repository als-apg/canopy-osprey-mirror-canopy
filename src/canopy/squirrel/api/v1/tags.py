"""Tag and tag-group endpoints."""

from fastapi import Query, Security, APIRouter
from pydantic import ValidationError

from canopy.core.security import require_read_access, require_write_access
from canopy.contracts.base import CanopyModel
from canopy.squirrel.errors import (
    TagNotFound,
    SquirrelError,
    TagGroupNotFound,
    TagGroupAlreadyExists,
)
from canopy.squirrel.schemas.tag import (
    TagCreate,
    TagUpdate,
    TagGroupDTO,
    AddTagResponse,
    TagGroupCreate,
    TagGroupUpdate,
    TagGroupSummaryDTO,
)
from canopy.squirrel.dependencies import TagServiceDep
from canopy.squirrel.api.v1._validation import is_uuid

router = APIRouter(prefix="/tags", tags=["Tags"])


@router.get("", dependencies=[Security(require_read_access)])
async def get_all_tag_groups(service: TagServiceDep) -> list[TagGroupSummaryDTO]:
    """All tag groups with tag counts."""
    return await service.get_all_groups_summary()


@router.post("", dependencies=[Security(require_write_access)])
async def create_tag_group(data: TagGroupCreate, service: TagServiceDep) -> TagGroupDTO:
    """Create a tag group."""
    return await service.create_group(data)


@router.get("/{group_id}", dependencies=[Security(require_read_access)])
async def get_tag_group(group_id: str, service: TagServiceDep) -> list[TagGroupDTO]:
    """Get a tag group by id with all its tags (wrapped in a list to match the frontend)."""
    if not is_uuid(group_id):
        raise TagGroupNotFound(f"Tag group {group_id} not found")
    group = await service.get_group_by_id(group_id)
    if not group:
        raise TagGroupNotFound(f"Tag group {group_id} not found")
    return [group]


@router.put("/{group_id}", dependencies=[Security(require_write_access)])
async def update_tag_group(group_id: str, data: TagGroupUpdate, service: TagServiceDep) -> TagGroupDTO:
    """Update a tag group's name and/or description."""
    if not is_uuid(group_id):
        raise TagGroupNotFound(f"Tag group {group_id} not found")
    group = await service.update_group(group_id, data)
    if not group:
        raise TagGroupNotFound(f"Tag group {group_id} not found")
    return group


@router.delete("/{group_id}", dependencies=[Security(require_write_access)])
async def delete_tag_group(
    group_id: str,
    service: TagServiceDep,
    # ``force`` is accepted but not yet enforced — see TagService.delete_group; deletion
    # always cascades the group's tags for now. Decision deferred pending team consultation.
    force: bool = Query(False),
) -> bool:
    """Delete a tag group (and its tags)."""
    if not is_uuid(group_id):
        raise TagGroupNotFound(f"Tag group {group_id} not found")
    if not await service.delete_group(group_id, force=force):
        raise TagGroupNotFound(f"Tag group {group_id} not found")
    return True


@router.post("/{group_id}/tags", dependencies=[Security(require_write_access)])
async def add_tag_to_group(
    group_id: str,
    data: TagCreate,
    service: TagServiceDep,
    skip_duplicates: bool = Query(False, description="Treat a duplicate tag as a no-op success"),
) -> AddTagResponse:
    """Add a tag to a group."""
    if not is_uuid(group_id):
        raise TagGroupNotFound(f"Tag group {group_id} not found")
    group, was_created = await service.add_tag_to_group(group_id, data, skip_duplicates=skip_duplicates)
    if not group:
        raise TagGroupNotFound(f"Tag group {group_id} not found")
    return AddTagResponse(group=group, was_created=was_created)


@router.put("/{group_id}/tags/{tag_id}", dependencies=[Security(require_write_access)])
async def update_tag(group_id: str, tag_id: str, data: TagUpdate, service: TagServiceDep) -> TagGroupDTO:
    """Update a tag within a group."""
    if not is_uuid(group_id) or not is_uuid(tag_id):
        raise TagNotFound(f"Tag {tag_id} not found in group {group_id}")
    group = await service.update_tag(group_id, tag_id, data)
    if not group:
        raise TagNotFound(f"Tag {tag_id} not found in group {group_id}")
    return group


@router.delete("/{group_id}/tags/{tag_id}", dependencies=[Security(require_write_access)])
async def remove_tag(group_id: str, tag_id: str, service: TagServiceDep) -> TagGroupDTO:
    """Remove a tag from a group."""
    if not is_uuid(group_id) or not is_uuid(tag_id):
        raise TagNotFound(f"Tag {tag_id} not found in group {group_id}")
    group = await service.remove_tag(group_id, tag_id)
    if not group:
        raise TagNotFound(f"Tag {tag_id} not found in group {group_id}")
    return group


class BulkTagImportRequest(CanopyModel):
    """Bulk import payload: ``{group_name: [tag_name, ...]}``."""

    groups: dict[str, list[str]]


class BulkTagImportResponse(CanopyModel):
    groups_created: int
    tags_created: int
    tags_skipped: int
    warnings: list[str]


@router.post("/bulk", dependencies=[Security(require_write_access)])
async def bulk_import_tags(data: BulkTagImportRequest, service: TagServiceDep) -> BulkTagImportResponse:
    """Bulk-import tag groups + tags, skipping (not failing on) duplicates."""
    groups_created = 0
    tags_created = 0
    tags_skipped = 0
    warnings: list[str] = []

    existing_summaries = await service.get_all_groups_summary()
    existing_group_map = {g.name: g.id for g in existing_summaries}

    for group_name, tag_names in data.groups.items():
        group_id = existing_group_map.get(group_name)
        if not group_id:
            try:
                group = await service.create_group(TagGroupCreate(name=group_name))
                group_id = group.id
                existing_group_map[group_name] = group_id
                groups_created += 1
            except TagGroupAlreadyExists as e:
                warnings.append(f"Failed to create group '{group_name}': {e.detail}")
                continue

        for tag_name in tag_names:
            try:
                _, was_created = await service.add_tag_to_group(
                    group_id, TagCreate(name=tag_name), skip_duplicates=True
                )
                if was_created:
                    tags_created += 1
                else:
                    tags_skipped += 1
                    warnings.append(f"Tag '{tag_name}' already exists in group '{group_name}'")
            except (SquirrelError, ValidationError) as e:
                # One bad tag (a domain error or an invalid tag name) becomes a warning rather
                # than aborting the whole import.
                warnings.append(f"Failed to add tag '{tag_name}' to group '{group_name}': {e}")

    return BulkTagImportResponse(
        groups_created=groups_created,
        tags_created=tags_created,
        tags_skipped=tags_skipped,
        warnings=warnings,
    )
