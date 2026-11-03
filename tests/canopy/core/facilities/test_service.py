"""Tests for FacilityService — hierarchy resolution and CRUD guards."""

from canopy.core.errors import (
    FacilityCycle,
    FacilityInUse,
    FacilityNotFound,
    FacilityHasChildren,
    FacilitySlugConflict,
)
from canopy.core.facilities.models import Facility
from canopy.core.facilities.schemas import FacilityCreate, FacilityUpdate
from canopy.core.facilities.service import FacilityService, register_in_use_checker


async def _seed_facility(session, slug: str, parent_id: str | None = None) -> Facility:
    facility = Facility(slug=slug, name=slug.upper(), parent_id=parent_id)
    session.add(facility)
    await session.flush()
    return facility


async def test_resolve_facility_scope_leaf_slug_returns_only_itself(session, session_maker):
    root = await _seed_facility(session, "root")
    leaf = await _seed_facility(session, "leaf", parent_id=root.id)
    await session.commit()

    service = FacilityService(session)
    scope = await service.resolve_facility_scope("leaf")
    assert scope == {leaf.id}


async def test_resolve_facility_scope_mid_tree_returns_descendants(session, session_maker):
    root = await _seed_facility(session, "lcls")
    sc = await _seed_facility(session, "lcls-sc", parent_id=root.id)
    nc = await _seed_facility(session, "lcls-nc", parent_id=root.id)
    leaf = await _seed_facility(session, "lcls-sc-leaf", parent_id=sc.id)
    await session.commit()

    service = FacilityService(session)
    scope = await service.resolve_facility_scope("lcls-sc")
    assert scope == {sc.id, leaf.id}
    assert nc.id not in scope


async def test_resolve_facility_scope_root_slug_returns_everything_beneath(session, session_maker):
    root = await _seed_facility(session, "top")
    sc = await _seed_facility(session, "top-sc", parent_id=root.id)
    nc = await _seed_facility(session, "top-nc", parent_id=root.id)
    await session.commit()

    service = FacilityService(session)
    scope = await service.resolve_facility_scope("top")
    assert scope == {root.id, sc.id, nc.id}


async def test_resolve_facility_scope_unknown_slug_returns_empty_set_not_none(session, session_maker):
    service = FacilityService(session)
    scope = await service.resolve_facility_scope("does-not-exist")
    assert scope == set()
    assert scope is not None


async def test_resolve_facility_scope_none_means_no_filter(session, session_maker):
    service = FacilityService(session)
    scope = await service.resolve_facility_scope(None)
    assert scope is None


async def test_resolve_facility_scope_no_parent_facility(session, session_maker):
    solo = await _seed_facility(session, "solo")
    await session.commit()

    service = FacilityService(session)
    scope = await service.resolve_facility_scope("solo")
    assert scope == {solo.id}


async def test_create_facility_rejects_duplicate_slug(session, session_maker):
    service = FacilityService(session)
    await service.create(FacilityCreate(slug="dupe", name="Dupe"))
    try:
        await service.create(FacilityCreate(slug="dupe", name="Dupe Again"))
        assert False, "expected FacilitySlugConflict"
    except FacilitySlugConflict:
        pass


async def test_create_facility_rejects_unknown_parent(session, session_maker):
    service = FacilityService(session)
    try:
        await service.create(FacilityCreate(slug="orphan", name="Orphan", parent="no-such-parent"))
        assert False, "expected FacilityNotFound"
    except FacilityNotFound:
        pass


async def test_update_rejects_self_as_parent(session, session_maker):
    facility = await _seed_facility(session, "lcls")
    await session.commit()

    service = FacilityService(session)
    try:
        await service.update(facility.id, FacilityUpdate(parent="lcls"))
        assert False, "expected FacilityCycle"
    except FacilityCycle:
        pass


async def test_update_rejects_reparenting_under_a_descendant(session, session_maker):
    root = await _seed_facility(session, "lcls")
    child = await _seed_facility(session, "lcls-sc", parent_id=root.id)
    grandchild = await _seed_facility(session, "lcls-sc-leaf", parent_id=child.id)
    await session.commit()

    service = FacilityService(session)
    try:
        await service.update(root.id, FacilityUpdate(parent="lcls-sc-leaf"))
        assert False, "expected FacilityCycle"
    except FacilityCycle:
        pass

    # Unaffected: a descendant can still be reparented under a sibling of its old parent.
    sibling = await _seed_facility(session, "lcls-nc", parent_id=root.id)
    await session.commit()
    updated = await service.update(grandchild.id, FacilityUpdate(parent="lcls-nc"))
    assert updated.parent_id == sibling.id


async def test_delete_blocked_by_children(session, session_maker):
    parent = await _seed_facility(session, "parent")
    await _seed_facility(session, "child", parent_id=parent.id)
    await session.commit()

    service = FacilityService(session)
    try:
        await service.delete(parent.id)
        assert False, "expected FacilityHasChildren"
    except FacilityHasChildren:
        pass


async def test_delete_blocked_by_in_use_checker(session, session_maker):
    facility = await _seed_facility(session, "in-use")
    await session.commit()

    async def always_in_use(facility_id: str, slug: str) -> bool:
        return facility_id == facility.id

    # The conftest's autouse `_reset_in_use_checkers` restores the registry after.
    register_in_use_checker(always_in_use, "widgets")
    service = FacilityService(session)
    try:
        await service.delete(facility.id)
        assert False, "expected FacilityInUse"
    except FacilityInUse as exc:
        assert "widgets" in exc.detail


async def test_delete_succeeds_when_unused(session, session_maker):
    facility = await _seed_facility(session, "unused")
    await session.commit()

    service = FacilityService(session)
    assert await service.delete(facility.id) is True
