"""The screen manifest of one server: both stores' listings, taken off the event loop and shared.

One :class:`Manifests` per (drafts directory, library directory) of the process
(:func:`open_manifests`) holds the personal store and the library store, so the
HTTP routes and the MCP tools read one listing cache and share one write lock. It
gives the event loop three things the stores do not:

- **Nothing blocks the loop.** A listing walks the tree, and on a first start it
  reads every screen, so the ``async`` calls run in a worker thread. Callers that
  ask while one is running share it instead of each walking the tree.
- **One encoded body per listing.** :meth:`Manifests.serve` answers the bytes of
  ``/drafts/manifest.json`` and their sha256, encoded again only when a store's
  listing or the library's ``navigation.json`` has changed. The ``tree`` view is
  the same list with only what a navigation tree shows.
- **A change is announced.** Whoever serves the manifest and finds another body
  than the last one tells the listeners (:meth:`Manifests.subscribe`), and
  :meth:`Manifests.watch` looks on its own at an interval while somebody
  listens, so a viewer can be told instead of asking.

A screen's listing that came from the listing cache file is read once more in the
background (:meth:`~canopy_mcp.store.DraftStore.verify_adopted`), so a start is
fast and the listed revs still end up being those of the bytes on disk.
"""

from __future__ import annotations

import json
import time
import asyncio
import hashlib
import logging
import threading
from typing import Any
from pathlib import Path
from functools import partial
from collections.abc import Callable, Hashable

from canopy_mcp.store import DraftStore, open_stores
from canopy_mcp.config import Settings
from canopy_mcp.navigation import NavigationFile

__all__ = ["FULL", "TREE", "VIEWS", "WATCH_S", "Manifests", "open_manifests", "reset_manifests"]

# The views of the manifest: every field, and what a navigation tree shows.
FULL = "full"
TREE = "tree"
VIEWS = (FULL, TREE)
# The fields of a ``tree`` entry. ``category`` stays: a draft and a library screen may share an id.
TREE_FIELDS = ("id", "category", "title")
# How often :meth:`Manifests.watch` looks at the disk while somebody listens.
WATCH_S = 2.0

logger = logging.getLogger(__name__)

Entries = tuple[dict[str, Any], ...]
Listener = Callable[[str], None]


class Manifests:
    """The personal store, the library store beside it (or ``None``) and the manifest served from both.

    ``max_age`` is how many seconds a listing is answered again without looking at
    the disk (:meth:`~canopy_mcp.store.DraftStore.listing`); 0 looks every time.
    The plain methods block on the disk and may be called from any thread; the
    ``async`` ones are for the event loop.
    """

    def __init__(self, store: DraftStore, library: DraftStore | None = None, *, max_age: float = 0.0):
        self.store = store
        self.library = library
        self.max_age = max_age
        self._stores = [store] if library is None else [store, library]
        # One encoding at a time; the stores take their own locks below it.
        self._lock = threading.Lock()
        self._generations: tuple[int, ...] | None = None
        self._screens: list[dict[str, Any]] = []
        # The library's own ordering of its screens, as last read and when.
        self._navigation_file = NavigationFile(library.root) if library is not None else None
        self._navigation: list[Any] | None = None
        self._navigation_at: float | None = None
        self._bodies: dict[str, tuple[bytes, str]] = {}
        # The sha256 of the full body as last encoded, and as last told to the listeners.
        self._full_rev: str | None = None
        self._announced: str | None = None
        self._listeners: list[Listener] = []
        self._flights: dict[Hashable, asyncio.Future] = {}
        self._verifier: threading.Thread | None = None
        self._verifier_lock = threading.Lock()
        self._stop = threading.Event()

    # --- blocking ---------------------------------------------------------------------

    def listings(self, *, descriptions: bool = False) -> tuple[Entries, Entries | None]:
        """The manifest entries of the personal store and of the library (``None`` without one).

        The entries are shared and must not be changed.
        """
        personal, _ = self.store.listing(descriptions=descriptions, max_age=self.max_age)
        library = None
        if self.library is not None:
            library, _ = self.library.listing(descriptions=descriptions, max_age=self.max_age)
        self._verify_in_background()
        return personal, library

    def body(self, view: str = FULL) -> tuple[bytes, str]:
        """The encoded manifest in ``view`` and the sha256 of those bytes.

        The full view is ``{"screens": [...]}``, the drafts and then the library's
        screens, each as its store lists it. The ``tree`` view holds the same
        screens in the same order with only :data:`TREE_FIELDS`. Both carry a
        library's valid ``navigation.json`` as ``navigation`` and leave the key out
        otherwise. A view is encoded when it is first asked for after a listing or
        the navigation changed, never otherwise.
        """
        if view not in VIEWS:
            raise ValueError(f"unknown manifest view: {view!r}")
        with self._lock:
            listed = [store.listing(max_age=self.max_age) for store in self._stores]
            generations = tuple(generation for _, generation in listed)
            if generations != self._generations:
                self._generations = generations
                self._screens = [entry for entries, _ in listed for entry in entries]
                self._bodies = {}
            navigation = self._read_navigation()
            if navigation is not self._navigation:
                if navigation != self._navigation:
                    self._bodies = {}
                self._navigation = navigation
            encoded = self._bodies.get(view)
            if encoded is None:
                screens = self._screens
                if view == TREE:
                    screens = [{field: entry[field] for field in TREE_FIELDS} for entry in screens]
                manifest: dict[str, Any] = {"screens": screens}
                if navigation is not None:
                    manifest["navigation"] = navigation
                data = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
                encoded = self._bodies[view] = (data, hashlib.sha256(data).hexdigest())
                if view == FULL:
                    self._full_rev = encoded[1]
        self._verify_in_background()
        return encoded

    def _read_navigation(self) -> list[Any] | None:
        """The library's navigation, looked at again once ``max_age`` seconds have passed (held under the lock)."""
        if self._navigation_file is None:
            return None
        now = time.monotonic()
        if self.max_age > 0 and self._navigation_at is not None and now - self._navigation_at < self.max_age:
            return self._navigation
        self._navigation_at = now
        return self._navigation_file.read()

    def _verify_in_background(self) -> None:
        """Start the one thread that reads the screens listed from a cache file, when there are any."""
        stores = [store for store in self._stores if store.unverified]
        if not stores or self._stop.is_set():
            return
        with self._verifier_lock:
            if self._verifier is not None and self._verifier.is_alive():
                return
            self._verifier = threading.Thread(
                target=self._verify, args=(stores,), name="canopy-manifest-verify", daemon=True
            )
            self._verifier.start()

    def _verify(self, stores: list[DraftStore]) -> None:
        for store in stores:
            try:
                differing = store.verify_adopted(self._stop)
            except Exception:  # a daemon thread: nothing else would report it
                logger.exception("manifest: checking the cached listing of %s failed", store.root)
                continue
            if differing:
                logger.warning(
                    "manifest: %d screens under %s changed without their size or time changing; they are listed again",
                    differing,
                    store.root,
                )

    def close(self) -> None:
        """Stop the background reading and wait for it."""
        self._stop.set()
        verifier = self._verifier
        if verifier is not None and verifier is not threading.current_thread():
            verifier.join(timeout=5.0)

    # --- on the event loop ------------------------------------------------------------

    async def _shared(self, name: str, call: Callable[[], Any]) -> Any:
        """``call()`` in a worker thread; callers that ask while it runs share that one run.

        A run is shared only with callers on the event loop that started it, and
        only while no write has gone through a store since it began: a caller who
        has just written must not be answered from a listing begun before that.
        """
        loop = asyncio.get_running_loop()
        key = (name, *(store.writes for store in self._stores))
        flight = self._flights.get(key)
        if flight is None or flight.get_loop() is not loop:
            flight = self._flights[key] = loop.run_in_executor(None, call)
            flight.add_done_callback(partial(self._landed, key))
        return await asyncio.shield(flight)

    def _landed(self, key: Hashable, flight: asyncio.Future) -> None:
        if self._flights.get(key) is flight:
            del self._flights[key]
        if not flight.cancelled():
            flight.exception()  # every waiter may have gone away; the failure is theirs, not the loop's

    async def read_listings(self, *, descriptions: bool = False) -> tuple[Entries, Entries | None]:
        """:meth:`listings`, off the event loop."""
        return await self._shared(f"listings:{descriptions}", partial(self.listings, descriptions=descriptions))

    async def serve(self, view: str = FULL) -> tuple[bytes, str]:
        """:meth:`body`, off the event loop; the listeners are told when the full manifest is another one."""
        if view not in VIEWS:
            raise ValueError(f"unknown manifest view: {view!r}")
        encoded = await self._shared(f"body:{view}", partial(self.body, view))
        self._announce()
        return encoded

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Call ``listener(rev)`` on the event loop whenever the full manifest's sha256 has changed.

        Nothing is said about the first manifest of a process: a change is a
        change from one that was served. Returns the call that unsubscribes.
        """
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _announce(self) -> None:
        rev = self._full_rev
        if rev is None or rev == self._announced:
            return
        first, self._announced = self._announced is None, rev
        if first:
            return
        for listener in list(self._listeners):
            try:
                listener(rev)
            except Exception:
                logger.exception("manifest: a listener failed")

    async def watch(self, wanted: Callable[[], bool], interval: float | None = None) -> None:
        """Until cancelled: every ``interval`` seconds (:data:`WATCH_S`), while ``wanted()``, bring the manifest up to date.

        That is one listing of each store (a walk and a stat per screen, in a
        worker thread) and, when it found a change, the announcement to the
        listeners. Nothing is looked at while nobody wants to know.
        """
        while True:
            await asyncio.sleep(WATCH_S if interval is None else interval)
            if not wanted():
                continue
            try:
                await self.serve(FULL)
            except Exception:
                logger.exception("manifest: the periodic listing failed")


# --- the process's manifests ------------------------------------------------------------

# One per (drafts directory, library directory), so every route and every tool call
# shares the personal store's write lock and both stores' listing caches.
_open: dict[tuple[Path, Path | None], Manifests] = {}


def open_manifests(settings: Settings) -> Manifests:
    """The process-wide :class:`Manifests` for ``settings``' drafts and library directories.

    The stores keep their listings between runs under ``settings.listing_cache_dir``.
    The first settings to name a pair of directories decide where; ``max_age``
    follows the latest.
    """
    library_dir = settings.library_dir
    key = (Path(settings.drafts_dir).resolve(), Path(library_dir).resolve() if library_dir is not None else None)
    manifests = _open.get(key)
    if manifests is None:
        store, library = open_stores(settings.drafts_dir, library_dir, cache_dir=settings.listing_cache_dir)
        manifests = _open[key] = Manifests(store, library)
    manifests.max_age = settings.manifest_max_age_s
    return manifests


def reset_manifests() -> None:
    """Forget every cached :class:`Manifests` and stop their background reading (tests switch directories)."""
    opened = list(_open.values())
    _open.clear()
    for manifests in opened:
        manifests.close()
