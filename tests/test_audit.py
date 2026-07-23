from __future__ import annotations

import json
import unittest

from app import audit


class AuditLogTests(unittest.TestCase):
    def test_audit_log_emits_a_structured_record(self) -> None:
        with self.assertLogs("app.audit", level="WARNING") as captured:
            audit.audit_log("break_glass_denied", ip="203.0.113.5", username="root")
        self.assertEqual(len(captured.records), 1)
        payload = json.loads(captured.records[0].getMessage())
        self.assertEqual(payload["event"], "break_glass_denied")
        self.assertEqual(payload["ip"], "203.0.113.5")
        self.assertEqual(payload["username"], "root")


if __name__ == "__main__":
    unittest.main()
