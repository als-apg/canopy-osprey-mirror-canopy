# Custom directory adapter

Raccoon supports directory services other than SLAC's `meme.names`. Other labs (or test environments) implement the `DirectoryAdapter` Protocol and load it via Raccoon's config; no fork of Canopy required.

Note the division of labor: adapters only answer the three *list* calls against the upstream. Everything else — `exists`, batch `validate`, caching, single-flight de-duplication, and per-call timeouts — lives in Raccoon's service layer above the adapter, so you get all of it for free.

## 1. Implement the Protocol

The contract lives in `canopy.raccoon.adapters.base.DirectoryAdapter`. Implement these six methods:

- `from_config(cfg) -> "YourAdapter"`: classmethod that builds an instance from validated config. Adapter-specific knobs arrive in `cfg.extras` (a plain dict).
- `list_pvs(pattern, *, tag, sort_by, element_type) -> list[str]`: PV names matching an upstream-native pattern.
- `list_devices(pattern, *, tag, sort_by, element_type) -> list[str]`: device names.
- `list_elements(pattern, *, tag, sort_by, element_type) -> list[str]`: element names.
- `health() -> AdapterHealth`: cheap reachability probe.
- `close()`: release any underlying client or worker pool. Called from app lifespan shutdown.

Plus two attributes:

- `name: str`: instance label used in logs and `/health/summary`.
- `type_name: str`: class attribute identifying the adapter type.

Pattern semantics are defined by *your* upstream (Oracle wildcards, regex, globs — Raccoon passes patterns through verbatim). `tag`, `sort_by`, and `element_type` are likewise opaque; reject values your upstream does not understand with `InvalidQuery`.

If your upstream client is synchronous, dispatch it to a thread pool you own rather than `asyncio.to_thread` — a timed-out call's thread keeps running until the upstream returns, and a bounded adapter-owned pool keeps a hung directory service from starving the process-wide executor. `MemeDirectoryAdapter` is the reference for this pattern.

### Errors

Raise the typed domain exceptions from `canopy.raccoon.adapters.base`:

| Exception              | HTTP status | When                                                      |
| ---------------------- | ----------- | --------------------------------------------------------- |
| `DirectoryUnavailable` | 503         | Upstream reachable but errored, or unreachable.            |
| `DirectoryTimeout`     | 504         | Subclass of `DirectoryUnavailable`; your client timed out. |
| `InvalidQuery`         | 400         | Upstream rejected the pattern or a filter value.           |
| `AdapterMisconfigured` | 500         | Missing dependency or invalid adapter config.              |

The Raccoon exception handler maps each to the right HTTP status with the right slug in the `{"code", "detail"}` envelope.

## 2. Register the type (decorator path)

If your adapter is part of a Python package that's imported, decorate it:

```python
from canopy.raccoon.adapters.base import register_directory_type

@register_directory_type("my_directory")
class MyAdapter:
    ...
```

Then config can reference it by short name:

```bash
CANOPY_RACCOON__DIRECTORY__TYPE=my_directory
```

## 3. Load via dotted path (no registration needed)

If your adapter lives in an external package and you don't want to wire decorator registration, reference it via the `module:Class` form:

```bash
CANOPY_RACCOON__DIRECTORY__TYPE=labxyz.canopy_plugin:LabXYZAdapter
CANOPY_RACCOON__DIRECTORY__TIMEOUT_S=5
CANOPY_RACCOON__DIRECTORY__EXTRAS__MY_KNOB=value
```

Raccoon imports the module and instantiates the class via `from_config(cfg)` at startup.

## 4. Verify against the contract suite

Raccoon ships reusable conformance assertions at `tests.canopy.raccoon.adapters.contract`. From your test file:

```python
import pytest
from tests.canopy.raccoon.adapters import contract

from your_package.your_adapter import YourAdapter


@pytest.fixture
async def adapter():
    a = YourAdapter.from_config(YourConfig(...))
    yield a
    await a.close()


async def test_identity(adapter):
    await contract.assert_identity_attributes(adapter)


async def test_health(adapter):
    await contract.assert_health(adapter)


async def test_list_pvs(adapter):
    await contract.assert_list_pvs_invariants(adapter, pattern="PV:KNOWN%")


async def test_close_idempotent(adapter):
    await contract.assert_close_idempotent(adapter)
```

The assertions cover the identity attributes, health DTO shape, name-list shape for all three list calls, and close idempotency.

## 5. Working example

The Raccoon test suite includes a complete example at `tests/_fixtures/raccoon_example_adapter.py` and an end-to-end test at `tests/canopy/raccoon/adapters/test_dotted_path_loading.py` that loads the example via dotted-path config, serves real gateway calls through it, and runs it through the contract suite. Use it as a template.
