"""Deterministic filesystem boundaries; no database, S3 or credentials."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.cloud_deletion import CloudDeletionError, DELETION_DELAY_SECONDS, require_local_absence
from app.services import cloud_deletion, source_identity, source_layout, vault_relocation


def simulated_mount(*nested: Path):
    """The descriptor mount-ID seam with ``nested`` as other mounts (#753).

    A non-privileged CI host cannot bind-mount a directory under the root, so
    the directories in ``nested`` (and everything below them) report another
    mount ID; every other descriptor keeps its real one.
    """
    real = source_identity.descriptor_mount_id
    targets = [os.path.realpath(path) for path in nested]

    def mount_id(fd: int) -> int:
        here = os.path.realpath(f"/proc/self/fd/{fd}")
        if any(os.path.commonpath((here, target)) == target for target in targets):
            return real(fd) + 1
        return real(fd)
    return patch.object(source_layout, "descriptor_mount_id", side_effect=mount_id)


class LocalAbsenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = {"source_root":str(self.root),"lifecycle":"active",
                      "root_identity_version":vault_relocation.ROOT_IDENTITY_VERSION,
                      "root_identity":vault_relocation.root_identity(self.root)}
        patcher = patch("app.services.source_layout.vault_local_access",
                        return_value=SimpleNamespace(local_operations_allowed=True))
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
        for overrides in ({"root_identity":"wrong"},{"root_identity":None},{"lifecycle":"reconnect_required"}):
            with self.subTest(overrides=overrides), self.assertRaises(CloudDeletionError):
                require_local_absence({**self.vault, **overrides},["missing.jpg"])

    def test_missing_or_untrusted_source_is_not_absence(self):
        with patch("app.services.source_layout.vault_local_access",return_value=SimpleNamespace(local_operations_allowed=False)):
            with self.assertRaises(CloudDeletionError): require_local_absence(self.vault,["missing.jpg"])

    def test_same_filesystem_bind_mount_below_the_root_is_not_absence(self):
        """Same ``st_dev``, another mount ID: a bind mount of the same
        filesystem nested under the root blocks the proof (#753)."""
        (self.root / "library/user/2026").mkdir(parents=True)
        with simulated_mount(self.root / "library/user"):
            for path in ("library/user/photo.jpg", "library/user/2026/photo.jpg"):
                with self.subTest(path=path), self.assertRaisesRegex(CloudDeletionError, "Nested mount"):
                    require_local_absence(self.vault, [path])
                with self.subTest(path=path), self.assertRaisesRegex(CloudDeletionError, "Nested mount"):
                    cloud_deletion._present_paths(self.vault, [path])
            # Outside the nested mount the proof still holds.
            require_local_absence(self.vault, ["library/photo.jpg", "elsewhere/photo.jpg"])

    def test_the_real_mount_id_lets_an_ordinary_tree_prove_absence(self):
        """Without the seam: directories of the root's own mount share its ID."""
        (self.root / "a/b/c").mkdir(parents=True)
        require_local_absence(self.vault, ["a/b/c/missing.jpg"])

    def test_an_unreadable_or_unparseable_mount_id_is_not_absence(self):
        (self.root / "album").mkdir()
        failure = source_identity.MountIdentityError("descriptor mount identity is unreadable")
        with patch.object(source_layout, "descriptor_mount_id", side_effect=failure):
            for paths in (["missing.jpg"], ["album/missing.jpg"]):
                with self.subTest(paths=paths), self.assertRaises(CloudDeletionError):
                    require_local_absence(self.vault, paths)
                with self.subTest(paths=paths), self.assertRaises(CloudDeletionError):
                    cloud_deletion._present_paths(self.vault, paths)
        with tempfile.TemporaryDirectory() as fdinfo:
            # The real reader, against an fdinfo directory without the entries.
            with patch.object(source_identity, "FDINFO_DIR", Path(fdinfo)), \
                    self.assertRaises(CloudDeletionError):
                require_local_absence(self.vault, ["album/missing.jpg"])

    def test_mountinfo_reads_do_not_grow_with_directories_or_files(self):
        """The proof compares descriptors: it never re-reads mountinfo per
        directory (#753: 81% of a 7-14 s duplicate count)."""
        def reads(paths):
            with patch.object(source_identity, "read_mountinfo_text",
                              wraps=source_identity.read_mountinfo_text) as spy:
                cloud_deletion._present_paths(self.vault, paths)
            return spy.call_count
        few = reads(["d0/e0/missing.jpg"])
        many = []
        for d in range(40):
            for e in range(5):
                (self.root / f"d{d}/e{e}").mkdir(parents=True, exist_ok=True)
                many.extend(f"d{d}/e{e}/missing{i}.jpg" for i in range(3))
        self.assertEqual(reads(many), few)
        self.assertEqual(few, 0, "with the local access probe mocked, nothing reads mountinfo")


    def test_traversal_absolute_empty_and_noncanonical_paths_fail_closed(self):
        for path in ("../photo.jpg","/photo.jpg","","a/../photo.jpg","a//photo.jpg","a/./photo.jpg"):
            with self.subTest(path=path), self.assertRaises(CloudDeletionError):
                require_local_absence(self.vault,[path])


class DescriptorMountIdTests(unittest.TestCase):
    def test_fdinfo_mount_id_parsing_is_strict(self):
        self.assertEqual(source_identity.mount_id_from_fdinfo("pos:\t0\nflags:\t02\nmnt_id:\t19846\nino:\t4\n"), 19846)
        for text in ("pos:\t0\n", "mnt_id:\t\n", "mnt_id:\tabc\n", "mnt_id:\t-1\n",
                     "mnt_id:\t1\nmnt_id:\t2\n", ""):
            with self.subTest(text=text), self.assertRaises(source_identity.MountIdentityError):
                source_identity.mount_id_from_fdinfo(text)

    def test_a_descriptor_reports_the_mount_of_its_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "child").mkdir()
            parent = os.open(tmp, os.O_RDONLY | os.O_DIRECTORY)
            self.addCleanup(os.close, parent)
            child = os.open("child", os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent)
            self.addCleanup(os.close, child)
            self.assertEqual(source_identity.descriptor_mount_id(parent),
                             source_identity.descriptor_mount_id(child))
        with self.assertRaises(source_identity.MountIdentityError):
            source_identity.descriptor_mount_id(10**6)
