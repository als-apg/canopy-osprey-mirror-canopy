"""Restore hook: trim magnet PVs after their BDES setpoint is written.

A magnet does not physically move when its BDES setpoint is updated — we must
additionally put the string ``TRIM`` to the magnet's CTRL PV, derived by replacing
the trailing ``BDES`` segment with ``CTRL``.

``derive_magnet_address`` is the shared BDES-segment rewrite every magnet hook needs.
"""

from __future__ import annotations

from typing import Any

from canopy.epics.pv_protocol import parse_pv_name
from canopy.squirrel.restore_hooks.base import RestoreHook, RestoreHookError

# Colon-separated first-segment markers identifying magnet PVs.
MAGNET_PREFIXES: frozenset[str] = frozenset({"BEND", "QUAD", "SEXT", "SOLN", "BTRM", "XCOR", "YCOR"})

TRIM_VALUE = "TRIM"
BDES_SEGMENT = "BDES"
CTRL_SEGMENT = "CTRL"


def derive_magnet_address(address: str, segment: str) -> str | None:
    """Rewrite a magnet BDES address, swapping its trailing segment for ``segment``.

    The original ``proto://`` prefix, if any, is carried over so downstream put
    routing is unchanged.

    Parameters
    ----------
    address : str
        The setpoint PV address being restored.
    segment : str
        The segment to write in place of the trailing ``BDES``.

    Returns
    -------
    str | None
        The rewritten address, or ``None`` when ``address`` is not a magnet PV.

    Raises
    ------
    RestoreHookError
        When ``address`` is a magnet PV whose trailing segment is not ``BDES``.
    """
    protocol, stripped = parse_pv_name(address)
    segments = stripped.split(":")
    if not segments or segments[0] not in MAGNET_PREFIXES:
        return None

    if segments[-1] != BDES_SEGMENT:
        raise RestoreHookError(
            f"Magnet PV {address} is not a {BDES_SEGMENT} setpoint "
            f"(expected a trailing :{BDES_SEGMENT}); cannot derive its {segment} PV"
        )

    segments[-1] = segment
    derived = ":".join(segments)
    if address.startswith(f"{protocol}://"):
        return f"{protocol}://{derived}"
    return derived


class MagnetTrimHook(RestoreHook):
    """Send ``TRIM`` to a magnet's CTRL PV after its BDES is written."""

    def derive_extra_writes(self, address: str, value: Any) -> list[tuple[str, Any]]:
        """Derive the CTRL put that makes a magnet act on its new BDES setpoint.

        Parameters
        ----------
        address : str
            The setpoint PV address being restored.
        value : Any
            The value the restore writes to ``address``. Unused: the CTRL PV always
            takes the literal ``TRIM`` command.

        Returns
        -------
        list[tuple[str, Any]]
            ``[(<CTRL address>, "TRIM")]`` for a magnet BDES setpoint, empty otherwise.

        Raises
        ------
        RestoreHookError
            When ``address`` is a magnet PV without a trailing ``BDES`` segment.
        """
        ctrl_addr = derive_magnet_address(address, CTRL_SEGMENT)
        if ctrl_addr is None:
            return []
        return [(ctrl_addr, TRIM_VALUE)]
