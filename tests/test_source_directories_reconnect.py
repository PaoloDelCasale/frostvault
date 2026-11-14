"""Reconnect browses its own Vault root as a free folder (#656)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import source_directories, source_layout


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=()):
        excluded = params[0] if params else None
        return _Rows([row for row in self.rows if row["id"] != excluded])


class ReconnectBrowseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.volume_path = Path(self.tmp.name) / "photos"
        (self.volume_path / "album").mkdir(parents=True)
        (self.volume_path / "other").mkdir()
        self.volume = source_layout.SourceVolume("photos", str(self.volume_path), "rw", "ok")
        self.connection = _Connection([
            {"id": 7, "name": "Photos", "source_root": str(self.volume_path / "album")},
            {"id": 8, "name": "Other", "source_root": str(self.volume_path / "other")},
        ])

    def browse(self, exclude=None):
        with patch.object(source_directories, "_volume_or_raise", return_value=self.volume):
            listing = source_directories.browse_source_directories(
                self.connection, volume_alias="photos", exclude_vault_id=exclude)
        return {item["name"]: item["selectable"] for item in listing["items"]}

    def test_every_vault_root_is_occupied_by_default(self):
        self.assertEqual(self.browse(), {"album": False, "other": False})

    def test_the_reconnecting_vault_may_choose_its_own_root_again(self):
        self.assertEqual(self.browse(exclude=7), {"album": True, "other": False})


if __name__ == "__main__":
    unittest.main()
