"""The remaining Rclone route is plaintext and bucket-rooted, never crypt."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.rclone_runtime import require_plain_vault_remote
from app.services.vault_paths import expected_cloud_key, object_key_to_path
from app.storage import validate_cloud_vault


class PlaintextRemoteTests(unittest.TestCase):
    def _check(self, text, **fields):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rclone.conf"
            path.write_text(text)
            require_plain_vault_remote(
                {"rclone_remote": "plain", "s3_bucket": "bucket", **fields}, str(path),
            )

    def test_only_plain_bucket_root_alias_is_accepted(self):
        self._check("[plain]\ntype = alias\nremote = base:bucket/\n[base]\ntype = s3\n")

    def test_legacy_or_unknown_routes_fail_closed_without_config_disclosure(self):
        for text in (
            "[plain]\ntype = crypt\npassword = legacy-secret-fixture\n",
            "[plain]\ntype = alias\nremote = base:bucket\n[base]\ntype = crypt\n",
            "[plain]\ntype = alias\nremote = base:other\n[base]\ntype = s3\n",
            "[plain]\ntype = alias\nremote = base:bucket/prefix\n[base]\ntype = s3\n",
            "[plain]\ntype = s3\n", "[plain]\ntype = unknown\n", "invalid",
        ):
            with self.subTest(text=text[:15]):
                with self.assertRaises(RuntimeError) as error:
                    self._check(text)
                self.assertNotIn("legacy-secret-fixture", str(error.exception))

    def test_stale_crypt_row_is_refused_even_with_plain_route(self):
        with self.assertRaisesRegex(RuntimeError, "Legacy encrypted"):
            self._check("", encryption_mode="crypt")

    def test_legacy_config_blocks_preflight_before_any_s3_call(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rclone.conf"
            path.write_text("[plain]\ntype = crypt\npassword = legacy-secret-fixture\n")
            with patch("app.storage.settings") as settings, patch("app.storage.s3_client") as client:
                settings.rclone_config = str(path)
                with self.assertRaises(RuntimeError):
                    validate_cloud_vault({"rclone_remote": "plain", "s3_bucket": "bucket"})
                client.assert_not_called()

    def test_readable_keys_preserve_unicode_spaces_and_bin_extension(self):
        prefix = "vaults/0275c250-23e1-4ad4-a911-22b62db1aade/"
        for relative in ("Foto/estate 2026/città.jpg", "original.bin"):
            key = expected_cloud_key(relative, prefix)
            self.assertEqual(key, prefix + relative)
            self.assertEqual(object_key_to_path(key, prefix), relative)
