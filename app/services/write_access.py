"""Whether FrostVault's own identity may write a folder, and why not (#681).

A local operation (Recovery in place, Local Copy cleanup, rename, reconnect)
creates, renames or removes entries in a folder. Without write and search
permission on that folder it fails with ``EACCES``; root without
``CAP_DAC_OVERRIDE`` is no exception. The check runs before the operation, so
the Job says which folder, whose it is and with which mode, instead of a raw
``Errno 13`` — and a Recovery says it before asking S3 for a restore.

The check uses the effective identity and lets the kernel decide (``access(2)``
or ``faccessat(AT_EACCESS)``), so capabilities and supplementary groups count.
"""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Any

from .database_retry import exception_chain

MESSAGE_KEY = "job.destination_not_writable"


def runtime_identity() -> dict[str, str]:
    """``uid:gid`` and the supplementary groups of this process, as shown text."""
    gid = os.getegid()
    groups = sorted({group for group in os.getgroups() if group != gid})
    return {
        "identity": f"{os.geteuid()}:{gid}",
        "groups": ", ".join(str(group) for group in groups) or "—",
    }


def _writable(directory: Path) -> bool:
    mode = os.W_OK | os.X_OK
    if (os.getuid(), os.getgid()) == (os.geteuid(), os.getegid()):
        # The real ids are the effective ones: access(2) is the kernel's own
        # check, capabilities included.
        return os.access(directory, mode)
    return os.access(directory, mode, effective_ids=True)


def owner_and_mode(info: os.stat_result) -> dict[str, str]:
    return {"owner": f"{info.st_uid}:{info.st_gid}", "mode": f"{stat.S_IMODE(info.st_mode):04o}"}


class DestinationNotWritable(RuntimeError):
    """A folder FrostVault must write is not writable by its identity."""

    message_key = MESSAGE_KEY

    def __init__(self, directory: Path, info: os.stat_result | None = None) -> None:
        self.directory = directory
        params: dict[str, Any] = {"path": str(directory), **runtime_identity()}
        if info is None:
            try:
                info = directory.stat()
            except OSError:
                info = None
        params.update(owner_and_mode(info) if info is not None else {"owner": "?", "mode": "?"})
        self.message_params = params
        super().__init__(
            f"The folder {directory} is not writable by FrostVault's user "
            f"({params['identity']}): owner {params['owner']}, mode {params['mode']}"
        )


def nearest_existing_directory(path: Path, root: Path) -> Path:
    """The folder that receives ``path``'s entry: its parent, or the closest
    ancestor that exists (a Recovery creates the missing ones), never above root."""
    directory = path.parent
    while directory != root and root in directory.parents and not directory.exists():
        directory = directory.parent
    return directory


def require_writable_directory(directory: Path) -> None:
    """Raise :class:`DestinationNotWritable` unless the folder may be written."""
    try:
        info = directory.stat()
    except FileNotFoundError:
        return  # Nothing to check yet; the operation reports a missing folder itself.
    except PermissionError:
        raise DestinationNotWritable(directory) from None
    if not stat.S_ISDIR(info.st_mode):
        return
    if not _writable(directory):
        raise DestinationNotWritable(directory, info)


def require_writable_parent(root: str | Path, entry: Path) -> None:
    """The folder that would receive ``entry`` must be writable and searchable."""
    require_writable_directory(nearest_existing_directory(entry, Path(root)))


def from_permission_error(exc: BaseException) -> DestinationNotWritable | None:
    """Map an unexpected ``EACCES`` of a local operation to the same message.

    The folder named is the parent of the path the error reports (the
    destination of a rename when there is one).
    """
    for inner in exception_chain(exc):
        if isinstance(inner, DestinationNotWritable):
            return inner
        if isinstance(inner, PermissionError) and inner.errno == errno.EACCES:
            name = inner.filename2 or inner.filename
            if not name:
                return None
            return DestinationNotWritable(Path(os.fsdecode(name)).parent)
    return None

