"""FileCatalogLoader — read ``widgets/*.json`` and build a :class:`WidgetIndex`.

Synchronous; called once at startup. Fails loud on any malformed file or
collision (duplicate id, duplicate ``qt_mapping.class``); a half-loaded
registry is worse than no registry.
"""

import json
import hashlib
import logging
from collections import defaultdict

from pydantic import ValidationError
from jsonschema import Draft7Validator

from canopy.beaver.config import BeaverConfig
from canopy.beaver.errors import CatalogMisconfigured
from canopy.beaver.catalog.index import WidgetIndex
from canopy.beaver.schemas.widget import WidgetDTO
from canopy.beaver.catalog.meta_schema import META_SCHEMA

logger = logging.getLogger(__name__)


class FileCatalogLoader:
    def __init__(self, config: BeaverConfig) -> None:
        self._config = config
        self._meta_validator = Draft7Validator(META_SCHEMA)

    def load(self) -> WidgetIndex:
        widgets_dir = self._config.widgets_dir
        if not widgets_dir.is_dir():
            raise CatalogMisconfigured(f"widgets_dir does not exist: {widgets_dir}")

        by_id: dict[str, WidgetDTO] = {}
        by_qt_class: dict[str, WidgetDTO] = {}
        by_category: dict[str, list[WidgetDTO]] = defaultdict(list)
        first_seen_id: dict[str, str] = {}
        first_seen_qt: dict[str, str] = {}

        for path in sorted(widgets_dir.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                raise CatalogMisconfigured(f"failed to read {path}: {exc}") from exc

            errors = sorted(
                self._meta_validator.iter_errors(raw),
                key=lambda e: list(e.absolute_path),
            )
            if errors:
                summary = "; ".join(f"/{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in errors)
                raise CatalogMisconfigured(f"{path} fails meta-schema: {summary}")

            try:
                widget = WidgetDTO.model_validate(raw)
            except ValidationError as exc:
                raise CatalogMisconfigured(f"{path} fails type validation: {exc}") from exc

            if widget.id in by_id:
                raise CatalogMisconfigured(
                    f"duplicate widget id {widget.id!r}: {path} conflicts with {first_seen_id[widget.id]}"
                )
            by_id[widget.id] = widget
            first_seen_id[widget.id] = str(path)

            if widget.qt_mapping is not None:
                qt_class = widget.qt_mapping.class_
                if qt_class in by_qt_class:
                    raise CatalogMisconfigured(
                        f"duplicate qtMapping.class {qt_class!r}: {path} conflicts with {first_seen_qt[qt_class]}"
                    )
                by_qt_class[qt_class] = widget
                first_seen_qt[qt_class] = str(path)

            by_category[widget.category].append(widget)

        registry_version = _hash_pairs(sorted((w.id, w.version) for w in by_id.values()))
        logger.info("Beaver loaded %d widgets from %s", len(by_id), widgets_dir)

        return WidgetIndex(
            by_id=by_id,
            by_qt_class=by_qt_class,
            by_category=dict(by_category),
            registry_version=registry_version,
        )


def _hash_pairs(pairs: list[tuple[str, str]]) -> str:
    h = hashlib.sha256()
    for k, v in pairs:
        h.update(f"{k}={v}\n".encode())
    return h.hexdigest()[:12]
