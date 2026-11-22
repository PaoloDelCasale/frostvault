"""The known cause of a failed source scan, as a stable code (#711).

A source error is stored as English text, sometimes with a path. The Owner
reads it translated: each known cause has a code with its own message, and an
unknown one keeps the stored text. The code is derived from the text, so it
also names the cause of an error recorded by an earlier release.
"""
from __future__ import annotations

from .operation_policies import SOURCE_SCAN_INCOMPLETE, SOURCE_WATCHER_DENIED

# Checked in order: the first matching fragment names the cause.
_CAUSES = (
    ("incomplete", (SOURCE_SCAN_INCOMPLETE.lower(),)),
    ("watcher_denied", (SOURCE_WATCHER_DENIED.lower(),)),
    ("superseded", ("superseded", "quiesced")),
    ("root_changed", ("identity mismatch", "identity is ambiguous", "identity is inconsistent",
                      "root changed before traversal", "source volume changed")),
    ("tree_changed", ("changed during traversal", "changed during local scan",
                      "changed while it was being pinned", "escapes the pinned root")),
    ("root_unavailable", ("unavailable", "no longer accessible", "could not be pinned")),
)


def source_error_cause(message: str | None) -> str | None:
    """The cause code of a stored source error, or None when it is unknown."""
    if not message:
        return None
    text = message.lower()
    return next((code for code, fragments in _CAUSES
                 if any(fragment in text for fragment in fragments)), None)
