"""PV registry + meta-PV domain service."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from canopy.core.errors import BadRequest
from canopy.squirrel.errors import PvNotFound, MetaPvNotFound, PvAlreadyExists
from canopy.squirrel.schemas.pv import (
    PVElementDTO,
    NewPVElementDTO,
    PVSearchResponse,
    UpdatePVElementDTO,
)
from canopy.contracts.pagination import PagedResult
from canopy.squirrel.data.models import Pv
from canopy.squirrel.schemas.tag import TagDTO
from canopy.squirrel.schemas.meta_pv import MetaPVDTO, DisplayMetaPVDTO
from canopy.squirrel.data.repositories import (
    PvRepository,
    TagRepository,
    MetaPvRepository,
)

IDENTITY_KINDS: tuple[str, ...] = ("setpoint", "readback", "config")


def _identity(
    setpoint_address: str | None,
    readback_address: str | None,
    config_address: str | None,
) -> tuple[str, str]:
    """Resolve the identifying address of a PV.

    A PV is identified by its first non-null address in the order setpoint, readback,
    config. Two PVs are duplicates when their identities are equal, so a PV that owns a
    setpoint never collides with a setpoint-less PV sharing its readback.

    Parameters
    ----------
    setpoint_address : str | None
        Normalized setpoint address.
    readback_address : str | None
        Normalized readback address.
    config_address : str | None
        Normalized config address.

    Returns
    -------
    tuple[str, str]
        The identity kind (``"setpoint"``, ``"readback"`` or ``"config"``) and the address
        carrying it.

    Raises
    ------
    BadRequest
        If every address is ``None``.
    """
    for kind, address in zip(IDENTITY_KINDS, (setpoint_address, readback_address, config_address)):
        if address:
            return kind, address
    raise BadRequest("At least one address (setpoint, readback, or config) is required")


class PvService:
    """PV registry + meta-PV management. Owns no transaction — the caller commits."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.pv_repo = PvRepository(session)
        self.meta_pv_repo = MetaPvRepository(session)
        self.tag_repo = TagRepository(session)

    def _to_dto(self, pv: Pv) -> PVElementDTO:
        return PVElementDTO(
            id=pv.id,
            setpoint_address=pv.setpoint_address,
            readback_address=pv.readback_address,
            config_address=pv.config_address,
            device=pv.device,
            description=pv.description,
            abs_tolerance=pv.abs_tolerance,
            rel_tolerance=pv.rel_tolerance,
            tags=[TagDTO(id=t.id, name=t.name, description=t.description) for t in pv.tags],
            created_date=pv.created_at,
            last_modified_date=pv.updated_at,
        )

    @staticmethod
    def _normalize_address(address: str | None) -> str | None:
        """Trim an optional address from an API payload; empty becomes ``None``."""
        if address is None:
            return None
        normalized = address.strip()
        return normalized or None

    async def search_paged(
        self,
        search: str | None = None,
        page_size: int = 100,
        continuation_token: str | None = None,
        tag_filters: dict[str, list[str]] | None = None,
    ) -> PagedResult[PVElementDTO]:
        pvs, next_token, total_count = await self.pv_repo.search_by_name(
            search=search,
            limit=page_size,
            continuation_token=continuation_token,
            tag_filters=tag_filters,
        )
        return PagedResult(
            results=[self._to_dto(pv) for pv in pvs],
            continuation_token=next_token,
            total_count=total_count,
        )

    async def get_by_id(self, pv_id: str) -> PVElementDTO | None:
        # Route through get_by_ids so tags (and their group) are eager-loaded: _to_dto reads
        # pv.tags, and a bare session.get() would trigger an async lazy-load (MissingGreenlet).
        pvs = await self.pv_repo.get_by_ids([pv_id])
        return self._to_dto(pvs[0]) if pvs else None

    async def _find_by_identity(self, kind: str, address: str) -> Pv | None:
        """Look up the PV already holding an identity.

        Parameters
        ----------
        kind : str
            Identity kind as returned by ``_identity``.
        address : str
            Address carrying the identity.

        Returns
        -------
        Pv | None
            The PV owning that identity, or ``None`` when the identity is free.
        """
        if kind == "setpoint":
            return await self.pv_repo.find_by_setpoint(address)
        if kind == "readback":
            return await self.pv_repo.find_by_readback_without_setpoint(address)
        return await self.pv_repo.find_by_config_only(address)

    async def create(self, data: NewPVElementDTO) -> PVElementDTO:
        """Register a single PV.

        Parameters
        ----------
        data : NewPVElementDTO
            Wire payload for the new PV.

        Returns
        -------
        PVElementDTO
            The created PV.

        Raises
        ------
        BadRequest
            If no address is given, or a tag ID is unknown.
        PvAlreadyExists
            If another PV already holds the same identifying address.
        """
        setpoint_address = self._normalize_address(data.setpoint_address)
        readback_address = self._normalize_address(data.readback_address)
        config_address = self._normalize_address(data.config_address)
        kind, identity_address = _identity(setpoint_address, readback_address, config_address)

        if await self._find_by_identity(kind, identity_address):
            raise PvAlreadyExists(f"PV with {kind} address '{identity_address}' already exists")

        tags = []
        if data.tags:
            tags = await self.tag_repo.get_by_ids(data.tags)
            if len(tags) != len(data.tags):
                raise BadRequest("One or more tag IDs are invalid")

        pv = Pv(
            setpoint_address=setpoint_address,
            readback_address=readback_address,
            config_address=config_address,
            device=data.device,
            description=data.description,
            abs_tolerance=data.abs_tolerance,
            rel_tolerance=data.rel_tolerance,
            tags=tags,
        )
        try:
            pv = await self.pv_repo.create(pv)
        except IntegrityError as e:
            raise PvAlreadyExists(f"PV addresses violate uniqueness constraints: {e}") from e
        return self._to_dto(pv)

    async def create_many(self, data_list: list[NewPVElementDTO]) -> list[PVElementDTO]:
        """Register many PVs in one import.

        Parameters
        ----------
        data_list : list[NewPVElementDTO]
            Wire payloads for the PVs to import.

        Returns
        -------
        list[PVElementDTO]
            The created PVs, in input order.

        Raises
        ------
        BadRequest
            If a row has no address, a tag ID is unknown, or two rows in the import share an
            identifying address.
        PvAlreadyExists
            If a row's identifying address is already held by a registered PV.
        """
        normalized_records: list[tuple[str | None, str | None, str | None, NewPVElementDTO]] = []
        identities: dict[str, set[str]] = {kind: set() for kind in IDENTITY_KINDS}
        duplicates: dict[str, set[str]] = {kind: set() for kind in IDENTITY_KINDS}
        for data in data_list:
            setpoint_address = self._normalize_address(data.setpoint_address)
            readback_address = self._normalize_address(data.readback_address)
            config_address = self._normalize_address(data.config_address)
            kind, identity_address = _identity(setpoint_address, readback_address, config_address)
            if identity_address in identities[kind]:
                duplicates[kind].add(identity_address)
            identities[kind].add(identity_address)
            normalized_records.append((setpoint_address, readback_address, config_address, data))

        for kind in IDENTITY_KINDS:
            if duplicates[kind]:
                raise BadRequest(f"Duplicate {kind} addresses in import: {sorted(duplicates[kind])[:10]}")

        existence_lookups = (
            ("setpoint", self.pv_repo.get_existing_setpoints),
            ("readback", self.pv_repo.get_existing_readbacks_without_setpoint),
            ("config", self.pv_repo.get_existing_config_only),
        )
        for kind, lookup in existence_lookups:
            existing = await lookup(sorted(identities[kind]))
            if existing:
                raise PvAlreadyExists(f"{kind.capitalize()} addresses already exist: {sorted(existing)[:10]}")

        all_tag_ids: set[str] = set()
        for _, _, _, data in normalized_records:
            all_tag_ids.update(data.tags)

        tags_by_id = {}
        if all_tag_ids:
            tags = await self.tag_repo.get_by_ids(list(all_tag_ids))
            tags_by_id = {t.id: t for t in tags}

        pvs = []
        for setpoint_address, readback_address, config_address, data in normalized_records:
            pv_tags = [tags_by_id[tid] for tid in data.tags if tid in tags_by_id]
            pvs.append(
                Pv(
                    setpoint_address=setpoint_address,
                    readback_address=readback_address,
                    config_address=config_address,
                    device=data.device,
                    description=data.description,
                    abs_tolerance=data.abs_tolerance,
                    rel_tolerance=data.rel_tolerance,
                    tags=pv_tags,
                )
            )

        try:
            pvs = await self.pv_repo.bulk_create(pvs)
        except IntegrityError as e:
            raise PvAlreadyExists(f"PV addresses violate uniqueness constraints: {e}") from e
        return [self._to_dto(pv) for pv in pvs]

    async def update(self, pv_id: str, data: UpdatePVElementDTO) -> PVElementDTO | None:
        pv = await self.pv_repo.get_by_id(pv_id)
        if not pv:
            return None
        if data.device is not None:
            pv.device = data.device
        if data.description is not None:
            pv.description = data.description
        if data.abs_tolerance is not None:
            pv.abs_tolerance = data.abs_tolerance
        if data.rel_tolerance is not None:
            pv.rel_tolerance = data.rel_tolerance
        if data.tags is not None:
            tags = await self.tag_repo.get_by_ids(data.tags)
            if len(tags) != len(data.tags):
                raise BadRequest("One or more tag IDs are invalid")
            pv.tags = tags
        pv = await self.pv_repo.update(pv)
        return self._to_dto(pv)

    async def delete(self, pv_id: str) -> bool:
        pv = await self.pv_repo.get_by_id(pv_id)
        if not pv:
            return False
        try:
            await self.pv_repo.delete(pv)
        except IntegrityError as e:
            # The meta_pv FK has no cascade by design: a PV that is still a meta-PV can't
            # be deleted until its marker is removed. Surface a clean 400 instead of
            # letting the raw IntegrityError become a 500.
            raise BadRequest(
                "Cannot delete a PV that is still registered as a meta-PV; remove the meta-PV marker first"
            ) from e
        return True

    async def get_all_for_snapshot(self) -> list[tuple[str, str | None, str | None]]:
        addresses = await self.pv_repo.get_all_addresses()
        return [(pv_id, setpoint, readback) for pv_id, setpoint, readback, _ in addresses]

    async def search_filtered(
        self,
        q: str | None = None,
        devices: list[str] | None = None,
        tag_ids: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> PVSearchResponse:
        """Offset-paginated, server-side filtered search (the source's live-value join is dropped)."""
        pvs, total = await self.pv_repo.search_filtered(
            search_term=q, devices=devices or None, tag_ids=tag_ids or None, limit=limit, offset=offset
        )
        return PVSearchResponse(results=[self._to_dto(pv) for pv in pvs], total_count=total, limit=limit, offset=offset)

    async def get_all_devices(self) -> list[str]:
        """All distinct device names, for the frontend's device filter."""
        return await self.pv_repo.get_all_devices()

    # --- meta PVs ------------------------------------------------------------ #

    async def enable_meta_pv(self, id: str) -> MetaPVDTO:
        meta_pv = await self.meta_pv_repo.enable(id)
        if meta_pv is None:
            raise MetaPvNotFound(f"Meta PV {id} not found")
        return MetaPVDTO(pv_id=meta_pv.pv_id, display_name=meta_pv.display_name, is_active=meta_pv.is_active)

    async def disable_meta_pv(self, id: str) -> MetaPVDTO:
        meta_pv = await self.meta_pv_repo.disable(id)
        if meta_pv is None:
            raise MetaPvNotFound(f"Meta PV {id} not found")
        return MetaPVDTO(pv_id=meta_pv.pv_id, display_name=meta_pv.display_name, is_active=meta_pv.is_active)

    async def set_meta_display_name(self, id: str, display_name: str) -> MetaPVDTO:
        meta_pv = await self.meta_pv_repo.set_display_name(id, display_name)
        if meta_pv is None:
            raise MetaPvNotFound(f"Meta PV {id} not found")
        return MetaPVDTO(pv_id=meta_pv.pv_id, display_name=meta_pv.display_name, is_active=meta_pv.is_active)

    async def add_meta_pv(self, pv_name: str, display_name: str) -> MetaPVDTO:
        pv = await self.pv_repo.find_by_address(pv_name)
        if pv is None:
            raise PvNotFound(f"PV {pv_name} could not be found")
        try:
            new_pv = await self.meta_pv_repo.add(pv.id, display_name)
        except IntegrityError as e:
            if getattr(e.orig, "sqlstate", None) == "23505":
                raise PvAlreadyExists(
                    f"{pv.setpoint_address or pv.readback_address} ({pv.id}) is already a meta PV"
                ) from e
            raise
        return MetaPVDTO(pv_id=new_pv.pv_id, display_name=new_pv.display_name, is_active=new_pv.is_active)

    async def get_meta_pv(self, id: str) -> MetaPVDTO:
        meta_pv = await self.meta_pv_repo.get_one(id)
        if meta_pv is None:
            raise MetaPvNotFound(f"Meta PV {id} not found")
        return MetaPVDTO(pv_id=meta_pv.pv_id, display_name=meta_pv.display_name, is_active=meta_pv.is_active)

    async def get_meta_pvs(self, active: bool | None = None) -> list[DisplayMetaPVDTO]:
        meta_pv_metadata = await self.meta_pv_repo.get(active=active)
        pvs = {pv.id: pv for pv in await self.pv_repo.get_by_ids([pv.pv_id for pv in meta_pv_metadata])}
        return [
            DisplayMetaPVDTO(
                pv_id=meta_pv.pv_id,
                pv_name=pvs[meta_pv.pv_id].setpoint_address or pvs[meta_pv.pv_id].readback_address or "",
                display_name=meta_pv.display_name,
                is_active=meta_pv.is_active,
            )
            for meta_pv in meta_pv_metadata
        ]

    async def delete_meta_pv(self, id: str) -> None:
        rowcount = await self.meta_pv_repo.delete(id)
        if rowcount == 0:
            raise MetaPvNotFound(f"Meta PV {id} not found")
