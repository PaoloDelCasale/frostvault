import unittest
from unittest.mock import Mock
from botocore.exceptions import ClientError
from app.services.lifecycle_profiles import GUIDED_PROFILES
from app.services.s3_lifecycle_rules import (
    ABORT_RULE_ID, LifecycleRuleLimitExceeded, build_vault_lifecycle_rule,
    sync_bucket_lifecycle_rules, incomplete_upload_rule,
)


class VaultLifecycleRuleTests(unittest.TestCase):
    def test_exact_vault_prefix_and_current_transitions_only(self):
        for profile in GUIDED_PROFILES.values():
            rule = build_vault_lifecycle_rule("uuid", "vaults/uuid/", profile)
            self.assertEqual(rule["Filter"], {"Prefix": "vaults/uuid/"})
            self.assertFalse(any("Expiration" in key or "Noncurrent" in key for key in rule))
        with self.assertRaises(ValueError):
            build_vault_lifecycle_rule("uuid", "vaults/", GUIDED_PROFILES["archive_tiered"])

    def test_sync_preserves_external_rules_replaces_legacy_and_keeps_abort(self):
        client = Mock()
        external = {"ID": "external", "Status": "Enabled", "Filter": {"Prefix": "elsewhere/"}, "Expiration": {"Days": 10}}
        client.get_bucket_lifecycle_configuration.return_value = {"Rules": [external,
            {"ID": "psa-policy-old", "Expiration": {"Days": 5}},
            {"ID": "frostvault-plan-old"}, incomplete_upload_rule()]}
        rule = build_vault_lifecycle_rule("uuid", "vaults/uuid/", GUIDED_PROFILES["archive_tiered"])
        sync_bucket_lifecycle_rules(client, bucket="bucket", app_rules=[rule])
        self.assertEqual(client.put_bucket_lifecycle_configuration.call_args.kwargs["LifecycleConfiguration"]["Rules"], [external, rule, incomplete_upload_rule()])
        client.delete_object.assert_not_called()
        client.delete_objects.assert_not_called()

    def test_standard_removes_old_transition_rules_but_not_completed_content(self):
        client = Mock()
        client.get_bucket_lifecycle_configuration.return_value = {"Rules": [{"ID": "psa-policy-old"}]}
        sync_bucket_lifecycle_rules(client, bucket="bucket", app_rules=[])
        rules = client.put_bucket_lifecycle_configuration.call_args.kwargs["LifecycleConfiguration"]["Rules"]
        self.assertEqual(rules, [incomplete_upload_rule()])
        self.assertEqual(rules[0]["ID"], ABORT_RULE_ID)
        self.assertEqual(rules[0]["AbortIncompleteMultipartUpload"], {"DaysAfterInitiation": 7})
        self.assertNotIn("Tag", rules[0]["Filter"])

    def test_missing_configuration_and_errors_fail_closed(self):
        client = Mock()
        client.get_bucket_lifecycle_configuration.side_effect = ClientError({"Error": {"Code": "NoSuchLifecycleConfiguration"}}, "GetBucketLifecycleConfiguration")
        sync_bucket_lifecycle_rules(client, bucket="bucket", app_rules=[])
        client.put_bucket_lifecycle_configuration.assert_called_once()
        client.reset_mock()
        client.get_bucket_lifecycle_configuration.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "GetBucketLifecycleConfiguration")
        with self.assertRaises(ClientError):
            sync_bucket_lifecycle_rules(client, bucket="bucket", app_rules=[])
        client.put_bucket_lifecycle_configuration.assert_not_called()

    def test_rule_limit_counts_abort_and_external_rules(self):
        client = Mock()
        client.get_bucket_lifecycle_configuration.return_value = {"Rules": [{"ID": f"external-{n}"} for n in range(1000)]}
        with self.assertRaises(LifecycleRuleLimitExceeded):
            sync_bucket_lifecycle_rules(client, bucket="bucket", app_rules=[])
        client.put_bucket_lifecycle_configuration.assert_not_called()
