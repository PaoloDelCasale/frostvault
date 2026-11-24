"""English-default localization with Italian UI catalogs.

Persisted Job and audit state must use stable message keys; localize only when
rendering responses or UI chrome. See docs/adr/0006-english-default-i18n.md.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone, tzinfo
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_LOCALE = "en"
SUPPORTED_LOCALES = ("en", "it")
LOCALE_COOKIE_NAME = "frostvault_locale"
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

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


def plural_category(count: Any) -> str | None:
    """CLDR plural category for English and Italian, or ``None`` if not a count.

    Both supported languages use ``one`` for exactly the integer 1 and
    ``other`` for everything else (the frontend uses ``Intl.PluralRules``).
    """
    if isinstance(count, bool) or not isinstance(count, (int, float)):
        return None
    return "one" if count == 1 and float(count).is_integer() else "other"


def _resolve_key(catalog_: Mapping[str, str], key: str, params: Mapping[str, Any]) -> str:
    """Counted strings: ``<key>_<category>``, then ``<key>_other``, then ``<key>``."""
    category = plural_category(params.get("count"))
    if category is not None:
        for candidate in (f"{key}_{category}", f"{key}_other"):
            if candidate in catalog_:
                return candidate
    return key


def has_message(key: str, locale: str | None = None) -> bool:
    """Whether ``key`` is defined, directly or as plural variants."""
    keys = _load_catalog(normalize_locale(locale))
    return key in keys or f"{key}_other" in keys


def translate(key: str, locale: str | None = None, **params: Any) -> str:
    resolved = normalize_locale(locale)
    current = _load_catalog(resolved)
    message = current.get(_resolve_key(current, key, params))
    if message is None and resolved != DEFAULT_LOCALE:
        english = _load_catalog(DEFAULT_LOCALE)
        message = english.get(_resolve_key(english, key, params))
    if message is None:
        return key
    if not params:
        return message
    try:
        return message.format(**params)
    except (KeyError, ValueError):
        return message


_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def localized_digits(text: str, locale: str | None) -> str:
    """Swap English separators for Italian ones (``1,234.5`` → ``1.234,5``)."""
    if normalize_locale(locale) == "it":
        return text.translate(str.maketrans({",": ".", ".": ","}))
    return text


def format_bytes(value: Any, locale: str | None = None) -> str:
    """A size in binary (IEC) units with the locale's decimal separator, as in the SPA."""
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    index = 0
    while abs(amount) >= 1024 and index < len(_BYTE_UNITS) - 1:
        amount /= 1024
        index += 1
    digits = f"{amount:,.0f}" if index == 0 else f"{amount:,.1f}"
    return f"{localized_digits(digits, locale)} {_BYTE_UNITS[index]}"


def format_money(value: Any, locale: str | None = None, currency: str = "EUR") -> str:
    """Two-decimal amount as the SPA shows it: ``€1,234.56`` / ``1.234,56 €``.

    Positive amounts under one cent read ``< €0.01``; invalid amounts ``—``.
    """
    try:
        amount = Decimal(str(value if value is not None else 0))
    except InvalidOperation:
        return "—"
    if not amount.is_finite():
        return "—"
    subcent = 0 < amount < Decimal("0.01")
    shown = Decimal("0.01") if subcent else amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    digits = localized_digits(f"{abs(shown):,.2f}", locale)
    sign = "-" if shown < 0 else ""
    symbol = "€" if currency == "EUR" else None
    if normalize_locale(locale) == "it":
        money = f"{sign}{digits} {symbol or currency}"
    else:
        money = f"{sign}{symbol}{digits}" if symbol else f"{sign}{digits} {currency}"
    return f"< {money}" if subcent else money


_MONTHS = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "it": ("gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"),
}


def owner_timezone() -> tuple[tzinfo, bool]:
    """The container ``TZ`` zone, or ``(UTC, True)`` when it is missing or invalid."""
    name = (os.getenv("TZ") or "").strip().lstrip(":")
    if name:
        try:
            return ZoneInfo(name), False
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone.utc, True


def format_datetime(value: Any, locale: str | None = None) -> str:
    """An ISO date-time as local date and time, like the SPA's ``formatDateTime``.

    ``15 nov 2026, 14:30`` / ``15 Nov 2026, 14:30`` in the ``TZ`` zone; without a
    valid ``TZ`` the time is UTC and says so. Date-only values keep their calendar
    day; empty values read ``—``; unparseable text is returned unchanged.
    """
    if not value:
        return "—"
    months = _MONTHS[normalize_locale(locale)]
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        return f"{value.day} {months[value.month - 1]} {value.year}"
    else:
        text = str(value)
        try:
            if len(text) == 10:
                day = date.fromisoformat(text)
                return f"{day.day} {months[day.month - 1]} {day.year}"
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    zone, fallback = owner_timezone()
    local = moment.astimezone(zone)
    shown = f"{local.day} {months[local.month - 1]} {local.year}, {local:%H:%M}"
    return f"{shown} UTC" if fallback else shown


def format_message_params(params: Mapping[str, Any] | None) -> str:
    if not params:
        return "{}"
    return json.dumps(dict(params), ensure_ascii=False, sort_keys=True)


def parse_message_params(raw: str | Mapping[str, Any] | None) -> dict[str, Any]:
    """Read Job message parameters: ``jobs.message_params`` is ``jsonb``."""
    if not raw:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


# Job message parameters that are a UTC calendar day (``YYYY-MM-DD``), shown
# in the reader's language: ``1 dic 2027`` / ``1 Dec 2027`` (#749).
_DATE_PARAMS: dict[str, tuple[str, ...]] = {"job.cloud_purge_deferred": ("until",)}


def present_job_message(row: Mapping[str, Any], locale: str | None = None) -> str:
    """Localize a Job row for display; fall back to stored English/legacy prose."""
    key = row.get("message_key")
    if key:
        params = row.get("message_params")
        if isinstance(params, str) or params is None:
            params = parse_message_params(params)
        elif not isinstance(params, Mapping):
            params = {}
        params = dict(params)
        for name in _DATE_PARAMS.get(str(key), ()):
            if params.get(name):
                params[name] = format_datetime(params[name], locale)
        return translate(str(key), locale=locale, **params)
    return str(row.get("message") or "")
