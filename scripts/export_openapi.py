#!/usr/bin/env python3
"""Export FrostVault's OpenAPI document without starting an HTTP server."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/export_openapi.py OUTPUT")
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
