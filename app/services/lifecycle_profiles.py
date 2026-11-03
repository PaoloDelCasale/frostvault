"""One current-version transition chain per Vault; never cloud expiration."""
from __future__ import annotations

import json
from dataclasses import dataclass

SUPPORTED_STORAGE_CLASSES = (
    "STANDARD_IA", "ONEZONE_IA", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE",
)
# Minimum *source-class residence*, not object age measured from STANDARD.
MIN_SOURCE_RESIDENCE = {
    "STANDARD_IA": 30,
    "ONEZONE_IA": 30,
    "GLACIER_IR": 90,
    "GLACIER": 90,
}
COST_WARNING_CLASSES = frozenset({"GLACIER_IR", "GLACIER", "DEEP_ARCHIVE"})
ALLOWED_TARGETS = {
    "STANDARD": frozenset(SUPPORTED_STORAGE_CLASSES),
    "STANDARD_IA": frozenset({"ONEZONE_IA", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE"}),
    "ONEZONE_IA": frozenset({"GLACIER", "DEEP_ARCHIVE"}),
    "GLACIER_IR": frozenset({"GLACIER", "DEEP_ARCHIVE"}),
    "GLACIER": frozenset({"DEEP_ARCHIVE"}),
    "DEEP_ARCHIVE": frozenset(),
}


@dataclass(frozen=True)
class LifecycleTransition:
    days: int
    storage_class: str


@dataclass(frozen=True)
class LifecycleProfile:
    transitions: tuple[LifecycleTransition, ...] = ()


@dataclass(frozen=True)
class ProfileValidation:
    ok: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


GUIDED_PROFILES = {
    "standard_only": LifecycleProfile(),
    "economical": LifecycleProfile((
        LifecycleTransition(30, "STANDARD_IA"),
        LifecycleTransition(90, "GLACIER_IR"),
    )),
    "archive_tiered": LifecycleProfile((
        LifecycleTransition(30, "STANDARD_IA"),
        LifecycleTransition(90, "GLACIER_IR"),
        LifecycleTransition(365, "DEEP_ARCHIVE"),
    )),
    "deep_archive_early": LifecycleProfile((LifecycleTransition(0, "DEEP_ARCHIVE"),)),
}


def profile_to_dict(profile: LifecycleProfile) -> dict[str, list[dict[str, int | str]]]:
    return {
        "transitions": [
            {"days": step.days, "storage_class": step.storage_class}
            for step in profile.transitions
        ]
    }


def profile_to_json(profile: LifecycleProfile) -> str:
    return json.dumps(profile_to_dict(profile), sort_keys=True)


def profile_from_json(value: str | None) -> LifecycleProfile:
    if value is None:
        return LifecycleProfile()
    payload = json.loads(value)
    if not isinstance(payload, dict) or set(payload) != {"transitions"}:
        raise ValueError("A storage plan contains only current-version transitions")
    if not isinstance(payload["transitions"], list):
        raise ValueError("Transitions must be a list")
    steps = []
    for item in payload["transitions"]:
        if not isinstance(item, dict) or set(item) != {"days", "storage_class"}:
            raise ValueError("Each transition requires days and storage_class only")
        steps.append(LifecycleTransition(item["days"], item["storage_class"]))
    profile = LifecycleProfile(tuple(steps))
    validation = validate_lifecycle_profile(profile)
    if not validation.ok:
        raise ValueError("; ".join(validation.errors))
    return profile


def validate_lifecycle_profile(profile: LifecycleProfile) -> ProfileValidation:
    errors: list[str] = []
    warnings: list[str] = []
    previous_days = -1
    previous_class = "STANDARD"
    for step in profile.transitions:
        if type(step.days) is not int or step.days < 0:
            errors.append("Transition days must be nonnegative integers")
            continue
        if step.storage_class not in SUPPORTED_STORAGE_CLASSES:
            errors.append(f"Unsupported storage class: {step.storage_class}")
            continue
        if step.days <= previous_days:
            errors.append("Transition days must strictly increase")
        if step.storage_class not in ALLOWED_TARGETS[previous_class]:
            errors.append(
                f"Unsupported transition from {previous_class} to {step.storage_class}"
            )
        if step.storage_class in {"STANDARD_IA", "ONEZONE_IA"} and step.days < 30:
            errors.append("IA transitions require object age of at least 30 days")
        residence = MIN_SOURCE_RESIDENCE.get(previous_class, 0)
        if step.days - previous_days < residence:
            errors.append(
                f"{previous_class} requires at least {residence} days "
                "before the next transition"
            )
        if step.storage_class != "DEEP_ARCHIVE":
            warnings.append(
                "Transition and retrieval fees and minimum storage-duration "
                "charges may apply"
            )
        else:
            warnings.append(
                "DEEP_ARCHIVE requires hours to restore and bills at least "
                "180 days even after early replacement or deletion"
            )
        previous_days, previous_class = step.days, step.storage_class
    return ProfileValidation(not errors, tuple(errors), tuple(dict.fromkeys(warnings)))


def guided_profile(name: str) -> LifecycleProfile:
    try:
        return GUIDED_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown storage plan: {name}") from exc
