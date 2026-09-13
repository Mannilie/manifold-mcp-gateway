"""The settings_schema subset the admin UI can render (DECISIONS.md, Phase 3 gate 2).

Anything outside this subset fails the toolset contract test, so a toolset author finds
out at test time rather than in the browser. The server validates settings against the
same schema on write; the renderer is a convenience, not the guard.
"""

from __future__ import annotations

import re
from typing import Any

import jsonschema

SECRET_WORDS = frozenset(
    {"password", "secret", "token", "key", "apikey", "credential", "credentials"}
)

_TOP_LEVEL_KEYS = frozenset(
    {"type", "properties", "required", "additionalProperties", "title", "description", "$schema"}
)
_COMMON_KEYS = frozenset({"type", "title", "description", "default", "x-manifold"})
_BY_TYPE: dict[str, frozenset[str]] = {
    "string": frozenset({"enum", "minLength", "maxLength", "pattern", "format"}),
    "number": frozenset({"minimum", "maximum", "enum"}),
    "integer": frozenset({"minimum", "maximum", "enum"}),
    "boolean": frozenset(),
    "array": frozenset({"items", "minItems", "maxItems", "uniqueItems"}),
}
_X_MANIFOLD_KEYS = frozenset({"placeholder", "help_url", "multiline"})


class SchemaError(ValueError):
    pass


def _segments(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [s.lower() for s in re.split(r"[^A-Za-z0-9]+| ", spaced) if s]


def looks_secret(name: str) -> bool:
    return any(seg in SECRET_WORDS for seg in _segments(name))


def validate_settings_schema(schema: dict[str, Any]) -> None:
    """Raise SchemaError if the schema is outside the renderable subset."""
    if not isinstance(schema, dict):
        raise SchemaError("settings_schema must be an object")
    if schema.get("type") != "object":
        raise SchemaError("settings_schema must have type 'object'")
    unknown = set(schema) - _TOP_LEVEL_KEYS
    if unknown:
        raise SchemaError(f"unsupported top-level keywords: {sorted(unknown)}")
    if schema.get("additionalProperties", False) is not False:
        raise SchemaError("additionalProperties must be false or omitted")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise SchemaError("properties must be an object")
    for name in schema.get("required", []):
        if name not in properties:
            raise SchemaError(f"required lists unknown property {name!r}")
    for name, prop in properties.items():
        if looks_secret(name):
            raise SchemaError(
                f"property {name!r} looks like a secret. Secrets never go in settings_schema; "
                "declare an auth kind in supported_auth and read them from Credentials."
            )
        _validate_property(name, prop)
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise SchemaError(f"invalid JSON Schema: {exc.message}") from exc


def _validate_property(name: str, prop: Any) -> None:
    if not isinstance(prop, dict):
        raise SchemaError(f"property {name!r} must be an object")
    ptype = prop.get("type")
    if ptype not in _BY_TYPE:
        raise SchemaError(f"property {name!r} has type {ptype!r}; supported: {sorted(_BY_TYPE)}")
    allowed = _COMMON_KEYS | _BY_TYPE[ptype]
    unknown = set(prop) - allowed
    if unknown:
        raise SchemaError(f"property {name!r} uses unsupported keywords {sorted(unknown)}")
    if ptype == "array":
        items = prop.get("items")
        if not isinstance(items, dict) or items.get("type") != "string":
            raise SchemaError(f"property {name!r}: arrays must have items of type string")
        if set(items) - {"type", "minLength", "maxLength", "pattern", "title", "description"}:
            raise SchemaError(f"property {name!r}: array items support only string constraints")
    if "enum" in prop:
        values = prop["enum"]
        if not isinstance(values, list) or not values:
            raise SchemaError(f"property {name!r}: enum must be a non-empty list")
    hints = prop.get("x-manifold", {})
    if not isinstance(hints, dict):
        raise SchemaError(f"property {name!r}: x-manifold must be an object")
    unknown_hints = set(hints) - _X_MANIFOLD_KEYS
    if unknown_hints:
        raise SchemaError(
            f"property {name!r}: unknown x-manifold hints {sorted(unknown_hints)};"
            f" supported: {sorted(_X_MANIFOLD_KEYS)}"
        )
    if "multiline" in hints and (ptype != "string" or not isinstance(hints["multiline"], bool)):
        raise SchemaError(f"property {name!r}: multiline applies to strings and must be boolean")


def validate_settings(schema: dict[str, Any], settings: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems, empty when the settings are valid."""
    validator = jsonschema.Draft202012Validator(schema)
    problems = []
    for error in sorted(validator.iter_errors(settings), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in error.path) or "(root)"
        problems.append(f"{where}: {error.message}")
    return problems
