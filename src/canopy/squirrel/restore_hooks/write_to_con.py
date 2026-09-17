"""Restore hook: mirror a magnet's restored BDES value onto its BCON PV.

Magnets are routinely tuned by hand after a restore, and operators need to see what
the last restored value was before that tuning started. BCON PVs are never captured
in a snapshot and are otherwise untouched, so they make a natural record of it.
"""

from __future__ import annotations

from typing import Any

from canopy.squirrel.restore_hooks.base import RestoreHook
from canopy.squirrel.restore_hooks.magnet_trim import derive_magnet_address

BCON_SEGMENT = "BCON"


class WriteToConHook(RestoreHook):
    """Write a magnet's restored BDES value to its BCON PV."""

    def derive_extra_writes(self, address: str, value: Any) -> list[tuple[str, Any]]:
        """Derive the BCON put mirroring the value restored to a magnet's BDES setpoint.

        Parameters
        ----------
        address : str
            The setpoint PV address being restored.
        value : Any
            The value the restore writes to ``address``; mirrored verbatim to BCON.

        Returns
        -------
        list[tuple[str, Any]]
            ``[(<BCON address>, value)]`` for a magnet BDES setpoint, empty otherwise.

        Raises
        ------
        RestoreHookError
            When ``address`` is a magnet PV without a trailing ``BDES`` segment.
        """
        con_addr = derive_magnet_address(address, BCON_SEGMENT)
        if con_addr is None:
            return []
        return [(con_addr, value)]
