"""Validate the example telemetry descriptor against the pulseUDP schema.

The schema (``spec/Schema.json``) is the normative definition of a telemetry
descriptor; the example (``spec/examples/telemetry_example.json``) must conform
to it.
"""

import json
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "spec" / "Schema.json"
EXAMPLE_PATH = REPO_ROOT / "spec" / "examples" / "telemetry_example.json"


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def schema():
    return _load(SCHEMA_PATH)


def test_example_descriptor_is_valid(schema):
    """The shipped example descriptor conforms to the schema."""
    jsonschema.validate(instance=_load(EXAMPLE_PATH), schema=schema)


def test_field_missing_name_is_rejected(schema):
    """A field lacking the required `name` must fail validation."""
    bad = {"packet": {"fields": [{"type": "uint32"}]}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad, schema=schema)


def test_bitfield_without_bits_is_rejected(schema):
    """A bitfield must carry a `bits` list."""
    bad = {"packet": {"fields": [{"name": "Flags", "type": "bitfield"}]}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad, schema=schema)
