"""The package-data schema copy must not drift from the normative spec/Schema.json."""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_SCHEMA = REPO_ROOT / "spec" / "Schema.json"
PKG_SCHEMA = REPO_ROOT / "src" / "pulseudp" / "data" / "Schema.json"


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_bundled_schema_matches_spec():
    assert PKG_SCHEMA.exists(), "package copy missing; re-copy spec/Schema.json"
    assert _load(PKG_SCHEMA) == _load(SPEC_SCHEMA), (
        "src/pulseudp/data/Schema.json has drifted from spec/Schema.json; "
        "re-copy the spec schema into the package")
