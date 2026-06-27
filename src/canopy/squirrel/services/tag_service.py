"""Tag + tag-group domain service. Pure DB CRUD — no EPICS, no Redis."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from canopy.squirrel.errors import TagAlreadyExists, TagGroupAlreadyExists
from canopy.squirrel.schemas.tag import (
    TagDTO,
    TagCreate,
    TagUpdate,
    TagGroupDTO,
    TagGroupCreate,
    TagGroupUpdate,
    TagGroupSummaryDTO,
)
from canopy.squirrel.data.models import Tag, TagGroup
from canopy.squirrel.data.repositories import TagRepository, TagGroupRepository


class TagService:
    """Tag-group / tag management. Owns no transaction — the caller commits."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.group_repo = TagGroupRepository(session)
        self.tag_repo = TagRepository(session)

    def _group_to_dto(self, group: TagGroup) -> TagGroupDTO:
        return TagGroupDTO(
            id=group.id,
            name=group.name,
            description=group.description,
            tags=[TagDTO(id=t.id, name=t.name, description=t.description) for t in group.tags],
            created_date=group.created_at,
            last_modified_date=group.updated_at,
        )

    async def get_all_groups_summary(self) -> list[TagGroupSummaryDTO]:
        groups_with_counts = await self.group_repo.get_all_with_counts()
        return [
            TagGroupSummaryDTO(id=group.id, name=group.name, description=group.description, tag_count=count)
            for group, count in groups_with_counts
        ]

    async def get_group_by_id(self, group_id: str) -> TagGroupDTO | None:
        group = await self.group_repo.get_with_tags(group_id)
        return self._group_to_dto(group) if group else None

    async def create_group(self, data: TagGroupCreate) -> TagGroupDTO:
        existing = await self.group_repo.find_by_name(data.name)
        if existing:
            raise TagGroupAlreadyExists(f"Tag group with name '{data.name}' already exists")
        group = TagGroup(name=data.name, description=data.description)
        group = await self.group_repo.create(group)
        return self._group_to_dto(group)

    async def update_group(self, group_id: str, data: TagGroupUpdate) -> TagGroupDTO | None:
        group = await self.group_repo.get_with_tags(group_id)
        if not group:
            return None
        if data.name is not None:
            existing = await self.group_repo.find_by_name(data.name)
            if existing and existing.id != group_id:
                raise TagGroupAlreadyExists(f"Tag group with name '{data.name}' already exists")
            group.name = data.name
        if data.description is not None:
            group.description = data.description
        group = await self.group_repo.update(group)
        return self._group_to_dto(group)

    async def delete_group(self, group_id: str, force: bool = False) -> bool:
        group = await self.group_repo.get_by_id(group_id)
        if not group:
            return False
        # NOTE: ``force`` is currently a no-op — a populated group is always deleted (its
        # tags cascade) regardless of the flag. This matches the source, where the guard was
        # never implemented either (``# TODO: Check if tags are in use (if not force)``). The
        # frontend always sends ``force=false`` and relies on this always-cascade behavior.
        # Whether to enforce a guard (refuse a non-empty group unless forced) or drop the
        # param outright is deferred.
        await self.group_repo.delete(group)
        return True

    async def add_tag_to_group(
        self, group_id: str, data: TagCreate, skip_duplicates: bool = False
    ) -> tuple[TagGroupDTO | None, bool]:
        """Add a tag to a group. Returns ``(group_dto | None, was_created)``.

        ``skip_duplicates`` makes an existing-tag name a no-op success instead of an error.
        """
        group = await self.group_repo.get_with_tags(group_id)
        if not group:
            return None, False

        existing = await self.tag_repo.find_by_group_and_name(group_id, data.name)
        if existing:
            if skip_duplicates:
                return await self.get_group_by_id(group_id), False
            raise TagAlreadyExists(f"Tag '{data.name}' already exists in this group")

        tag = Tag(name=data.name, description=data.description, group_id=group_id)
        await self.tag_repo.create(tag)

        # Refresh so the returned group includes the new tag.
        self.group_repo.session.expire(group)
        updated_group = await self.group_repo.get_with_tags(group_id)
        assert updated_group is not None  # we just created a tag in it
        return self._group_to_dto(updated_group), True

    async def update_tag(self, group_id: str, tag_id: str, data: TagUpdate) -> TagGroupDTO | None:
        tag = await self.tag_repo.get_by_id(tag_id)
        if not tag or tag.group_id != group_id:
            return None
        if data.name is not None:
            tag.name = data.name
        if data.description is not None:
            tag.description = data.description
        await self.tag_repo.update(tag)
        return await self.get_group_by_id(group_id)

    async def remove_tag(self, group_id: str, tag_id: str) -> TagGroupDTO | None:
        tag = await self.tag_repo.get_by_id(tag_id)
        if not tag or tag.group_id != group_id:
            return None
        await self.tag_repo.delete(tag)
        return await self.get_group_by_id(group_id)
