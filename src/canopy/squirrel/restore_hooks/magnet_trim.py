"""Restore hook: trim magnet PVs after their BDES setpoint is written.

A magnet does not physically move when its BDES setpoint is updated — we must
additionally put the string ``TRIM`` to the magnet's CTRL PV, derived by replacing
the trailing ``BDES`` segment with ``CTRL``.
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


class MagnetTrimHook(RestoreHook):
    """Send ``TRIM`` to a magnet's CTRL PV after its BDES is written."""

    def derive_extra_writes(self, address: str) -> list[tuple[str, Any]]:
        protocol, stripped = parse_pv_name(address)
        segments = stripped.split(":")
        if not segments or segments[0] not in MAGNET_PREFIXES:
            return []

        if segments[-1] != BDES_SEGMENT:
            raise RestoreHookError(
                f"Magnet PV {address} is not a {BDES_SEGMENT} setpoint "
                f"(expected a trailing :{BDES_SEGMENT}); cannot derive its {CTRL_SEGMENT} PV"
            )

        segments[-1] = CTRL_SEGMENT
        ctrl_stripped = ":".join(segments)

        # Preserve the original protocol prefix so downstream put routing is unchanged.
        if address.startswith(f"{protocol}://"):
            ctrl_addr = f"{protocol}://{ctrl_stripped}"
        else:
            ctrl_addr = ctrl_stripped

        return [(ctrl_addr, TRIM_VALUE)]
