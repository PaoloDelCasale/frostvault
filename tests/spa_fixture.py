"""Minimal frontend/dist fixture for HTTP tests that hit HTML routes."""

from __future__ import annotations

from pathlib import Path


def write_spa_dist(parent: Path) -> Path:
    """Create a tiny Vite-like dist tree under ``parent`` and return its path."""
    dist_dir = parent / "frontend-dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "assets").mkdir(exist_ok=True)
    (dist_dir / "index.html").write_text(
        "<!doctype html><html><head><title>FrostVault</title></head>"
        '<body><div id="root">spa-shell</div>'
        '<script type="module" src="/assets/index-test.js"></script>'
        "</body></html>\n",
        encoding="utf-8",
    )
    (dist_dir / "assets" / "index-test.js").write_text(
        "console.log('spa');\n",
        encoding="utf-8",
    )
    return dist_dir
