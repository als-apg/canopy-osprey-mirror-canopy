"""Lint: checks that need the widget catalog, Fox or the PV directory.

Four passes, each tolerant of malformed input (the schema pass reports it):

* **props** — every node's ``props`` validated (draft-07) against its catalog
  entry's Beaver ``propSchema``. A type the catalog does not know is error
  ``unknown_type``; a verbatim type Beaver does not document is warning
  ``undocumented_type``; each violation is error ``invalid_prop`` whose path
  ends at the offending prop name. ``unknown-widget`` placeholders are skipped:
  the semantic pass reports them as ``unknown_widget``.
* **formulas** — every ``formulas[]`` entry parsed in-process by Fox
  (:mod:`canopy_mcp.fox`); a parse failure is error ``invalid_formula``.
* **pvs** — PV names asked of Raccoon (``POST /raccoon/v1/pvs/validate``). Names
  with macros (``${``) or ``loc://``/``fox://`` are not machine PVs and are
  dropped; ``ca://``/``pva://`` are stripped; names containing any of
  ``% * ? ( | [`` are warning ``pv_not_checkable`` and never sent (one such name
  makes Raccoon reject the whole batch). A name Raccoon lists in ``unknown`` is
  warning ``unknown_pv`` on each node that uses it. If Raccoon cannot answer
  (non-2xx, connection error, timeout) the pass reports
  ``{check: 'raccoon', reason}`` in ``skipped`` and no per-node PV finding —
  never a pass that was not verified.
* **roles** — every colour role (``{"$role", "fallback"}``) in ``props`` (nested
  objects and arrays included), rule condition values and rule defaults checked
  against the roles of the served flat scheme. A role the scheme does not define
  is warning ``unknown_role`` (never an error: roles are open, and the fallback
  paints). When the scheme cannot be flattened the pass is skipped.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from jsonschema import Draft7Validator

from canopy.core.color_scheme import get_color_scheme
from canopy_mcp import fox
from canopy_mcp.catalog import Catalog
from canopy_mcp.check.findings import Finding, node_id, pointer
from canopy_mcp.check.semantic import UNKNOWN_WIDGET_TYPE, iter_nodes
from canopy_mcp.client import error_reason, borrowed_client
from canopy_mcp.config import Settings

RACCOON_VALIDATE_PATH = "/raccoon/v1/pvs/validate"
RACCOON_TIMEOUT_S = 1.5
RACCOON_BATCH = 10_000

_DROP_CONTAINS = ("${",)
_DROP_PREFIXES = ("loc://", "fox://")
_STRIP_PREFIXES = ("ca://", "pva://")
_NOT_CHECKABLE_CHARS = frozenset("%*?(|[")

#: Props that name a PV directly.
_PV_PROPS = ("pv", "readbackPV")
#: Keys inside ``props.curves[]`` items that name a PV.
_CURVE_PV_KEYS = ("y_channel", "x_channel")
#: Key inside ``props.curves[]`` items that names a list of PVs (``pv-array-plot``).
_CURVE_PV_LIST_KEY = "y_channels"

Path = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class PvRef:
    """One PV name as written, and where."""

    name: str
    node_id: str | None
    path: Path


@dataclass
class LintResult:
    findings: list[Finding] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)


async def check_lint(
    doc: Any,
    catalog: Catalog,
    *,
    client: httpx.AsyncClient | None = None,
    settings: Settings | None = None,
    roles: Collection[str] | None = None,
) -> LintResult:
    """All lint findings for ``doc``, plus any check that could not run.

    ``roles`` are the colour roles the viewer defines; by default the served
    scheme's (``canopy.core.color_scheme.get_color_scheme``).
    """
    if not isinstance(doc, dict):
        return LintResult()
    if roles is None:
        roles = served_roles()
    result = LintResult()
    result.findings += prop_findings(doc, catalog)
    result.findings += role_findings(doc, roles)
    result.findings += await formula_findings(doc)
    pv_found, skipped = await pv_findings(collect_pvs(doc), client=client, settings=settings)
    result.findings += pv_found
    result.skipped += skipped
    return result


# --- props -----------------------------------------------------------------

_validators: dict[int, tuple[dict[str, Any], Draft7Validator]] = {}


def _validator(schema: dict[str, Any]) -> Draft7Validator:
    cached = _validators.get(id(schema))
    if cached is not None and cached[0] is schema:
        return cached[1]
    validator = Draft7Validator(schema)
    _validators[id(schema)] = (schema, validator)
    return validator


def prop_findings(doc: Any, catalog: Catalog) -> list[Finding]:
    """Catalog-driven type and prop findings, in document order.

    Colour props carry the registry's inline colour def (a string or a
    ``{"$role", "fallback"}`` role), so a role-bearing draft validates here; a
    malformed role is an ``invalid_prop`` on the colour prop.
    """
    out: list[Finding] = []
    for path, node in iter_nodes(doc):
        ntype = node.get("type")
        if not isinstance(ntype, str) or ntype == UNKNOWN_WIDGET_TYPE:
            continue
        nid = node_id(node)
        entry = catalog.get(ntype)
        if entry is None:
            out.append(
                Finding(nid, "unknown_type", pointer((*path, "type")), "error", f"widget type '{ntype}' is not in the catalog")
            )
            continue
        if entry.placeholder:
            continue
        if entry.undocumented:
            out.append(
                Finding(
                    nid,
                    "undocumented_type",
                    pointer((*path, "type")),
                    "warning",
                    f"widget type '{ntype}' renders verbatim but Beaver documents no props for it",
                )
            )
        if entry.prop_schema is None:
            continue
        props = node.get("props", {})
        if not isinstance(props, dict):
            continue
        errors = sorted(_validator(entry.prop_schema).iter_errors(props), key=lambda e: list(map(str, e.absolute_path)))
        for error in errors:
            out.append(
                Finding(
                    nid,
                    "invalid_prop",
                    pointer((*path, "props", *_prop_parts(error))),
                    "error",
                    error.message,
                )
            )
    return out


def _prop_parts(error: Any) -> tuple[str | int, ...]:
    """Path under ``props`` for a violation; a missing required prop is named."""
    parts = tuple(error.absolute_path)
    if parts:
        return parts[:1]
    if error.validator == "required" and isinstance(error.instance, dict):
        missing = [name for name in error.validator_value if name not in error.instance]
        if missing:
            return (missing[0],)
    return ()


# --- roles -----------------------------------------------------------------


def served_roles() -> list[str] | None:
    """Role names of the served flat scheme, or ``None`` when it is unavailable."""
    flat = get_color_scheme().flat_json
    roles = flat.get("roles") if isinstance(flat, dict) else None
    return [r for r in roles if isinstance(r, str)] if isinstance(roles, list) else None


def _is_role(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("$role"), str)


def _iter_roles(value: Any, path: Path) -> Iterator[tuple[str, Path]]:
    """Every colour role inside ``value`` (itself included), depth-first."""
    if _is_role(value):
        yield value["$role"], path
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_roles(item, (*path, key))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _iter_roles(item, (*path, i))


def role_findings(doc: Any, roles: Collection[str] | None) -> list[Finding]:
    """``unknown_role`` warnings in document order; none when ``roles`` is ``None``."""
    if roles is None or not isinstance(doc, dict):
        return []
    known = frozenset(roles)
    out: list[Finding] = []
    for path, node in iter_nodes(doc):
        nid = node_id(node)
        refs: list[tuple[str, Path]] = []
        props = node.get("props")
        if isinstance(props, dict):
            refs += _iter_roles(props, (*path, "props"))
        rules = node.get("rules")
        for r, rule in enumerate(rules if isinstance(rules, list) else []):
            if not isinstance(rule, dict):
                continue
            conditions = rule.get("conditions")
            for c, condition in enumerate(conditions if isinstance(conditions, list) else []):
                if isinstance(condition, dict) and _is_role(condition.get("value")):
                    refs.append((condition["value"]["$role"], (*path, "rules", r, "conditions", c, "value")))
            if _is_role(rule.get("default")):
                refs.append((rule["default"]["$role"], (*path, "rules", r, "default")))
        for name, role_path in refs:
            if name not in known:
                out.append(
                    Finding(
                        nid,
                        "unknown_role",
                        pointer(role_path),
                        "warning",
                        f"color role '{name}' on '{nid}' is not in the color scheme; its fallback will paint",
                    )
                )
    return out


# --- formulas --------------------------------------------------------------


async def formula_findings(doc: Any) -> list[Finding]:
    """Fox parse errors for each declared formula."""
    formulas = doc.get("formulas") if isinstance(doc, dict) else None
    out: list[Finding] = []
    for i, formula in enumerate(formulas if isinstance(formulas, list) else []):
        if not isinstance(formula, dict) or not isinstance(formula.get("expression"), str):
            continue
        bindings = formula.get("bindings")
        if not isinstance(bindings, dict) or not all(isinstance(v, str) for v in bindings.values()):
            bindings = {}
        errors = await fox.formula_errors(formula["expression"], bindings, formula.get("kind"))
        if errors:
            out.append(
                Finding(
                    None,
                    "invalid_formula",
                    pointer(("formulas", i, "expression")),
                    "error",
                    f"formula '{formula.get('name')}': {'; '.join(errors)}",
                )
            )
    return out


# --- pvs -------------------------------------------------------------------


def collect_pvs(doc: Any) -> list[PvRef]:
    """Every PV name the document uses, as written, with its node and path."""
    return list(_iter_pvs(doc)) if isinstance(doc, dict) else []


def _iter_pvs(doc: dict[str, Any]) -> Iterator[PvRef]:
    formulas = doc.get("formulas")
    for i, formula in enumerate(formulas if isinstance(formulas, list) else []):
        bindings = formula.get("bindings") if isinstance(formula, dict) else None
        if isinstance(bindings, dict):
            for var, pv in bindings.items():
                if isinstance(pv, str):
                    yield PvRef(pv, None, ("formulas", i, "bindings", var))
    for path, node in iter_nodes(doc):
        nid = node_id(node)
        props = node.get("props")
        if isinstance(props, dict):
            for key in _PV_PROPS:
                if isinstance(props.get(key), str):
                    yield PvRef(props[key], nid, (*path, "props", key))
            curves = props.get("curves")
            for c, curve in enumerate(curves if isinstance(curves, list) else []):
                if isinstance(curve, dict):
                    for key in _CURVE_PV_KEYS:
                        if isinstance(curve.get(key), str):
                            yield PvRef(curve[key], nid, (*path, "props", "curves", c, key))
                    channels = curve.get(_CURVE_PV_LIST_KEY)
                    for k, channel in enumerate(channels if isinstance(channels, list) else []):
                        if isinstance(channel, str):
                            yield PvRef(channel, nid, (*path, "props", "curves", c, _CURVE_PV_LIST_KEY, k))
            actions = props.get("actions")
            for a, action in enumerate(actions if isinstance(actions, list) else []):
                if isinstance(action, dict) and action.get("type") == "write_pv" and isinstance(action.get("pvName"), str):
                    yield PvRef(action["pvName"], nid, (*path, "props", "actions", a, "pvName"))
        rules = node.get("rules")
        for r, rule in enumerate(rules if isinstance(rules, list) else []):
            pvs = rule.get("pvs") if isinstance(rule, dict) else None
            for p, pv in enumerate(pvs if isinstance(pvs, list) else []):
                name = pv.get("name") if isinstance(pv, dict) else None
                if isinstance(name, str):
                    yield PvRef(name, nid, (*path, "rules", r, "pvs", p, "name"))


def normalize_pv(name: str) -> str | None:
    """The name Raccoon should see, or ``None`` when it is not a machine PV."""
    name = name.strip()
    if not name or any(s in name for s in _DROP_CONTAINS) or name.startswith(_DROP_PREFIXES):
        return None
    for prefix in _STRIP_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return name or None


def is_checkable(name: str) -> bool:
    return not any(ch in _NOT_CHECKABLE_CHARS for ch in name)


async def pv_findings(
    refs: list[PvRef],
    *,
    client: httpx.AsyncClient | None = None,
    settings: Settings | None = None,
) -> tuple[list[Finding], list[dict[str, str]]]:
    """``pv_not_checkable`` and ``unknown_pv`` findings, and a ``skipped`` list."""
    out: list[Finding] = []
    by_name: dict[str, list[PvRef]] = {}
    for ref in refs:
        name = normalize_pv(ref.name)
        if name is None:
            continue
        if not is_checkable(name):
            out.append(
                Finding(
                    ref.node_id,
                    "pv_not_checkable",
                    pointer(ref.path),
                    "warning",
                    f"PV name {ref.name!r} contains a pattern character; the directory cannot check it",
                )
            )
            continue
        by_name.setdefault(name, []).append(ref)
    if not by_name:
        return out, []

    unknown, reason = await _ask_raccoon(list(by_name), client=client, settings=settings)
    if reason is not None:
        return out, [{"check": "raccoon", "reason": reason}]
    for name in unknown:
        for ref in by_name.get(name, []):
            out.append(
                Finding(
                    ref.node_id,
                    "unknown_pv",
                    pointer(ref.path),
                    "warning",
                    f"PV '{name}' is not in the PV directory",
                )
            )
    return out, []


async def _ask_raccoon(
    names: list[str],
    *,
    client: httpx.AsyncClient | None,
    settings: Settings | None,
) -> tuple[list[str], str | None]:
    """Unknown names in order, or a skip reason when any batch fails."""
    unknown: list[str] = []
    async with borrowed_client(client, settings, timeout=RACCOON_TIMEOUT_S) as http:
        for start in range(0, len(names), RACCOON_BATCH):
            batch = names[start : start + RACCOON_BATCH]
            try:
                response = await http.post(RACCOON_VALIDATE_PATH, json={"pvs": batch}, timeout=RACCOON_TIMEOUT_S)
            except httpx.TimeoutException:
                return [], "timeout"
            except httpx.HTTPError as exc:
                return [], type(exc).__name__
            if not response.is_success:
                return [], error_reason(response)
            try:
                body = response.json()
                batch_unknown = body["unknown"]
                if not isinstance(batch_unknown, list):
                    raise TypeError("unknown is not a list")
            except Exception:
                return [], "bad_response"
            unknown += [n for n in batch_unknown if isinstance(n, str)]
    return unknown, None

