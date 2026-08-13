import unittest

from adb_sms_worker.db import SmsRepository


class ClaimScheduleQueryTests(unittest.TestCase):
    def test_claim_query_only_allows_immediate_or_due_scheduled_jobs(self):
        """Regression test for the production SQL boundary used by MySQL."""
        constants = [
            value
            for value in SmsRepository.claim_next.__code__.co_consts
            if isinstance(value, str) and "FROM message_info" in value
        ]
        self.assertEqual(len(constants), 1)
        sql = " ".join(constants[0].split())

        self.assertIn("auditStatus = 1", sql)
        self.assertIn("status IN (1, 3)", sql)
        self.assertIn("sendType = 1 OR", sql)
        self.assertIn(
            "sendType = 2 AND scheduledAt IS NOT NULL AND scheduledAt <= NOW()",
            sql,
        )
        self.assertNotIn("scheduledAt >= NOW()", sql)


if __name__ == "__main__":
    unittest.main()
