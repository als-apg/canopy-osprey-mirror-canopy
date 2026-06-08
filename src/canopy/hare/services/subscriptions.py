"""In-memory WebSocket subscription registry (single-replica).

Tracks client<->PV subscriptions and the per-PV subscriber count that drives
on-demand monitoring: the broker starts an EPICS monitor when a PV gains its
first subscriber and stops it when the last one leaves.
"""

from collections import defaultdict


class SubscriptionRegistry:
    def __init__(self) -> None:
        self._client_pvs: dict[str, set[str]] = defaultdict(set)
        self._pv_clients: dict[str, set[str]] = defaultdict(set)

    def add_client(self, client_id: str) -> None:
        self._client_pvs.setdefault(client_id, set())

    def remove_client(self, client_id: str) -> list[str]:
        """Drop a client and all its subscriptions.

        Returns the PVs that now have zero subscribers (the broker stops
        monitoring these).
        """
        pvs = self._client_pvs.pop(client_id, set())
        deactivated: list[str] = []
        for pv in pvs:
            clients = self._pv_clients.get(pv)
            if not clients:
                continue
            clients.discard(client_id)
            if not clients:
                self._pv_clients.pop(pv, None)
                deactivated.append(pv)
        return deactivated

    def subscribe(self, client_id: str, pvs: list[str]) -> list[str]:
        """Subscribe a client to PVs. Returns PVs newly activated (0->1)."""
        self._client_pvs.setdefault(client_id, set())
        newly: list[str] = []
        for pv in pvs:
            clients = self._pv_clients[pv]
            if not clients:
                newly.append(pv)
            clients.add(client_id)
            self._client_pvs[client_id].add(pv)
        return newly

    def unsubscribe(self, client_id: str, pvs: list[str]) -> list[str]:
        """Unsubscribe a client from PVs. Returns PVs newly deactivated (1->0)."""
        deactivated: list[str] = []
        client_set = self._client_pvs.get(client_id)
        for pv in pvs:
            if client_set is not None:
                client_set.discard(pv)
            clients = self._pv_clients.get(pv)
            if not clients:
                continue
            clients.discard(client_id)
            if not clients:
                self._pv_clients.pop(pv, None)
                deactivated.append(pv)
        return deactivated

    def subscribers(self, pv: str) -> set[str]:
        return set(self._pv_clients.get(pv, ()))

    def client_pvs(self, client_id: str) -> set[str]:
        return set(self._client_pvs.get(client_id, ()))

    def active_pvs(self) -> list[str]:
        return list(self._pv_clients.keys())

    def stats(self) -> dict:
        return {
            "clients": len(self._client_pvs),
            "active_pvs": len(self._pv_clients),
            "subscriptions": sum(len(pvs) for pvs in self._client_pvs.values()),
        }
