"""The vault authorization matrix: primary owner, operator, viewer.

A vault always has exactly one primary owner (enforced at the database
layer by ``vault_members_one_owner_uq`` since migration 0007). The owner
manages sharing, operation/lifecycle policy, and notification preferences;
operators may upload/recover but never touch membership/policy; viewers
are strictly read-only. See issue #7.
"""
from __future__ import annotations

OWNER = "owner"
OPERATOR = "operator"
VIEWER = "viewer"

ROLES = (OWNER, OPERATOR, VIEWER)

# Roles allowed to run vault operations (upload/recover). The owner is a
# superset of the operator, never the other way around.
_OPERATING_ROLES = frozenset({OWNER, OPERATOR})


def is_valid_role(role: str) -> bool:
    """Whether ``role`` is one of the three matrix roles."""
    return role in ROLES


def can_operate(role: str) -> bool:
    """Whether ``role`` may upload or recover files in a vault."""
    return role in _OPERATING_ROLES


def is_owner(role: str) -> bool:
    """Whether ``role`` is the vault's primary owner.

    Only the primary owner may manage sharing, operation/lifecycle policy,
    and notification preferences, and run destructive lifecycle actions
    such as freeing local space.
    """
    return role == OWNER
