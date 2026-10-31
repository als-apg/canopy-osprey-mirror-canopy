"""Wire contracts shared by canopy's modules and its clients.

Besides the pydantic models in the submodules, the package carries
``wc6_vectors.json`` (package data): the table of PV write outcomes, keyed by row
id, from which a client maps an answer to a PUT (or the lack of one) to its
status, code and toast text. It also carries the cases that define how a long
write detail is cut. canopy is the source of the file; canopy-sdk-js vendors a
byte copy and tests its write-outcome mapping against it.
"""

import json
from typing import Any
from importlib import resources

#: File name of the write-outcome table inside this package.
WC6_VECTORS_FILE = "wc6_vectors.json"


def load_wc6_vectors() -> dict[str, Any]:
    """Return the parsed write-outcome table (read fresh on every call)."""
    text = (resources.files(__name__) / WC6_VECTORS_FILE).read_text(encoding="utf-8")
    return json.loads(text)
