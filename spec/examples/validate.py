#!/usr/bin/env python3
"""
JSON Validator by Schema

Usage:
    python validate_json.py <json_file> <schema_file>

Пример:
    python validate_json.py data.json schema.json
"""

import json
import sys
import argparse
from pathlib import Path

try:
    from jsonschema import validate, ValidationError
except ImportError:
    print("Ошибка: библиотека 'jsonschema' не установлена.")
    print("Установите её командой: pip install jsonschema")
    sys.exit(1)

def load_json_file(file_path):
    """Загружает JSON из файла, возвращает объект Python."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Ошибка: файл не найден - {file_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Ошибка: некорректный JSON в файле {file_path}\n{e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(
        description="Проверяет JSON-файл на соответствие JSON Schema."
    )
    parser.add_argument("json_file", help="Путь к JSON-файлу для проверки")
    parser.add_argument("schema_file", help="Путь к файлу с JSON Schema")
    args = parser.parse_args()

    # Загрузка данных
    json_data = load_json_file(args.json_file)
    schema_data = load_json_file(args.schema_file)

    # Валидация
    try:
        validate(instance=json_data, schema=schema_data)
        print("[OK] JSON успешно прошёл проверку по схеме.")
    except ValidationError as e:
        print("[ERROR] Ошибка валидации:")
        print(f"   Путь: {' -> '.join(str(p) for p in e.absolute_path) or 'корень'}")
        print(f"   Сообщение: {e.message}")
        # Дополнительно: показать схему в месте ошибки (опционально)
        if e.schema:
            print(f"   Схема ограничения: {e.schema}")
        sys.exit(1)

if __name__ == "__main__":
    main()