"""Locale catalog integrity checks (#791): critical keys and locale parity.

The contributor translation workflow (``docs/translation-workflow.md``) lists
here the keys every supported locale must define.
"""
from __future__ import annotations

from app import i18n
from app.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, has_message

# Keys that every supported locale must define.
CRITICAL_KEYS: frozenset[str] = frozenset(
    {
        "ui.sign_out",
        "ui.language",
        "ui.language_en",
        "ui.language_it",
        "activity.heading",
        "activity.by_vault",
        "activity.idle",
        "activity.stability_wait",
        "activity.queued",
        "activity.uploading",
        "activity.verifying",
        "activity.sent",
        "activity.stable_in",
        "activity.waiting_observation",
        "activity.admission",
        "activity.current_work",
        "activity.unavailable",
        "state.pending",
        "state.protected",
        "state.problem",
        "state.unconfirmed",
        "state.cloud_only",
        "state.excluded",
        "state.unsupported",
        "state.last_known",
        "state.previous_snapshot_protected",
        "state.manual_upload",
        "action.upload",
        "action.recover",
        "action.free-space",
        "action.rename",
        "operation.queued",
        "operation.uploading",
        "operation.verifying",
        "operation.retrying",
        "operation.pending_approval",
        "operation.failed",
        "operation.cancelled",
        "operation.upload_verified",
        "operation.rename_completed",
        "job.hashing_local_file",
        "job.verifying_cloud_copy",
        "job.upload_verified",
        "job.local_space_freed",
        "job.recovered_to",
        "job.retrying_transient",
        "job.retrying_source_changed",
        "job.source_changed_stalled",
        "job.upload_stopped",
        "job.recovery_stopped",
        "job.recovery_stopped_waiting",
        "job.recovery_approval_expired",
        "notification.recovery_approval.title",
        "notification.recovery_approval.body",
        "job.cleanup_stopped",
        "job.rename_stopped",
        "job.operation_failed",
        "job.operation_stopped",
        "notification.recovery_completed.title",
        "notification.recovery_completed.body",
        "notification.recovery_failed.title",
        "notification.protection_delayed.title",
        "notification.weekly_summary.title",
        "notification.job_failed.title",
        "notification.job_failed.body",
        "notification.archive_version_missing.title",
        "notification.archive_version_missing.body",
        "notification.cloud_only_conflict.title",
        "notification.cloud_only_conflict.body",
        "api.upload_started",
        "api.recovery_started",
        "api.free_space_started",
        "api.locale_updated",
    }
)


def catalog_keys(locale: str | None = None) -> frozenset[str]:
    return frozenset(i18n._load_catalog(i18n.normalize_locale(locale)))


def missing_critical_keys(locale: str | None = None) -> frozenset[str]:
    return frozenset(key for key in CRITICAL_KEYS if not has_message(key, locale))


def unused_critical_keys() -> frozenset[str]:
    """Critical keys that are not present in the English source catalog."""
    return missing_critical_keys(DEFAULT_LOCALE)


def locale_key_parity() -> dict[str, frozenset[str]]:
    """Keys present in one locale catalog but missing from another."""
    english = catalog_keys(DEFAULT_LOCALE)
    gaps: dict[str, frozenset[str]] = {}
    for locale in SUPPORTED_LOCALES:
        if locale == DEFAULT_LOCALE:
            continue
        other = catalog_keys(locale)
        missing = english - other
        extra = other - english
        if missing:
            gaps[f"{locale}.missing"] = missing
        if extra:
            gaps[f"{locale}.extra"] = extra
    return gaps
