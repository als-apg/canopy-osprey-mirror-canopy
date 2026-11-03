"""Wire contracts shared by canopy's modules and its clients.

Besides the pydantic models in the submodules, the package carries
``write_outcome_vectors.json`` (package data): the table of PV write outcomes,
keyed by row id, from which a client maps an answer to a PUT (or the lack of
one) to its status, code and toast text. It also carries the cases that define how a long
write detail is cut. canopy is the source of the file; canopy-sdk-js vendors a
byte copy and tests its write-outcome mapping against it.

It also carries ``heartbeat_vectors.json``: the machine statement a heartbeat
carries (``reachable`` and ``writes``), the ``since`` phrase, and frame sequences
with the badge and toasts the viewer shows for them. canopy-sdk-js vendors a
byte copy of it too.
"""

import json
from typing import Any
from importlib import resources

#: File name of the write-outcome table inside this package.
WRITE_OUTCOME_VECTORS_FILE = "write_outcome_vectors.json"


def load_write_outcome_vectors() -> dict[str, Any]:
    """Return the parsed write-outcome table (read fresh on every call)."""
    text = (resources.files(__name__) / WRITE_OUTCOME_VECTORS_FILE).read_text(encoding="utf-8")
    return json.loads(text)


#: File name of the machine-statement vectors inside this package.
HEARTBEAT_VECTORS_FILE = "heartbeat_vectors.json"


def load_heartbeat_vectors() -> dict[str, Any]:
    """Return the parsed machine-statement vectors (read fresh on every call)."""
    text = (resources.files(__name__) / HEARTBEAT_VECTORS_FILE).read_text(encoding="utf-8")
    return json.loads(text)
