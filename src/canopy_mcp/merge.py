"""RFC 7396 JSON Merge Patch.

``patch_node`` applies the agent's ``set`` to a widget node with these rules:
an object in the patch merges key by key into the target, ``null`` deletes a
key, and anything else -- a scalar or an array -- replaces the target value
outright. Arrays are never merged element-wise, which is why ``rules`` and
``warnings`` given in a patch replace the node's list.
"""

from __future__ import annotations

import copy
from typing import Any


def merge_patch(target: Any, patch: Any) -> Any:
    """Return ``target`` with ``patch`` applied per RFC 7396; neither argument is modified."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result
