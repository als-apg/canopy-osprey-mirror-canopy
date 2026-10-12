"""Wire contracts shared by canopy's modules and its clients.

Besides the pydantic models in the submodules, the package carries
``wc6_vectors.json`` (package data): the WC6 table of write outcomes, keyed by
row id, from which canopy, the OSPREY adapter and the SDK map statuses, codes
and toast texts. It also carries the cases that define the write-detail cut,
which each side's cut is tested against.
"""

import json
from typing import Any
from importlib import resources

#: File name of the WC6 write-outcome table inside this package.
WC6_VECTORS_FILE = "wc6_vectors.json"


def load_wc6_vectors() -> dict[str, Any]:
    """Return the parsed WC6 vector file (read fresh on every call)."""
    text = (resources.files(__name__) / WC6_VECTORS_FILE).read_text(encoding="utf-8")
    return json.loads(text)
