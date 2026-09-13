from __future__ import annotations

import pytest

from manifold.gateway.schema import (
    SchemaError,
    looks_secret,
    validate_settings,
    validate_settings_schema,
)

GOOD = {
    "type": "object",
    "title": "Sheets",
    "properties": {
        "allowed_spreadsheet_ids": {
            "type": "array",
            "title": "Allowed spreadsheet IDs",
            "description": "Empty means any the credential can reach.",
            "items": {"type": "string", "minLength": 10},
            "default": [],
            "x-manifold": {"placeholder": "1AbC...", "help_url": "https://example.com/help"},
        },
        "region": {"type": "string", "enum": ["au", "eu"], "default": "au"},
        "max_rows": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 500},
        "notes": {"type": "string", "x-manifold": {"multiline": True}},
        "dry_run": {"type": "boolean", "default": False},
    },
    "required": ["region"],
    "additionalProperties": False,
}


def test_good_schema_passes():
    validate_settings_schema(GOOD)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("api_key", True),
        ("apiKey", True),
        ("client_secret", True),
        ("password", True),
        ("refresh_token", True),
        ("private_key_pem", True),
        ("allowed_spreadsheet_ids", False),
        ("keyboard_layout", False),
        ("tokenizer", False),
        ("region", False),
    ],
)
def test_secret_looking_names(name, expected):
    assert looks_secret(name) is expected


def test_secret_property_is_rejected_with_pointer_to_credentials():
    schema = {"type": "object", "properties": {"api_key": {"type": "string"}}}
    with pytest.raises(SchemaError, match="supported_auth"):
        validate_settings_schema(schema)


@pytest.mark.parametrize(
    "prop",
    [
        {"type": "object", "properties": {}},
        {"type": "array", "items": {"type": "integer"}},
        {"type": "string", "oneOf": [{"const": "a"}]},
        {"type": "string", "x-manifold": {"colour": "red"}},
        {"type": "integer", "x-manifold": {"multiline": True}},
        {"type": "string", "enum": []},
        {"$ref": "#/defs/x"},
    ],
)
def test_outside_subset_is_rejected(prop):
    with pytest.raises(SchemaError):
        validate_settings_schema({"type": "object", "properties": {"field": prop}})


def test_top_level_must_be_closed_object():
    with pytest.raises(SchemaError):
        validate_settings_schema({"type": "object", "additionalProperties": True})
    with pytest.raises(SchemaError):
        validate_settings_schema({"type": "object", "required": ["missing"]})
    with pytest.raises(SchemaError):
        validate_settings_schema({"type": "object", "allOf": []})


def test_validate_settings_reports_paths():
    problems = validate_settings(GOOD, {"region": "us", "max_rows": 0, "extra": 1})
    assert any(p.startswith("region:") for p in problems)
    assert any(p.startswith("max_rows:") for p in problems)
    assert any("extra" in p for p in problems)
    assert validate_settings(GOOD, {"region": "au"}) == []
