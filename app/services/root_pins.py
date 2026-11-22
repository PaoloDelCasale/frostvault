"""Whether two pinned Root Identities name the same Vault root (#784, #787).

Proofs (upload snapshots, upload attempts, cleanup batches, bulk uploads) pin
the Vault's Root Identity when they are written and stay immutable. When the
startup upgrade rewrites a Vault's identity for the same folder (v1 -> v2,
ADR-0054; v1/v2 -> v3, ADR-0055) the replaced value joins
``vaults.retired_root_identities``: a proof pinned with any of them still
names the current root. Every other difference is a changed root.
"""
from __future__ import annotations

from typing import Any, Mapping


def root_names(vault: Mapping[str, Any]) -> frozenset[Any]:
    """Every identity naming the Vault's current root: the current one and the retired ones."""
    root = vault.get("root_identity")
    if not root:
        return frozenset()
    return frozenset({root, *(vault.get("retired_root_identities") or ())})


def same_root(pinned: Any, current: Any, vault: Mapping[str, Any]) -> bool:
    if pinned == current:
        return True
    names = root_names(vault)
    return pinned in names and current in names


def same_pin(pinned: Mapping[str, Any], current: Mapping[str, Any],
             vault: Mapping[str, Any]) -> bool:
    """Equal target pins, the root identity compared with ``same_root``."""
    if pinned.keys() != current.keys():
        return False
    return all(
        same_root(value, current[key], vault) if key == "root_identity" else value == current[key]
        for key, value in pinned.items()
    )
