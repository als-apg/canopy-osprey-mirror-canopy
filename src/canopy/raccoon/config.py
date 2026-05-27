from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class CacheConfig(BaseModel):
    """Tunables for the in-process cache tier.

    Defaults follow `Module-Architecture-Raccoon.md` §5: pattern queries are
    short-lived, positive existence entries are long-lived, negative existence
    entries are short-lived to avoid pinning a transient miss.
    """

    pattern_ttl_s: float = 300.0
    exists_ttl_s: float = 1800.0
    negative_ttl_s: float = 60.0
    max_pattern_entries: int = 10000
    max_exists_entries: int = 100000
    batch_chunk_size: int = 1000


class DirectoryInstanceConfig(BaseModel):
    """The single configured directory backend.

    `type` is either a registered short name (e.g. `"meme"`) resolved by the
    registry, or a `module:Class` dotted path for an out-of-tree adapter that
    has been pip-installed alongside Canopy.
    """

    type: str
    timeout_s: float = 5.0
    extras: dict = {}
    cache: CacheConfig = CacheConfig()


class RaccoonConfig(BaseSettings):
    """Raccoon's configuration.

    Sourced from env vars with prefix `CANOPY_RACCOON__` and nested delimiter
    `__`. Examples::

        CANOPY_RACCOON__DIRECTORY__TYPE=meme
        CANOPY_RACCOON__DIRECTORY__TIMEOUT_S=5
        CANOPY_RACCOON__DIRECTORY__CACHE__EXISTS_TTL_S=1800
    """

    model_config = SettingsConfigDict(
        env_prefix="CANOPY_RACCOON__",
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    directory: DirectoryInstanceConfig | None = None


def get_raccoon_config() -> RaccoonConfig:
    return RaccoonConfig()
