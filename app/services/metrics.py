"""In-process Prometheus-style metrics with low-cardinality labels (issue #16)."""
from __future__ import annotations

import threading
from typing import Any

_FORBIDDEN_LABELS = frozenset(
    {
        "path",
        "filepath",
        "file",
        "username",
        "user",
        "user_id",
        "email",
        "token",
        "ip",
        "object_key",
        "key",
    }
)

_ALLOWED_METRICS = {
    "jobs_completed_total": "counter",
    "jobs_failed_total": "counter",
    "jobs_retries_total": "counter",
    "verification_failures_total": "counter",
    "restore_state_total": "counter",
    "lifecycle_reconciliations_total": "counter",
    "notification_deliveries_total": "counter",
    "worker_errors_total": "counter",
    "metadata_backups_total": "counter",
    "metadata_backup_verifications_total": "counter",
    "queue_depth": "gauge",
    "worker_up": "gauge",
    "metadata_backup_last_success_unixtime": "gauge",
    # Bounded archive-stats / filesystem-health observability (issue #228).
    "stats_last_duration_seconds": "gauge",
    "filesystem_health_last_duration_seconds": "gauge",
    "filesystem_health_findings": "gauge",
    "filesystem_health_cache_age_seconds": "gauge",
    "filesystem_health_status": "gauge",
}

_lock = threading.Lock()
_counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
_gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}


def _normalize_labels(labels: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    for name in labels:
        if name.lower() in _FORBIDDEN_LABELS:
            raise ValueError(f"high-cardinality or sensitive metric label: {name}")
    return tuple(sorted((str(key), str(value)) for key, value in labels.items()))


def inc(name: str, amount: float = 1.0, **labels: Any) -> None:
    if name not in _ALLOWED_METRICS or _ALLOWED_METRICS[name] != "counter":
        raise ValueError(f"unknown counter metric: {name}")
    key = (name, _normalize_labels(labels))
    with _lock:
        _counters[key] = _counters.get(key, 0.0) + amount


def set_gauge(name: str, value: float, **labels: Any) -> None:
    if name not in _ALLOWED_METRICS or _ALLOWED_METRICS[name] != "gauge":
        raise ValueError(f"unknown gauge metric: {name}")
    key = (name, _normalize_labels(labels))
    with _lock:
        _gauges[key] = float(value)


def reset_for_tests() -> None:
    with _lock:
        _counters.clear()
        _gauges.clear()


def render_prometheus() -> str:
    """Render the current metric snapshot in Prometheus text format."""
    lines: list[str] = []
    with _lock:
        counter_items = sorted(_counters.items())
        gauge_items = sorted(_gauges.items())

    seen_types: set[str] = set()
    for (name, labels), value in counter_items:
        if name not in seen_types:
            lines.append(f"# TYPE {name} counter")
            seen_types.add(name)
        lines.append(_sample_line(name, labels, value))
    for (name, labels), value in gauge_items:
        if name not in seen_types:
            lines.append(f"# TYPE {name} gauge")
            seen_types.add(name)
        lines.append(_sample_line(name, labels, value))
    # Always advertise worker_up even when never set, so scrapers see the series.
    if "worker_up" not in seen_types:
        lines.append("# TYPE worker_up gauge")
        lines.append("worker_up 0")
    return "\n".join(lines) + "\n"


def _sample_line(
    name: str, labels: tuple[tuple[str, str], ...], value: float
) -> str:
    if not labels:
        return f"{name} {_format_value(value)}"
    rendered = ",".join(f'{key}="{_escape(val)}"' for key, val in labels)
    return f"{name}{{{rendered}}} {_format_value(value)}"


def _format_value(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
