"""The ``check`` tool: every check pass over one Screen-IR document.

A document is checked either as given (``screen``) or as stored (``draft_id``
alone). A given document may also name a ``draft_id``: its refs then resolve in
that draft's directory, as they will once it is saved there, and the stored
copy is not read. The result groups findings by pass:

* ``schema`` -- the staged Screen-IR JSON Schema (:mod:`canopy_mcp.check.schema`);
  without one, a single ``spec_unavailable`` warning and no prop lint;
* ``semantic`` -- the semantic pass plus draft-ref and image-ref resolution
  (:mod:`canopy_mcp.check.semantic`, :mod:`canopy_mcp.check.refs`);
* ``lint`` -- catalog props, in-process Fox formulas and Raccoon PV lookup
  (:mod:`canopy_mcp.check.lint`);
* ``skipped`` -- checks that could not run, e.g. ``{check: 'raccoon', reason}``.

Every finding carries ``nodeId``, ``code``, ``path`` (and ``docId`` for a node
of an embedded document). Write tools call :func:`run_check` directly and gate
on the error-severity findings it returns.
"""

from __future__ import annotations

from typing import Any
from dataclasses import field, dataclass

import httpx
from fastmcp.exceptions import ToolError

from canopy_mcp.store import DraftStore
from canopy_mcp.config import Settings, get_settings
from canopy_mcp.server import mcp
from canopy_mcp.static import configured_tools
from canopy_mcp.catalog import Catalog, CatalogError, ensure_catalog
from canopy_mcp.check.lint import check_lint
from canopy_mcp.check.refs import Tree, ServedAt, check_refs, read_screen
from canopy_mcp.check.schema import SPEC_UNAVAILABLE, check_schema
from canopy_mcp.tools._common import route, get_store, read_draft, validate_id
from canopy_mcp.check.findings import Finding
from canopy_mcp.check.semantic import check_semantic


@dataclass
class CheckResult:
    """Findings per pass, and the checks that could not run."""

    schema: list[Finding] = field(default_factory=list)
    semantic: list[Finding] = field(default_factory=list)
    lint: list[Finding] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    @property
    def findings(self) -> list[Finding]:
        """Every finding, schema then semantic then lint."""
        return [*self.schema, *self.semantic, *self.lint]

    @property
    def errors(self) -> list[Finding]:
        """The error-severity findings (what a write refuses on)."""
        return [f for f in self.findings if f.severity == "error"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": [f.to_dict() for f in self.schema],
            "semantic": [f.to_dict() for f in self.semantic],
            "lint": [f.to_dict() for f in self.lint],
            "skipped": [dict(s) for s in self.skipped],
        }


async def run_check(
    screen: Any = None,
    draft_id: str | None = None,
    *,
    store: DraftStore | None = None,
    tree: Tree | None = None,
    served_at: ServedAt | None = None,
    catalog: Catalog | None = None,
    client: httpx.AsyncClient | None = None,
    settings: Settings | None = None,
) -> CheckResult:
    """Run every pass on ``screen``, or on the stored draft ``draft_id`` when no screen is given.

    Without ``store``, ``draft_id`` is the id a tool was given and is routed:
    ``library/<id>`` is checked in the library store when one is configured.
    With ``store``, ``draft_id`` is already an id inside that store.

    A ``tree`` (:class:`canopy_mcp.check.refs.Tree`) takes the store's place: the
    stored draft, ref existence and embedded documents are read through it and
    no store is opened, ``draft_id`` being an id inside it. ``served_at`` names
    the root the tree stands for (``personal`` when not given).

    Raises :class:`ToolError` when neither argument is given, the id is invalid,
    the stored draft does not exist (or is not JSON), or the widget catalog
    cannot be loaded.
    """
    if screen is None and draft_id is None:
        raise ToolError("check needs a screen document or a draft_id")
    settings = settings or get_settings()
    given = draft_id
    if tree is not None or store is not None:
        if draft_id is not None:
            validate_id(draft_id)
    elif draft_id is not None:
        store, draft_id = route(draft_id, settings)
    else:
        store = get_store(settings)
    if screen is None and tree is not None:
        screen = _read_from_tree(tree, draft_id)  # type: ignore[arg-type]
    elif screen is None:
        screen, _ = read_draft(store, draft_id, given)  # type: ignore[arg-type]
    if catalog is None:
        try:
            catalog = await ensure_catalog(settings)
        except CatalogError as exc:
            raise ToolError(f"widget catalog unavailable: {exc}") from exc

    result = CheckResult()
    result.schema = check_schema(screen, settings.staged_spec_dir)
    spec_staged = not any(f.code == SPEC_UNAVAILABLE for f in result.schema)
    refs = check_refs(screen, tree if tree is not None else store, draft_id, served_at=served_at)  # type: ignore[arg-type]
    semantic = check_semantic(screen, relay_on=settings.put_relay, tools=configured_tools(settings))
    result.semantic = semantic + refs
    lint = await check_lint(screen, catalog, client=client, settings=settings, props=spec_staged)
    result.lint = lint.findings
    result.skipped = lint.skipped
    return result


def _read_from_tree(tree: Tree, draft_id: str) -> Any:
    try:
        return read_screen(tree, draft_id)
    except FileNotFoundError as exc:
        raise ToolError(f"no draft stored under {draft_id!r}") from exc
    except ValueError as exc:
        raise ToolError(f"draft {draft_id!r} is not valid JSON: {exc}") from exc


@mcp.tool
async def check(screen: dict[str, Any] | None = None, draft_id: str | None = None) -> dict[str, Any]:
    """Check a Screen-IR document without saving it: schema, semantic, ref and lint findings.

    Pass exactly one source: ``screen`` (a document) or ``draft_id`` (a stored
    draft, or ``library/<id>`` for a screen of the shared library). With ``screen``, ``draft_id`` is optional and only sets the
    directory its ``embedded-display``/``open_display`` refs resolve in.

    Returns ``{schema, semantic, lint, skipped}``: each finding has ``nodeId``,
    ``code``, ``path`` (JSON pointer), ``severity``, ``message`` and, for a node
    inside an embedded screen, ``docId``. ``skipped`` lists checks that could
    not run (e.g. the PV directory was unreachable), so an empty finding list
    there is not proof the PVs exist. Errors block saving; warnings do not.

    An ``unresolved_ref`` warning also carries ``kind`` (``screen`` for an
    ``embedded-display``/``open_display`` ref, ``image`` for ``image-view.src``
    or ``pv-button.onImage``/``offImage``) and ``reason``. Image refs that are
    ``http(s):``, ``data:``, ``blob:`` or start with ``/`` are fetched by the
    browser and not checked; the others report ``missing`` (not in the draft
    store: ``convert_legacy`` copies images found beside the legacy file, so put
    the file there and re-port with ``overwrite=true``; a hand-written screen
    references the image by URL), ``above_root`` (the ref climbs above the
    drafts root: fix it or use a URL) or ``invalid_ref`` (not a storable asset
    path -- bad characters, an unexpanded macro, a non-image suffix, or a scheme
    a browser cannot fetch such as Phoebus ``platform:``: fix it or use an
    http(s) URL).
    """
    return (await run_check(screen, draft_id)).to_dict()
