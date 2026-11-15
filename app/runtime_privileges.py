"""Root without root privileges: the PUID=0 contract of #679.

FrostVault may run as uid 0 so it can read and recover files that containers
running as root (Immich, Frigate) wrote with ``root:root 755/644``. That is
safe only while uid 0 is an ordinary owner: without the capabilities that
bypass file permissions, and with ``no-new-privileges`` so none can be gained.
The entrypoint runs this module before starting anything as root; it exits 0
when the contract holds, else prints the reason and exits 64. The app logs
:func:`startup_warnings` at start whenever it runs as uid 0.

``FROSTVAULT_ALLOW_DAC_OVERRIDE=1`` (#681, ADR-0042) is the one explicit
exception, for shares with several owners: root may then hold
``CAP_DAC_OVERRIDE`` and ``CAP_DAC_READ_SEARCH``. ``CAP_FOWNER`` and
``CAP_SYS_ADMIN`` stay refused, and so does a missing ``no-new-privileges``.

The module imports nothing from the app, so it runs before any configuration.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

# Capability numbers from linux/capability.h.
FORBIDDEN_CAPABILITIES = {
    1: "CAP_DAC_OVERRIDE",
    2: "CAP_DAC_READ_SEARCH",
    3: "CAP_FOWNER",
    21: "CAP_SYS_ADMIN",
}
# Allowed only with FROSTVAULT_ALLOW_DAC_OVERRIDE=1.
DAC_CAPABILITIES = {1: "CAP_DAC_OVERRIDE", 2: "CAP_DAC_READ_SEARCH"}
ALLOW_DAC_OVERRIDE_ENV = "FROSTVAULT_ALLOW_DAC_OVERRIDE"

ROOT_WARNING = (
    "WARNING: FrostVault runs as root (PUID=0). It has no capability that "
    "bypasses file permissions, so it reads and recovers files owned by root "
    "but cannot override other owners' permissions. See docs/filesystem-permissions.md."
)
DAC_WARNING = (
    "WARNING: FrostVault runs as root (PUID=0) with CAP_DAC_OVERRIDE and "
    "CAP_DAC_READ_SEARCH (FROSTVAULT_ALLOW_DAC_OVERRIDE=1): it reads, writes and "
    "deletes files of every owner mounted into the container, bypassing their "
    "permissions. See docs/filesystem-permissions.md."
)
NO_GROUPS_WARNING = (
    "WARNING: FrostVault runs as root without supplementary groups: files of other "
    "owners readable only by their group (for example 0640 files of the users group) "
    "stay unreadable. Add group_add: [\"100\"] (or your PGID) to the service. "
    "See docs/filesystem-permissions.md."
)


def dac_override_allowed(environ: Mapping[str, str] | None = None) -> bool:
    return (os.environ if environ is None else environ).get(ALLOW_DAC_OVERRIDE_ENV) == "1"


def _status_fields(status: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in status.splitlines():
        name, separator, value = line.partition(":")
        if separator:
            fields[name.strip()] = value.strip()
    return fields


def _capabilities(status: str, field: str) -> int | None:
    try:
        return int(_status_fields(status)[field], 16)
    except (KeyError, ValueError):
        return None


def holds_dac_override(status: str) -> bool:
    """Whether the effective set bypasses file read, write or search checks."""
    effective = _capabilities(status, "CapEff")
    return bool(effective and any(effective >> bit & 1 for bit in DAC_CAPABILITIES))


def root_runtime_problems(status: str, *, allow_dac_override: bool = False) -> list[str]:
    """Why a root process with this ``/proc/<pid>/status`` must not start.

    Both the effective and the bounding set are checked: a root process can
    regain at ``execve`` any capability still in its bounding set.
    """
    fields = _status_fields(status)
    problems: list[str] = []
    try:
        held = int(fields["CapEff"], 16) | int(fields["CapBnd"], 16)
    except (KeyError, ValueError):
        return ["the process capabilities cannot be read from /proc/self/status"]
    forbidden = {bit: name for bit, name in FORBIDDEN_CAPABILITIES.items()
                 if not (allow_dac_override and bit in DAC_CAPABILITIES)}
    present = [name for bit, name in sorted(forbidden.items()) if held >> bit & 1]
    if present:
        problems.append(
            "it still holds " + ", ".join(present)
            + ", which bypass file permissions; keep cap_drop: [ALL] and add back only CHOWN and KILL"
            + ("" if allow_dac_override else
               " (DAC_OVERRIDE and DAC_READ_SEARCH only with FROSTVAULT_ALLOW_DAC_OVERRIDE=1)")
        )
    if fields.get("NoNewPrivs") != "1":
        problems.append('no-new-privileges is not active; add security_opt: ["no-new-privileges:true"]')
    return problems


def startup_warnings(status: str, *, euid: int, gid: int, groups: Sequence[int]) -> list[str]:
    """What the startup log says about a root runtime (empty when not root)."""
    if euid != 0:
        return []
    if holds_dac_override(status):
        return [DAC_WARNING]
    warnings = [ROOT_WARNING]
    if not {group for group in groups if group != gid}:
        warnings.append(NO_GROUPS_WARNING)
    return warnings


def read_status(status_path: str = "/proc/self/status") -> str:
    try:
        return Path(status_path).read_text(encoding="ascii")
    except OSError:
        return ""


def main(status_path: str = "/proc/self/status") -> int:
    problems = root_runtime_problems(read_status(status_path), allow_dac_override=dac_override_allowed())
    if problems:
        print(
            "ERROR: PUID=0 runs FrostVault as root only without root privileges, but "
            + "; ".join(problems) + ".",
            file=sys.stderr,
        )
        return 64
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:2]))
