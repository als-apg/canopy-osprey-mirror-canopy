"""Module-level runtime state for Hare.

Kept separate from ``app.py`` so the API layer can read uptime without an import
cycle through the FastAPI app. Thin wrapper around the shared
``canopy.core.runtime.ModuleRuntime`` so each module still owns a distinct
instance behind its own import path.
"""

from canopy.core.runtime import ModuleRuntime

_runtime = ModuleRuntime()
mark_started = _runtime.mark_started
uptime_s = _runtime.uptime_s
