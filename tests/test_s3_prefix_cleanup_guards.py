"""Unit coverage for prefix cleanup guardrails (issue #13)."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from app.services.s3_prefix_cleanup import cleanup_prefix_versions


class PrefixCleanupGuardTests(unittest.TestCase):
    def test_empty_prefix_is_refused_without_listing(self) -> None:
        client = Mock()
        report = cleanup_prefix_versions(client, bucket="archive-ci", prefix="")
        self.assertFalse(report.ok)
        self.assertIn("empty prefix", report.message.lower())
        client.list_object_versions.assert_not_called()
        client.delete_objects.assert_not_called()

    def test_slash_only_prefix_is_refused(self) -> None:
        client = Mock()
        report = cleanup_prefix_versions(client, bucket="archive-ci", prefix="/")
        self.assertFalse(report.ok)
        client.list_object_versions.assert_not_called()
