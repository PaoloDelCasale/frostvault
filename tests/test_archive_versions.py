"""The Archive Version cluster behind the catalog's names (#803).

The replacement in progress, Current and the Archive Version rows live in
``app.services.archive_versions``. The catalog still offers them under its own
names: these tests fix that the two interfaces are one.
"""
from __future__ import annotations

import inspect
import unittest

from app import catalog
from app.catalog import ArchiveCatalog
from app.services import archive_versions
from app.services.archive_versions import ArchiveVersions


class ArchiveVersionInterfaceTests(unittest.TestCase):
    def test_replacement_errors_are_the_same_classes_through_the_catalog(self) -> None:
        # An ``except`` written against either name catches what the cluster raises.
        self.assertIs(catalog.ReplacementInProgress, archive_versions.ReplacementInProgress)
        self.assertIs(catalog.StaleReplacement, archive_versions.StaleReplacement)
        self.assertTrue(issubclass(catalog.ReplacementInProgress, RuntimeError))
        self.assertTrue(issubclass(catalog.StaleReplacement, LookupError))

    def test_catalog_names_take_the_same_arguments_as_the_cluster(self) -> None:
        names = [
            name for name, member in vars(ArchiveVersions).items()
            if inspect.isfunction(member) and not name.startswith("_")
        ]
        self.assertEqual(len(names), 11)
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(
                    inspect.signature(getattr(ArchiveCatalog, name)),
                    inspect.signature(getattr(ArchiveVersions, name)),
                )


if __name__ == "__main__":
    unittest.main()
