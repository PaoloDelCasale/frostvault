"""Whether two pinned Root Identities name the same Vault root (#784, ADR-0054).

Proofs (upload snapshots, cleanup batches) pin the Vault's Root Identity when
they are written and stay immutable. A v1 identity rewritten as v2 for a
provably unchanged folder is kept as ``vaults.legacy_root_identity``: a proof
pinned with it still names the current root. Every other difference is a
changed root.
"""
from __future__ import annotations

from typing import Any, Mapping


def same_root(pinned: Any, current: Any, vault: Mapping[str, Any]) -> bool:
    if pinned == current:
        return True
    legacy = vault.get("legacy_root_identity")
    root = vault.get("root_identity")
    return bool(legacy and root) and {pinned, current} == {legacy, root}


def same_pin(pinned: Mapping[str, Any], current: Mapping[str, Any],
             vault: Mapping[str, Any]) -> bool:
    """Equal target pins, the root identity compared with ``same_root``."""
    if pinned.keys() != current.keys():
        return False
    return all(
        same_root(value, current[key], vault) if key == "root_identity" else value == current[key]
        for key, value in pinned.items()
    )
