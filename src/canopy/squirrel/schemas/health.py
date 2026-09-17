from canopy.contracts.base import CanopyModel


class RestoreHooksStatusDTO(CanopyModel):
    """Which restore hooks are configured versus registered in this process.

    ``status`` is ``"disabled"`` when nothing is configured, ``"active"`` when every
    configured provider key is registered, ``"unknown"`` when none is, and
    ``"partial"`` when only some are. ``loaded`` holds the keys that will actually run
    and ``unknown`` the ones that match no provider.
    """

    status: str
    configured: list[str] = []
    loaded: list[str] = []
    unknown: list[str] = []
    available: list[str] = []
