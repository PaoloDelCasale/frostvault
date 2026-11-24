"""The "restore required" truth table on every storage class, at every site (#804).

One predicate, ``s3.storage_class_requires_restore``, decides whether a
version must be restored before it can be read. Each site that decides it is
checked here on every S3 storage class, ``None`` and unknown strings, against
the table it had before it used the shared predicate. The Reconnect site of
``app.storage`` is checked in ``tests.test_vault_reconnect``.
"""
from __future__ import annotations

import unittest

from app.services import s3, vault_moves
from app.services.lifecycle_profiles import ALLOWED_TARGETS, SUPPORTED_STORAGE_CLASSES

# Every S3 storage class, a missing one, an unknown one, and other spellings.
CLASSES = (
    "STANDARD", "STANDARD_IA", "ONEZONE_IA", "INTELLIGENT_TIERING", "GLACIER_IR",
    "GLACIER", "DEEP_ARCHIVE", "REDUCED_REDUNDANCY", "OUTPOSTS", "EXPRESS_ONEZONE",
    None, "", "FUTURE_CLASS", "glacier", "deep_archive", "Glacier_Ir",
)
# Exactly these spellings need a restore: the predicate compares case-blind.
REQUIRES_RESTORE = frozenset({"GLACIER", "DEEP_ARCHIVE", "glacier", "deep_archive"})
TARGETS = ("STANDARD", *SUPPORTED_STORAGE_CLASSES)
RESTORE_STATES = (None, "not_requested", "restoring", "available")
RESTORE_HEADERS = {
    None: "not_requested",
    'ongoing-request="true"': "restoring",
    'ongoing-request="false", expiry-date="Fri, 21 Dec 2012 00:00:00 GMT"': "available",
}


class PredicateTests(unittest.TestCase):
    def test_every_class(self):
        for storage_class in CLASSES:
            with self.subTest(storage_class=storage_class):
                self.assertEqual(s3.storage_class_requires_restore(storage_class),
                                 storage_class in REQUIRES_RESTORE)


class VaultMoveEligibilityTests(unittest.TestCase):
    """``vault_moves.eligibility`` on a verified, idle Current."""

    ROW = dict(busy=False, integrity="verified", availability="available",
               plaintext_sha256="a" * 64, size=1, provider_version_id="source")

    def test_every_class_target_and_restore_state(self):
        restore_required = 0
        for storage_class in CLASSES:
            current = (storage_class or "STANDARD").upper()
            for target in TARGETS:
                for restore_state in RESTORE_STATES:
                    if target not in ALLOWED_TARGETS.get(current, ()):
                        expected = "already_equal_or_colder"
                    elif storage_class in REQUIRES_RESTORE and restore_state != "available":
                        expected = "explicit_restore_required"
                        restore_required += 1
                    else:
                        expected = None
                    row = {**self.ROW, "storage_class": storage_class, "restore_state": restore_state}
                    with self.subTest(storage_class=storage_class, target=target,
                                      restore_state=restore_state):
                        self.assertEqual(vault_moves.eligibility(row, target), expected)
        # GLACIER and glacier to DEEP_ARCHIVE, three non-available states each.
        self.assertEqual(restore_required, 6)


class VaultMoveSourceCheckTests(unittest.TestCase):
    """``vault_moves.check_source``: the worker's recheck of the source HEAD."""

    def test_every_class_target_and_restore_header(self):
        restore_first = 0
        for storage_class in CLASSES:
            observed = (storage_class or "STANDARD").upper()
            for target in TARGETS:
                for header, state in RESTORE_HEADERS.items():
                    if target not in ALLOWED_TARGETS.get(observed, ()):
                        expected = "api.vault_move.colder_only"
                    elif storage_class in REQUIRES_RESTORE and state != "available":
                        expected = "api.vault_move.restore_first"
                        restore_first += 1
                    else:
                        expected = None
                    head = {"StorageClass": storage_class, "Restore": header}
                    with self.subTest(storage_class=storage_class, target=target, header=header):
                        try:
                            vault_moves.check_source(target, head, {})
                        except vault_moves.MoveBlocked as exc:
                            self.assertEqual(exc.message_key, expected)
                        else:
                            self.assertIsNone(expected)
        self.assertEqual(restore_first, 4)


if __name__ == "__main__":
    unittest.main()
