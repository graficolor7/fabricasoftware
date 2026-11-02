#!/usr/bin/env python3
"""Valida archivos JSON de la fábrica contra sus schemas.

Uso: python tools/validate.py <schema> <archivo.json> [...]
     schema = project-state | task | supervisor-packet | supervisor-verdict | change-request
"""
import json
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    schema = json.loads((ROOT / "schemas" / f"{argv[0]}.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    bad = 0
    for p in argv[1:]:
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"FAIL {p}: {e}")
            bad += 1
            continue
        errs = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
        if errs:
            bad += 1
            for e in errs:
                print(f"FAIL {p}: {'/'.join(map(str, e.path)) or '(raíz)'}: {e.message}")
        else:
            print(f"OK   {p}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
