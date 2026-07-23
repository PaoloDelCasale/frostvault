from __future__ import annotations

import unittest
from unittest.mock import Mock

from app.services.lifecycle_profiles import GUIDED_PROFILES
from app.services.s3_lifecycle_rules import (
    LifecycleRuleLimitExceeded,
    build_policy_lifecycle_rule,
    enforce_rule_limit,
    is_application_lifecycle_rule,
    merge_lifecycle_rules,
    sync_bucket_lifecycle_rules,
)


class BuildPolicyLifecycleRuleTests(unittest.TestCase):
    def test_rule_filters_by_policy_tag_not_object_prefix(self) -> None:
        policy_id = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
        rule = build_policy_lifecycle_rule(
            policy_id,
            GUIDED_PROFILES["ia_after_30"],
        )
        self.assertEqual(
            rule["Filter"],
            {"Tag": {"Key": "psa:policy-id", "Value": policy_id}},
        )
        self.assertNotIn("Prefix", rule.get("Filter", {}))
        self.assertEqual(rule["Transitions"][0]["StorageClass"], "STANDARD_IA")


class MergeLifecycleRulesTests(unittest.TestCase):
    def test_merge_preserves_non_application_rules(self) -> None:
        existing = [
            {
                "ID": "abort-incomplete-multipart-uploads",
                "Status": "Enabled",
                "Filter": {},
            },
            {
                "ID": "psa-policy-old",
                "Status": "Enabled",
                "Filter": {"Tag": {"Key": "psa:policy-id", "Value": "old"}},
            },
        ]
        app_rules = [
            build_policy_lifecycle_rule(
                "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                GUIDED_PROFILES["standard_only"],
            )
        ]
        merged = merge_lifecycle_rules(existing, app_rules)
        self.assertEqual(merged[0]["ID"], "abort-incomplete-multipart-uploads")
        self.assertEqual(len(merged), 2)
        self.assertTrue(is_application_lifecycle_rule(merged[1]))


class LifecycleRuleLimitTests(unittest.TestCase):
    def test_enforce_rule_limit_rejects_buckets_over_1000_rules(self) -> None:
        rules = [{"ID": f"rule-{index}", "Status": "Enabled"} for index in range(1001)]
        with self.assertRaises(LifecycleRuleLimitExceeded):
            enforce_rule_limit(rules)


class SyncBucketLifecycleRulesTests(unittest.TestCase):
    def test_sync_merges_application_rules_and_puts_configuration(self) -> None:
        client = Mock()
        client.get_bucket_lifecycle_configuration.return_value = {
            "Rules": [
                {
                    "ID": "abort-incomplete-multipart-uploads",
                    "Status": "Enabled",
                    "Filter": {},
                }
            ]
        }
        policy_id = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
        sync_bucket_lifecycle_rules(
            client,
            bucket="archive-bucket",
            app_rules=[
                build_policy_lifecycle_rule(
                    policy_id,
                    GUIDED_PROFILES["ia_after_30"],
                )
            ],
        )
        client.put_bucket_lifecycle_configuration.assert_called_once()
        rules = client.put_bucket_lifecycle_configuration.call_args.kwargs[
            "LifecycleConfiguration"
        ]["Rules"]
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[1]["Filter"]["Tag"]["Value"], policy_id)


class LifecyclePolicyProfileDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from app.database import SQLiteConnection
        from app.services.lifecycle_policies import create_policy, set_policy_profile
        from tests.test_database import run_alembic

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "profiles.db"
        result = run_alembic(self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        with SQLiteConnection(str(self.path)) as connection:
            connection.execute(
                "INSERT INTO vaults(id, slug, name, source_root, s3_bucket, s3_prefix, rclone_remote) "
                "VALUES (1, 'docs', 'Docs', '/source', 'bucket', 'vaults/uuid', 'remote')"
            )
            self.policy_id = create_policy(connection, vault_id=1, name="default")
        self.set_policy_profile = set_policy_profile
        self.SQLiteConnection = SQLiteConnection

    def test_set_policy_profile_rejects_invalid_profile(self) -> None:
        from app.services.lifecycle_profiles import LifecycleProfile, LifecycleTransition

        profile = LifecycleProfile(
            transitions=(LifecycleTransition(days=7, storage_class="STANDARD_IA"),)
        )
        with self.SQLiteConnection(str(self.path)) as connection:
            validation = self.set_policy_profile(connection, self.policy_id, profile)
            row = connection.execute(
                "SELECT profile_json FROM lifecycle_policies WHERE id=%s",
                (self.policy_id,),
            ).fetchone()
        self.assertFalse(validation.ok)
        self.assertIsNone(row["profile_json"])

    def test_sync_lifecycle_rules_for_bucket_builds_tag_filtered_rules(self) -> None:
        from app.services.lifecycle_policies import sync_lifecycle_rules_for_bucket

        with self.SQLiteConnection(str(self.path)) as connection:
            self.set_policy_profile(
                connection,
                self.policy_id,
                GUIDED_PROFILES["ia_after_30"],
            )
        client = Mock()
        client.get_bucket_lifecycle_configuration.return_value = {"Rules": []}
        with self.SQLiteConnection(str(self.path)) as connection:
            count = sync_lifecycle_rules_for_bucket(
                connection,
                client,
                bucket="bucket",
            )
        self.assertEqual(count, 1)
        rules = client.put_bucket_lifecycle_configuration.call_args.kwargs[
            "LifecycleConfiguration"
        ]["Rules"]
        self.assertEqual(rules[0]["Filter"]["Tag"]["Value"], self.policy_id)

