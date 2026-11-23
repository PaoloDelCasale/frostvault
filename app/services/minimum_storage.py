"""Minimum Storage Duration and Class Entry of Archive Versions (#731).

Some storage classes bill a version for a minimum number of days once it
enters the class (``vault_moves.MINIMUM_DAYS``), whether or not it is deleted
earlier. The count starts at the version's Class Entry, not at its upload:
with a lifecycle transition a version enters DEEP_ARCHIVE days after it was
written.

The Class Entry is ``archive_versions.storage_class_since`` when recorded.
Otherwise it is *estimated* from the Storage Plan, and every result says so:

- a version in a class its Storage Plan transitions to entered it
  ``created_at`` + the transition's days + one day (lifecycle rules run once a
  day); a version in any other class entered it when it was written;
- a version written straight into its class entered it when it was written:
  a copy (``copy_attempt_id``: rename, storage-class change, Vault move) and
  an object under 128 KiB in a class other than STANDARD, which no lifecycle
  rule moves there;
- the current version of a file, or the one replacing it, still recorded in
  an earlier class is expected in the colder class of the Storage Plan once
  that transition is due, because the change may simply not be observed yet.
  A superseded version never is: the lifecycle has no noncurrent-version
  transitions. Objects under 128 KiB never transition with a lifecycle rule:
  they stay where they were written.

Every amount is a Cost Estimate from the bundled AWS list prices
(``cost_estimates``): the remaining days of the minimum, times the size,
times the class's monthly rate, with a month of 30 days.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..errors import VaultNotFound
from .cost_estimates import PriceList, list_prices
from .lifecycle_profiles import ALLOWED_TARGETS, LifecycleProfile
from .vault_moves import MINIMUM_DAYS

# S3 lifecycle rules do not transition objects smaller than this by default.
SMALL_OBJECT_BYTES = 128 * 1024
# A lifecycle transition runs once a day: the estimate adds a day of margin.
TRANSITION_MARGIN = timedelta(days=1)
# AWS bills the remaining days at the class's monthly rate, a month being 30 days.
DAYS_PER_MONTH = 30
_DAY_SECONDS = 86400.0


def _aware(value: Any) -> datetime | None:
    if value is None:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True)
class ClassEntry:
    """When a version entered the class whose minimum applies to it.

    ``storage_class`` is the recorded class, or the colder class the Storage
    Plan expects (``expected``) when the transition is due but not observed.
    ``estimated``: ``since`` comes from the Storage Plan, not a record.
    """

    storage_class: str
    since: datetime | None
    estimated: bool
    expected: bool = False

    @property
    def minimum_days(self) -> int:
        return MINIMUM_DAYS.get(self.storage_class, 0)

    @property
    def minimum_until(self) -> datetime | None:
        """When the Minimum Storage Duration ends; None for a class without one."""
        if not self.minimum_days or self.since is None:
            return None
        return self.since + timedelta(days=self.minimum_days)

    def remaining_days(self, at: datetime) -> float:
        until = self.minimum_until
        if until is None:
            return 0.0
        return max(0.0, (until - at).total_seconds() / _DAY_SECONDS)

    def as_dict(self) -> dict[str, Any]:
        return {"storage_class": self.storage_class, "since": _iso(self.since),
                "estimated": self.estimated, "expected": self.expected,
                "minimum_days": self.minimum_days, "minimum_until": _iso(self.minimum_until)}


def class_entry(*, storage_class: str | None, since: Any, created_at: Any, size: int | None,
                plan: LifecycleProfile, now: datetime, project: bool = True,
                current: bool = True, copied: bool = False) -> ClassEntry:
    """The Class Entry of one version at ``now``.

    ``project`` lets the Storage Plan move a version recorded in an earlier
    class to the colder class it should be in by now; it is off when the class
    was observed just now (a live listing), which is already the truth.
    ``current``: the version is the current one of its file or the one
    replacing it; only those transition, a superseded version never does.
    ``copied``: the version was written by a server-side copy, straight into
    its class.
    """
    current_class = (storage_class or "STANDARD").upper()
    created = _aware(created_at)
    recorded = _aware(since)
    small = size is not None and int(size) < SMALL_OBJECT_BYTES
    if (project and current and created is not None and size is not None and not small):
        due = [step for step in plan.transitions
               if step.storage_class in ALLOWED_TARGETS.get(current_class, ())
               and created + timedelta(days=step.days) + TRANSITION_MARGIN <= now]
        if due:
            step = due[-1]
            entered = created + timedelta(days=step.days) + TRANSITION_MARGIN
            if recorded is not None:
                # It cannot have entered the colder class before the earlier one.
                entered = max(entered, recorded)
            return ClassEntry(step.storage_class, min(entered, now), True, True)
    if recorded is not None:
        return ClassEntry(current_class, recorded, False)
    if created is None:
        return ClassEntry(current_class, None, True)
    if copied or (small and current_class != "STANDARD"):
        # Written straight into its class: no lifecycle transition brought it.
        return ClassEntry(current_class, created, True)
    for step in plan.transitions:
        if step.storage_class == current_class:
            entered = created + timedelta(days=step.days) + TRANSITION_MARGIN
            # Already observed in that class: a future entry is impossible, so
            # it was written straight into it.
            return ClassEntry(current_class, entered if entered <= now else created, True)
    return ClassEntry(current_class, created, True)


def remaining_cost(entry: ClassEntry, size: int, at: datetime, prices: PriceList) -> float:
    """What deleting at ``at`` still bills for the rest of the minimum (estimate)."""
    days = entry.remaining_days(at)
    if days <= 0:
        return 0.0
    rate = float(prices.storage_rates.get(entry.storage_class, 0.0))
    return max(0, int(size)) / (1024 ** 3) * rate * days / DAYS_PER_MONTH


def _pricing(prices: PriceList) -> dict[str, Any]:
    return {"currency": prices.currency, "pricing_effective_at": prices.effective_at,
            "region": prices.region, "disclaimer": prices.assumptions.get("disclaimer")}


def _rows_entries(rows: Iterable[dict[str, Any]], plan: LifecycleProfile, now: datetime,
                  project: bool) -> Iterable[tuple[dict[str, Any], ClassEntry]]:
    for row in rows:
        yield row, class_entry(
            storage_class=row.get("storage_class"), since=row.get("storage_class_since"),
            created_at=row.get("version_created_at", row.get("created_at")),
            size=row.get("object_size", row.get("size")), plan=plan, now=now, project=project,
            current=bool(row.get("is_current", True)), copied=bool(row.get("copied", False)))


def deletion_summary(rows: Iterable[dict[str, Any]], *, plan: LifecycleProfile, at: datetime,
                     now: datetime | None = None, prices: PriceList | None = None) -> dict[str, Any]:
    """The versions a deletion at ``at`` would remove while still within their
    Minimum Storage Duration: what they still bill, and from when deleting them
    would start to save (the last minimum to end).

    Each row has ``storage_class``, ``storage_class_since``, ``created_at``
    (or ``version_created_at``) and ``size``; optionally ``is_current``
    (default true: the current or replacing version of its file) and
    ``copied`` (default false: written by a server-side copy).
    """
    prices = prices or list_prices()
    moment = now or datetime.now(timezone.utc)
    classes: dict[str, dict[str, Any]] = {}
    count = size = estimated = 0
    cost = 0.0
    savings_from: datetime | None = None
    for row, entry in _rows_entries(rows, plan, moment, True):
        if entry.remaining_days(at) <= 0:
            continue
        bytes_ = int(row.get("size") or 0)
        amount = remaining_cost(entry, bytes_, at, prices)
        item = classes.setdefault(entry.storage_class, {
            "storage_class": entry.storage_class, "minimum_days": entry.minimum_days,
            "count": 0, "bytes": 0, "remaining_cost": 0.0})
        item["count"] += 1
        item["bytes"] += bytes_
        item["remaining_cost"] += amount
        count += 1
        size += bytes_
        cost += amount
        estimated += entry.estimated
        until = entry.minimum_until
        if until is not None and (savings_from is None or until > savings_from):
            savings_from = until
    by_class = sorted(classes.values(), key=lambda item: item["storage_class"])
    for item in by_class:
        item["remaining_cost"] = round(item["remaining_cost"], 6)
    return {"count": count, "bytes": size, "remaining_cost": round(cost, 6),
            "savings_from": _iso(savings_from), "estimated_count": estimated,
            "by_storage_class": by_class, "evaluated_at": at.isoformat(), **_pricing(prices)}


def beyond_minimum(row: dict[str, Any], *, plan: LifecycleProfile, at: datetime,
                   now: datetime) -> bool:
    """True when deleting ``row`` at ``at`` bills nothing for its minimum."""
    _, entry = next(iter(_rows_entries([row], plan, now, True)))
    return entry.remaining_days(at) <= 0


def vault_summary(rows: Iterable[dict[str, Any]], *, plan: LifecycleProfile, now: datetime,
                  prices: PriceList | None = None) -> dict[str, Any]:
    """The Vault page summary: per storage class, the versions within and
    beyond their Minimum Storage Duration; the minimum still to pay; and, month
    by month, when the versions within it are released.

    Rows may be aggregated: ``count`` versions of ``size`` bytes in total with
    the same class, Class Entry and creation time (``object_size`` is the size
    of one, for the 128 KiB rule).
    """
    prices = prices or list_prices()
    classes: dict[str, dict[str, Any]] = {}
    months: dict[str, dict[str, int]] = {}
    total_cost = 0.0
    estimated = 0
    for row, entry in _rows_entries(rows, plan, now, True):
        count = int(row.get("count") or 1)
        bytes_ = int(row.get("size") or 0)
        item = classes.setdefault(entry.storage_class, {
            "storage_class": entry.storage_class, "minimum_days": entry.minimum_days,
            "within": {"count": 0, "bytes": 0}, "beyond": {"count": 0, "bytes": 0},
            "remaining_cost": 0.0, "estimated_count": 0})
        within = entry.remaining_days(now) > 0
        side = item["within" if within else "beyond"]
        side["count"] += count
        side["bytes"] += bytes_
        if entry.estimated and entry.minimum_days:
            item["estimated_count"] += count
            estimated += count
        until = entry.minimum_until
        if within and until is not None:
            amount = remaining_cost(entry, bytes_, now, prices)
            item["remaining_cost"] += amount
            total_cost += amount
            month = months.setdefault(until.astimezone(timezone.utc).strftime("%Y-%m"),
                                      {"count": 0, "bytes": 0})
            month["count"] += count
            month["bytes"] += bytes_
    by_class = sorted(classes.values(), key=lambda item: item["storage_class"])
    for item in by_class:
        item["remaining_cost"] = round(item["remaining_cost"], 6)
    return {
        "by_storage_class": by_class,
        "remaining_cost": round(total_cost, 6),
        "releases": [{"month": month, **values} for month, values in sorted(months.items())],
        "estimated_count": estimated,
        "evaluated_at": now.isoformat(),
        **_pricing(prices),
    }


# The billed versions of a Vault, grouped so a large Vault reads few rows:
# same class, Class Entry hour, creation hour, 128 KiB side, current-or-
# replacing role (only those transition) and copy (written straight into its
# class). An hour of imprecision is far below the one day of the estimate.
_VAULT_VERSIONS_SQL = """
    SELECT COALESCE(upper(av.storage_class), 'STANDARD') AS storage_class,
           date_trunc('hour', av.storage_class_since) AS storage_class_since,
           date_trunc('hour', av.created_at) AS created_at,
           CASE WHEN av.size >= %s THEN %s ELSE 0 END AS object_size,
           COALESCE(av.id IN (f.current_version_id, f.replacing_version_id), false) AS is_current,
           av.copy_attempt_id IS NOT NULL AS copied,
           count(*) AS count, COALESCE(sum(av.size), 0) AS size
    FROM archive_versions av JOIN files f ON f.id = av.file_id
    WHERE f.vault_id = %s AND av.availability <> 'missing'
    GROUP BY 1, 2, 3, 4, 5, 6
"""


def load_vault_summary(connection: Any, vault_id: int, *,
                       now: datetime | None = None) -> dict[str, Any]:
    from .lifecycle_profiles import profile_from_json

    vault = connection.execute(
        "SELECT storage_plan::text AS plan FROM vaults WHERE id=%s", (vault_id,)).fetchone()
    if vault is None:
        raise VaultNotFound("api.minimum_storage.not_found")
    plan = profile_from_json(vault["plan"])
    rows = connection.execute(
        _VAULT_VERSIONS_SQL, (SMALL_OBJECT_BYTES, SMALL_OBJECT_BYTES, vault_id)).fetchall()
    moment = now or datetime.now(timezone.utc)
    return {"vault_id": vault_id, **vault_summary(rows, plan=plan, now=moment)}


def observed_since_sql(*, old_class: str, new_class: str, old_since: str, observed_at: str,
                       created_at: str, size: str, plan: str) -> str:
    """SQL for ``storage_class_since`` after an observation of ``new_class``.

    Every argument is an SQL expression without placeholders (each may be
    repeated): the recorded class, Class Entry, creation and size of the
    version, the observed class and instant, and the Vault's ``storage_plan``.

    It changes only when the observed class differs from a recorded one, and
    then takes the observation's instant; an unknown earlier class tells
    nothing about when the version entered the observed one. Classes compare
    case-insensitively.

    A version with no recorded Class Entry (written before #731) observed late
    in the class its Storage Plan predicts by now (the last transition whose
    days + 1 have elapsed since ``created_at``; never an object under
    128 KiB) keeps none: the plan's estimate, ``created_at`` + days + 1,
    stays closer to the truth than the instant of a late observation.
    """
    predicted = (
        f"(SELECT upper(step->>'storage_class')"
        f" FROM jsonb_array_elements(COALESCE({plan}->'transitions', '[]'::jsonb)) AS step"
        f" WHERE {created_at} + make_interval(days => (step->>'days')::int + 1) <= {observed_at}"
        f" ORDER BY (step->>'days')::int DESC LIMIT 1)")
    return (f"CASE WHEN {old_class} IS NOT NULL AND {new_class} IS NOT NULL"
            f" AND upper({old_class}) <> upper({new_class})"
            f" AND NOT ({old_since} IS NULL AND {size} >= {SMALL_OBJECT_BYTES}"
            f" AND upper({new_class}) IS NOT DISTINCT FROM {predicted})"
            f" THEN {observed_at} ELSE {old_since} END")
