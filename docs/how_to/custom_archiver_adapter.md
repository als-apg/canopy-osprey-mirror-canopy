# Custom archiver adapter

Mole supports archivers other than the EPICS Archiver Appliance. Other labs (or test environments) implement the `ArchiverAdapter` Protocol and load it via Mole's config; no fork of Canopy required.

## 1. Implement the Protocol

The contract lives in `canopy.mole.adapters.base.ArchiverAdapter`. Implement these seven methods:

- `from_config(name, cfg) -> "YourAdapter"`: classmethod that builds an instance from validated config.
- `fetch_window(pv, t0_ns, t1_ns, *, max_points=None) -> list[Sample]`: raw samples in the window.
- `fetch_binned(pv, t0_ns, t1_ns, *, binning, target_buckets, bin_size_ns) -> list[BinnedPoint]`: statistical buckets in `auto` or `time` mode.
- `metadata(pv) -> PVMetadata`: per-process variable metadata your archiver carries.
- `search(pattern, *, limit) -> list[str]`: pattern-based process variable name search.
- `health() -> AdapterHealth`: reachability probe (≤2 s).
- `close()`: drain any client connections.

Plus two attributes:

- `name: str`: set from the config key (in `from_config`).
- `type_name: str`: class attribute identifying the adapter type.

### Severity normalization

If your archiver returns severity as integers, use `canopy.mole.adapters.base.normalize_severity`. If it returns strings, map directly to `canopy.contracts.sample.AlarmSeverity`.

### Errors

Raise the typed domain exceptions from `canopy.mole.adapters.base`:

| Exception                 | HTTP status | When                                                    |
| ------------------------- | ----------- | ------------------------------------------------------- |
| `PVNotFoundInArchiver`    | 404         | The requested PV is not indexed by this archiver.        |
| `ArchiverUnavailable`     | 503         | Archiver reachable but errored, or unreachable.          |
| `ArchiverTimeout`         | 504         | Subclass of `ArchiverUnavailable`; your client timed out.|
| `ArchiverBadResponse`     | 502         | Archiver returned malformed / unparseable response.      |
| `BinningNotSupported`     | 501         | Your archiver cannot honor the requested binning mode.   |

The Mole exception handler maps each to the right HTTP status with the right slug in the `{"code", "detail"}` envelope.

## 2. Register the type (decorator path)

If your adapter is part of a Python package that's imported, decorate it:

```python
from canopy.mole.adapters.base import register_archiver_type

@register_archiver_type("my_archiver")
class MyAdapter:
    ...
```

Then config can reference it by short name:

```toml
[mole.archivers.lab]
type = "my_archiver"
url = "https://archiver.lab.example/"
```

## 3. Load via dotted path (no registration needed)

If your adapter lives in an external package and you don't want to wire decorator registration, reference it via the `module:Class` form in config:

```toml
[mole.archivers.lab]
type = "labxyz.canopy_plugin:LabXYZAdapter"
url = "https://archiver.lab.example/"
```

Mole imports the module and instantiates the class via `from_config(name, cfg)` at startup.

## 4. Verify against the contract suite

Mole ships a parameterized contract suite at `tests.canopy.mole.adapters.contract`. From your test file:

```python
import pytest
from tests.canopy.mole.adapters import contract

from your_package.your_adapter import YourAdapter


@pytest.fixture
async def adapter():
    a = YourAdapter.from_config("test", YourConfig(...))
    yield a
    await a.close()


async def test_health(adapter):
    await contract.assert_health(adapter)


async def test_fetch_window_invariants(adapter):
    await contract.assert_fetch_window_invariants(
        adapter, pv="PV:KNOWN", t0_ns=0, t1_ns=10_000_000_000
    )


async def test_fetch_binned_auto(adapter):
    await contract.assert_fetch_binned_auto(
        adapter, pv="PV:KNOWN", t0_ns=0, t1_ns=10_000_000_000, target_buckets=5
    )
```

The assertions cover ordering, severity normalization, the `source="archive"` tag, and bucket-shape invariants.

## 5. Working example

The Mole test suite includes a complete example at `tests/_fixtures/example_adapter.py` and an end-to-end test at `tests/canopy/mole/adapters/test_dotted_path_loading.py` that loads the example via dotted-path config and runs it through the contract suite. Use it as a template.
