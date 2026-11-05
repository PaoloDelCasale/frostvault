"""Deterministic filesystem boundaries; no database, S3 or credentials."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.cloud_deletion import CloudDeletionError, DELETION_DELAY_SECONDS, require_local_absence
from app.services import vault_relocation


class LocalAbsenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = {"source_root":str(self.root),"relocation_state":"ready",
                      "root_identity_version":vault_relocation.ROOT_IDENTITY_VERSION,
                      "root_identity":vault_relocation.root_identity(self.root)}
        for target, result in (("app.services.source_layout.vault_local_access", SimpleNamespace(local_operations_allowed=True)),
                               ("app.services.source_layout.path_is_mount", False)):
            patcher = patch(target, return_value=result)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_delay_is_unconditionally_seven_days(self):
        self.assertEqual(DELETION_DELAY_SECONDS,604800)

    def test_missing_leaf_and_missing_parent_below_verified_root_are_absent(self):
        (self.root / "existing").mkdir()
        require_local_absence(self.vault,["photo.jpg","missing/deep/photo.jpg","existing/photo.jpg"])

    def test_present_regular_directory_or_dangling_symlink_are_not_absence(self):
        (self.root / "regular").write_bytes(b"data")
        (self.root / "directory").mkdir()
        (self.root / "dangling").symlink_to(self.root / "missing")
        for path in ("regular","directory","dangling"):
            with self.subTest(path=path), self.assertRaises(CloudDeletionError):
                require_local_absence(self.vault,[path])

    def test_symlink_parent_is_not_traversed(self):
        (self.root / "link").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(CloudDeletionError): require_local_absence(self.vault,["link/missing.jpg"])

    def test_replaced_or_unenrolled_root_and_reconnect_are_not_absence(self):
        for overrides in ({"root_identity":"wrong"},{"root_identity":None},{"relocation_state":"scan_required"}):
            with self.subTest(overrides=overrides), self.assertRaises(CloudDeletionError):
                require_local_absence({**self.vault, **overrides},["missing.jpg"])

    def test_missing_or_untrusted_source_is_not_absence(self):
        with patch("app.services.source_layout.vault_local_access",return_value=SimpleNamespace(local_operations_allowed=False)):
            with self.assertRaises(CloudDeletionError): require_local_absence(self.vault,["missing.jpg"])

    def test_nested_mount_does_not_authorize_deletion(self):
        (self.root / "nested").mkdir()
        with patch("app.services.source_layout.path_is_mount",return_value=True):
            with self.assertRaises(CloudDeletionError): require_local_absence(self.vault,["nested/missing.jpg"])

    def test_traversal_absolute_empty_and_noncanonical_paths_fail_closed(self):
        for path in ("../photo.jpg","/photo.jpg","","a/../photo.jpg","a//photo.jpg","a/./photo.jpg"):
            with self.subTest(path=path), self.assertRaises(CloudDeletionError):
                require_local_absence(self.vault,[path])
