"""Validate the example telemetry descriptor against the pulseUDP schema.

The schema (``spec/Schema.json``) is the normative definition of a telemetry
descriptor; the example (``spec/examples/telemetry_example.json``) must conform
to it.
"""

import json
from pathlib import Path

import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "spec" / "Schema.json"
EXAMPLE_PATH = REPO_ROOT / "spec" / "examples" / "telemetry_example.json"


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_example_descriptor_is_valid():
    """The shipped example descriptor conforms to the schema."""
    jsonschema.validate(instance=_load(EXAMPLE_PATH), schema=_load(SCHEMA_PATH))
