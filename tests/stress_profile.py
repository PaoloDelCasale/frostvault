"""Quantitative upload stress only; deterministic race tests never use this knob."""
from __future__ import annotations

import os


def stress_file_count(full_count: int) -> int:
    """Explicit local invocations stay full; CI must choose a valid profile."""
    profile = os.environ.get("FROSTVAULT_STRESS_PROFILE", "full")
    if profile == "full":
        return full_count
    if profile == "pr":
        # Four nested uploads on every worker, not fewer concurrent workers.
        return 64
    raise ValueError(f"Invalid FROSTVAULT_STRESS_PROFILE: {profile!r}; expected pr or full")
