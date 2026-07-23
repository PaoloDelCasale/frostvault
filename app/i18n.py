"""English-default localization with Italian UI catalogs.

Persisted Job and audit state must use stable message keys; localize only when
rendering responses or UI chrome. See docs/adr/0006-english-default-i18n.md.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

DEFAULT_LOCALE = "en"
SUPPORTED_LOCALES = ("en", "it")
LOCALE_COOKIE_NAME = "frostvault_locale"
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

# Keys that every supported locale must define. Used by catalog integrity tests
# and the contributor translation workflow.
CRITICAL_KEYS: frozenset[str] = frozenset(
    {
        "ui.sign_out",
        "ui.refresh_list",
        "ui.language",
        "ui.language_en",
        "ui.language_it",
        "state.both",
        "state.local_only",
        "state.cloud_only",
        "state.restoring",
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
        "job.upload_stopped",
        "job.recovery_stopped",
        "job.cleanup_stopped",
        "job.rename_stopped",
        "job.operation_failed",
        "job.operation_stopped",
        "api.upload_started",
        "api.recovery_started",
        "api.free_space_started",
        "api.locale_updated",
        "email.subject.admin_action",
        "email.body.admin_action",
    }
)


def resolved_catalog_path(locale: str | None = None) -> Path:
    """Return the on-disk catalog path for an allowlisted locale only.

    Filenames are chosen from constants after normalization so path construction
    never embeds raw caller-controlled locale text.
    """
    resolved = normalize_locale(locale)
    # Constant-backed branches keep filesystem paths independent of raw input.
    if resolved == "it":
        return LOCALES_DIR / "it.json"
    return LOCALES_DIR / "en.json"


@lru_cache(maxsize=None)
def _load_catalog(locale: str) -> dict[str, str]:
    path = resolved_catalog_path(locale)
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Locale catalog {path} must be a JSON object")
    return {str(key): str(value) for key, value in data.items()}


def available_locales() -> tuple[str, ...]:
    return SUPPORTED_LOCALES


def normalize_locale(value: str | None) -> str:
    if not value:
        return DEFAULT_LOCALE
    language = value.replace("_", "-").split("-", 1)[0].strip().lower()
    if language == "it":
        return "it"
    if language == "en":
        return "en"
    return DEFAULT_LOCALE


def resolve_locale(
    *,
    cookie_value: str | None = None,
    accept_language: str | None = None,
) -> str:
    """Resolve locale from explicit preference, then Accept-Language, then default."""
    if cookie_value:
        return normalize_locale(cookie_value)
    if accept_language:
        for part in accept_language.split(","):
            tag = part.split(";", 1)[0].strip()
            if not tag:
                continue
            candidate = normalize_locale(tag)
            # normalize_locale maps unknowns to English; only accept an exact match.
            language = tag.replace("_", "-").split("-", 1)[0].strip().lower()
            if language in SUPPORTED_LOCALES:
                return candidate
    return DEFAULT_LOCALE


def catalog(locale: str | None = None) -> dict[str, str]:
    return dict(_load_catalog(normalize_locale(locale)))


def translate(key: str, locale: str | None = None, **params: Any) -> str:
    resolved = normalize_locale(locale)
    message = _load_catalog(resolved).get(key)
    if message is None and resolved != DEFAULT_LOCALE:
        message = _load_catalog(DEFAULT_LOCALE).get(key)
    if message is None:
        return key
    if not params:
        return message
    try:
        return message.format(**params)
    except (KeyError, ValueError):
        return message


def render_email(template_key: str, locale: str | None = None, **params: Any) -> str:
    """Render a notification/email body from a stable message key."""
    return translate(template_key, locale=locale, **params)


def catalog_keys(locale: str | None = None) -> frozenset[str]:
    return frozenset(_load_catalog(normalize_locale(locale)))


def missing_critical_keys(locale: str | None = None) -> frozenset[str]:
    return CRITICAL_KEYS - catalog_keys(locale)


def unused_critical_keys() -> frozenset[str]:
    """Critical keys that are not present in the English source catalog."""
    return CRITICAL_KEYS - catalog_keys(DEFAULT_LOCALE)


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


def format_message_params(params: Mapping[str, Any] | None) -> str:
    if not params:
        return "{}"
    return json.dumps(dict(params), ensure_ascii=False, sort_keys=True)


def parse_message_params(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def present_job_message(row: Mapping[str, Any], locale: str | None = None) -> str:
    """Localize a Job row for display; fall back to stored English/legacy prose."""
    key = row.get("message_key")
    if key:
        params = row.get("message_params")
        if isinstance(params, str) or params is None:
            params = parse_message_params(params)
        elif not isinstance(params, Mapping):
            params = {}
        return translate(str(key), locale=locale, **dict(params))
    return str(row.get("message") or "")
