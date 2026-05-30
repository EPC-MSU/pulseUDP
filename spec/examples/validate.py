#!/usr/bin/env python3
"""
JSON Validator by Schema

Usage:
    python validate_json.py <json_file> <schema_file>

Example:
    python validate_json.py data.json schema.json
"""

import json
import sys
import argparse

try:
    from jsonschema import validate, ValidationError
except ImportError:
    print("Error: the 'jsonschema' library is not installed.")
    print("Install it with: pip install jsonschema")
    sys.exit(1)

def load_json_file(file_path):
    """Load JSON from a file and return a Python object."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: file not found - {file_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in file {file_path}\n{e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(
        description="Validates a JSON file against a JSON Schema."
    )
    parser.add_argument("json_file", help="Path to the JSON file to validate")
    parser.add_argument("schema_file", help="Path to the JSON Schema file")
    args = parser.parse_args()

    # Load data
    json_data = load_json_file(args.json_file)
    schema_data = load_json_file(args.schema_file)

    # Validation
    try:
        validate(instance=json_data, schema=schema_data)
        print("[OK] JSON validated successfully against the schema.")
    except ValidationError as e:
        print("[ERROR] Validation error:")
        print(f"   Path: {' -> '.join(str(p) for p in e.absolute_path) or 'root'}")
        print(f"   Message: {e.message}")
        # Optionally: show the schema at the point of the error
        if e.schema:
            print(f"   Constraint schema: {e.schema}")
        sys.exit(1)

if __name__ == "__main__":
    main()