"""`meme` short-name resolves to MemeDirectoryAdapter via the registry."""

from canopy.raccoon.adapters.meme import MemeDirectoryAdapter
from canopy.raccoon.adapters.base import get_registered_type


def test_meme_short_name_registered_at_package_import() -> None:
    # canopy.raccoon.__init__ imports adapters.meme to trigger the decorator.
    cls = get_registered_type("meme")
    assert cls is MemeDirectoryAdapter
    assert MemeDirectoryAdapter.type_name == "meme"
