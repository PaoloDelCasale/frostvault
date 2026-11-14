"""Root without root privileges: the PUID=0 contract of #679.

FrostVault may run as uid 0 so it can read and recover files that containers
running as root (Immich, Frigate) wrote with ``root:root 755/644``. That is
safe only while uid 0 is an ordinary owner: without the capabilities that
bypass file permissions, and with ``no-new-privileges`` so none can be gained.
The entrypoint runs this module before starting anything as root; it exits 0
when the contract holds, else prints the reason and exits 64. The app logs
``ROOT_WARNING`` at start whenever it runs as uid 0.

The module imports nothing from the app, so it runs before any configuration.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Capability numbers from linux/capability.h.
FORBIDDEN_CAPABILITIES = {
    1: "CAP_DAC_OVERRIDE",
    2: "CAP_DAC_READ_SEARCH",
    3: "CAP_FOWNER",
    21: "CAP_SYS_ADMIN",
}

ROOT_WARNING = (
    "WARNING: FrostVault runs as root (PUID=0). It has no capability that "
    "bypasses file permissions, so it reads and recovers files owned by root "
    "but cannot override other owners' permissions. See docs/filesystem-permissions.md."
)


def _status_fields(status: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in status.splitlines():
        name, separator, value = line.partition(":")
        if separator:
            fields[name.strip()] = value.strip()
    return fields


def root_runtime_problems(status: str) -> list[str]:
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
    present = [name for bit, name in sorted(FORBIDDEN_CAPABILITIES.items()) if held >> bit & 1]
    if present:
        problems.append(
            "it still holds " + ", ".join(present)
            + ", which bypass file permissions; keep cap_drop: [ALL] and add back only CHOWN and KILL"
        )
    if fields.get("NoNewPrivs") != "1":
        problems.append('no-new-privileges is not active; add security_opt: ["no-new-privileges:true"]')
    return problems


def main(status_path: str = "/proc/self/status") -> int:
    try:
        status = Path(status_path).read_text(encoding="ascii")
    except OSError:
        status = ""
    problems = root_runtime_problems(status)
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
