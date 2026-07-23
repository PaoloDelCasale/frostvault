"""Ready-to-use SMTP email templates for operational notifications (issue #16)."""
from __future__ import annotations

from typing import Any

from ..branding import PRODUCT_NAME

SUBJECT_PREFIX = f"[{PRODUCT_NAME}]"

TEMPLATES: dict[str, dict[str, str]] = {
    "vault_membership_changed": {
        "subject": f"{SUBJECT_PREFIX} Vault membership changed",
        "body": (
            "An administrator changed membership for vault {vault_id}.\n"
            "Reason: {reason}\n"
        ),
    },
    "vault_ownership_transferred": {
        "subject": f"{SUBJECT_PREFIX} Vault ownership transferred",
        "body": (
            "Primary ownership for vault {vault_id} was transferred.\n"
            "Reason: {reason}\n"
        ),
    },
    "vault_recovery_exported": {
        "subject": f"{SUBJECT_PREFIX} Vault recovery material exported",
        "body": (
            "Recovery material for vault {vault_id} was exported.\n"
            "Reason: {reason}\n"
        ),
    },
    "upload_verified": {
        "subject": f"{SUBJECT_PREFIX} Upload verified",
        "body": "An upload for vault {vault_id} completed verification.\n",
    },
    "verification_failed": {
        "subject": f"{SUBJECT_PREFIX} Verification failed",
        "body": (
            "Verification failed for vault {vault_id}.\n"
            "Details: {body}\n"
        ),
    },
    "worker_error": {
        "subject": f"{SUBJECT_PREFIX} Background worker error",
        "body": (
            "Component {component} reported a {classification} error.\n"
            "{message}\n"
        ),
    },
    "metadata_backup_failed": {
        "subject": f"{SUBJECT_PREFIX} Metadata backup failed",
        "body": (
            "An encrypted metadata backup failed.\n"
            "Details: {body}\n"
        ),
    },
    "default": {
        "subject": f"{SUBJECT_PREFIX} {{title}}",
        "body": "{body}\n",
    },
}


def render_email(event: str, **context: Any) -> dict[str, str]:
    """Render subject/body for ``event`` using the built-in templates."""
    template = TEMPLATES.get(event) or TEMPLATES["default"]
    safe = {key: ("" if value is None else str(value)) for key, value in context.items()}
    safe.setdefault("title", event)
    safe.setdefault("body", "")
    safe.setdefault("reason", "")
    safe.setdefault("vault_id", "")
    safe.setdefault("component", "")
    safe.setdefault("classification", "")
    safe.setdefault("message", "")
    return {
        "subject": template["subject"].format_map(safe),
        "body": template["body"].format_map(safe),
    }
