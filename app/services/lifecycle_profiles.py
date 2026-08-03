"""Guided lifecycle profiles for Archive Version storage-class transitions."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

SUPPORTED_STORAGE_CLASSES = (
    "STANDARD_IA",
    "ONEZONE_IA",
    "GLACIER_IR",
    "GLACIER",
    "DEEP_ARCHIVE",
)

MIN_TRANSITION_DAYS: dict[str, int] = {
    "STANDARD_IA": 30,
    "ONEZONE_IA": 30,
    "GLACIER_IR": 0,
    "GLACIER": 90,
    "DEEP_ARCHIVE": 180,
}

COST_WARNING_CLASSES = frozenset({"GLACIER_IR", "GLACIER", "DEEP_ARCHIVE"})


@dataclass(frozen=True)
class LifecycleTransition:
    days: int
    storage_class: str


@dataclass(frozen=True)
class LifecycleProfile:
    transitions: tuple[LifecycleTransition, ...] = ()
    expiration_days: int | None = None
    noncurrent_expiration_days: int | None = None
    # NoncurrentDays → StorageClass for retained noncurrent Archive Versions
    # after a Delete Marker hides the current key (issue #10).
    noncurrent_transitions: tuple[LifecycleTransition, ...] = ()


@dataclass(frozen=True)
class ProfileValidation:
    ok: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


GUIDED_PROFILES: dict[str, LifecycleProfile] = {
    "standard_only": LifecycleProfile(),
    "ia_after_30": LifecycleProfile(
        transitions=(LifecycleTransition(days=30, storage_class="STANDARD_IA"),)
    ),
    "archive_tiered": LifecycleProfile(
        transitions=(
            LifecycleTransition(days=30, storage_class="STANDARD_IA"),
            LifecycleTransition(days=90, storage_class="GLACIER_IR"),
            LifecycleTransition(days=365, storage_class="DEEP_ARCHIVE"),
        ),
        noncurrent_transitions=(
            LifecycleTransition(days=180, storage_class="DEEP_ARCHIVE"),
        ),
    ),
}


def profile_to_json(profile: LifecycleProfile) -> str:
    payload = {
        "transitions": [
            {"days": transition.days, "storage_class": transition.storage_class}
            for transition in profile.transitions
        ],
        "expiration_days": profile.expiration_days,
        "noncurrent_expiration_days": profile.noncurrent_expiration_days,
        "noncurrent_transitions": [
            {"days": transition.days, "storage_class": transition.storage_class}
            for transition in profile.noncurrent_transitions
        ],
    }
    return json.dumps(payload, sort_keys=True)


def profile_from_json(value: str | None) -> LifecycleProfile | None:
    if not value:
        return None
    payload = json.loads(value)
    transitions = tuple(
        LifecycleTransition(
            days=int(item["days"]),
            storage_class=str(item["storage_class"]).upper(),
        )
        for item in payload.get("transitions", [])
    )
    noncurrent_transitions = tuple(
        LifecycleTransition(
            days=int(item["days"]),
            storage_class=str(item["storage_class"]).upper(),
        )
        for item in payload.get("noncurrent_transitions", [])
    )
    expiration_days = payload.get("expiration_days")
    noncurrent_expiration_days = payload.get("noncurrent_expiration_days")
    return LifecycleProfile(
        transitions=transitions,
        expiration_days=int(expiration_days) if expiration_days is not None else None,
        noncurrent_expiration_days=(
            int(noncurrent_expiration_days)
            if noncurrent_expiration_days is not None
            else None
        ),
        noncurrent_transitions=noncurrent_transitions,
    )


def validate_lifecycle_profile(profile: LifecycleProfile) -> ProfileValidation:
    errors: list[str] = []
    warnings: list[str] = []
    previous_days = 0
    # Lifecycle targets always start deeper than STANDARD. IA variants share band 1.
    previous_depth = 0

    for transition in profile.transitions:
        storage_class = transition.storage_class.upper()
        if storage_class not in SUPPORTED_STORAGE_CLASSES:
            errors.append(f"Unsupported storage class: {storage_class}")
            continue
        if transition.days <= previous_days:
            errors.append("Transition days must strictly increase across the profile")
        from .storage_classes import storage_class_depth

        depth = storage_class_depth(storage_class)
        if depth <= previous_depth:
            errors.append(
                "Transition storage classes must deepen "
                f"(got {storage_class} after a class of equal or colder depth)"
            )
        minimum_days = MIN_TRANSITION_DAYS[storage_class]
        if transition.days < minimum_days:
            errors.append(
                f"{storage_class} transitions require at least {minimum_days} days"
            )
        if storage_class in COST_WARNING_CLASSES:
            warnings.append(
                f"Transition to {storage_class} incurs retrieval charges and minimum "
                "storage-duration billing even if objects are deleted early"
            )
        previous_days = transition.days
        previous_depth = depth

    if profile.expiration_days is not None and profile.expiration_days <= 0:
        errors.append("expiration_days must be positive when set")
    if (
        profile.noncurrent_expiration_days is not None
        and profile.noncurrent_expiration_days <= 0
    ):
        errors.append("noncurrent_expiration_days must be positive when set")

    previous_noncurrent_days = 0
    previous_noncurrent_depth = 0
    for transition in profile.noncurrent_transitions:
        storage_class = transition.storage_class.upper()
        if storage_class not in SUPPORTED_STORAGE_CLASSES:
            errors.append(f"Unsupported noncurrent storage class: {storage_class}")
            continue
        if transition.days <= previous_noncurrent_days:
            errors.append(
                "Noncurrent transition days must strictly increase across the profile"
            )
        from .storage_classes import storage_class_depth

        depth = storage_class_depth(storage_class)
        if depth <= previous_noncurrent_depth:
            errors.append(
                "Noncurrent transition storage classes must deepen "
                f"(got {storage_class} after a class of equal or colder depth)"
            )
        minimum_days = MIN_TRANSITION_DAYS[storage_class]
        if transition.days < minimum_days:
            errors.append(
                f"Noncurrent {storage_class} transitions require at least "
                f"{minimum_days} days"
            )
        if storage_class in COST_WARNING_CLASSES:
            warnings.append(
                f"Noncurrent transition to {storage_class} incurs retrieval charges "
                "and minimum storage-duration billing"
            )
        previous_noncurrent_days = transition.days
        previous_noncurrent_depth = depth

    return ProfileValidation(
        ok=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def guided_profile(name: str) -> LifecycleProfile:
    try:
        return GUIDED_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown guided lifecycle profile: {name}") from exc
