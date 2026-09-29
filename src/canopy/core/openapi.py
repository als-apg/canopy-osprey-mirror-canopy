from __future__ import annotations

from copy import deepcopy
from typing import Any


def _rewrite_references(value: Any, component_names: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [_rewrite_references(item, component_names) for item in value]
    if not isinstance(value, dict):
        return value

    rewritten = {}
    for key, item in value.items():
        if key == "$ref" and isinstance(item, str):
            parts = item.split("/")
            if len(parts) == 4 and parts[:2] == ["#", "components"]:
                section, name = parts[2], parts[3].replace("~1", "/").replace("~0", "~")
                if name in component_names.get(section, {}):
                    escaped_name = component_names[section][name].replace("~", "~0").replace("/", "~1")
                    item = f"#/components/{section}/{escaped_name}"
        elif key == "security" and isinstance(item, list):
            item = [
                {
                    component_names.get("securitySchemes", {}).get(name, name): scopes
                    for name, scopes in requirement.items()
                }
                for requirement in item
            ]
        elif key == "operationId" and isinstance(item, str):
            item = f"{component_names['prefix']}_{item}"
        rewritten[key] = _rewrite_references(item, component_names)
    return rewritten


def build_aggregate_openapi(
    root_spec: dict[str, Any],
    module_specs: dict[str, dict[str, Any]],
    server_url: str,
) -> dict[str, Any]:
    """Combine service specs while isolating component and operation names."""
    aggregate = deepcopy(root_spec)
    aggregate["servers"] = [{"url": server_url or "/"}]
    aggregate["paths"] = {}
    aggregate["components"] = {}

    specs = [("Canopy", "", root_spec), *[(name.title(), f"/{name}", spec) for name, spec in module_specs.items()]]
    for prefix, path_prefix, spec in specs:
        component_names = {
            section: {name: f"{prefix}{name}" for name in components}
            for section, components in spec.get("components", {}).items()
        }
        component_names["prefix"] = prefix
        rewritten_spec = _rewrite_references(spec, component_names)

        for path, path_item in rewritten_spec.get("paths", {}).items():
            aggregate_path = f"{path_prefix}{path}" or "/"
            if aggregate_path in aggregate["paths"]:
                raise ValueError(f"Duplicate path in aggregate OpenAPI document: {aggregate_path}")
            aggregate["paths"][aggregate_path] = path_item

        for section, components in rewritten_spec.get("components", {}).items():
            target = aggregate["components"].setdefault(section, {})
            for name, definition in components.items():
                aggregate_name = component_names[section][name]
                if aggregate_name in target:
                    raise ValueError(f"Duplicate OpenAPI component: {section}/{aggregate_name}")
                target[aggregate_name] = definition

    return aggregate
