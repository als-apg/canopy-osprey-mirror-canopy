"""SubscriptionRegistry: on-demand activation/deactivation semantics."""

from canopy.hare.services.subscriptions import SubscriptionRegistry


def test_first_subscriber_activates_pv():
    r = SubscriptionRegistry()
    r.add_client("c1")
    assert set(r.subscribe("c1", ["A", "B"])) == {"A", "B"}

    # A second client to an already-active PV does not re-activate it.
    r.add_client("c2")
    assert r.subscribe("c2", ["A"]) == []
    assert r.subscribers("A") == {"c1", "c2"}


def test_last_unsubscribe_deactivates_pv():
    r = SubscriptionRegistry()
    r.subscribe("c1", ["A"])
    r.subscribe("c2", ["A"])
    assert r.unsubscribe("c1", ["A"]) == []  # c2 still subscribed
    assert r.unsubscribe("c2", ["A"]) == ["A"]  # now deactivated


def test_remove_client_returns_only_fully_deactivated():
    r = SubscriptionRegistry()
    r.subscribe("c1", ["A", "B"])
    r.subscribe("c2", ["B"])
    assert set(r.remove_client("c1")) == {"A"}  # B still has c2
    assert r.subscribers("B") == {"c2"}
    assert r.client_pvs("c1") == set()


def test_stats():
    r = SubscriptionRegistry()
    r.subscribe("c1", ["A", "B"])
    r.subscribe("c2", ["B"])
    stats = r.stats()
    assert stats["clients"] == 2
    assert stats["active_pvs"] == 2
    assert stats["subscriptions"] == 3
