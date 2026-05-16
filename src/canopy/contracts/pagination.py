from canopy.contracts.base import CanopyModel


class PagedResult[T](CanopyModel):
    results: list[T]
    continuation_token: str | None = None
    total_count: int | None = None
