#!/usr/bin/env python3
"""Export FrostVault's OpenAPI document without starting an HTTP server.

The only writer of ``frontend/openapi.json`` (ADR-0056): CI runs it and fails
on any byte of drift from the committed file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402


def document() -> str:
    """The document as committed: in the order the application generates it."""
    return json.dumps(app.openapi(), indent=2) + "\n"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/export_openapi.py OUTPUT")
    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document(), encoding="utf-8")


if __name__ == "__main__":
    main()
