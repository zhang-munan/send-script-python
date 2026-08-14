import unittest

from adb_sms_worker.db import SmsRepository


class RecipientNoticeMilestoneTests(unittest.TestCase):
    def test_first_and_every_fifth_are_milestones(self):
        milestones = [
            count
            for count in range(1, 17)
            if SmsRepository._is_notice_milestone(count)
        ]
        self.assertEqual(milestones, [1, 5, 10, 15])

    def test_switch_values_default_to_disabled(self):
        for value in ("1", "true", "ON", "enabled", 1, True):
            self.assertTrue(SmsRepository._enabled_param_value(value))
        for value in ("0", "false", "", None, 0, False):
            self.assertFalse(SmsRepository._enabled_param_value(value))

    def test_daily_queue_guard_uses_database_date(self):
        class Cursor:
            def __init__(self, row):
                self.row = row
                self.sql = ""
                self.params = None

            def execute(self, sql, params):
                self.sql = " ".join(sql.split())
                self.params = params

            def fetchone(self):
                return self.row

        repository = SmsRepository.__new__(SmsRepository)
        cursor = Cursor({"1": 1})

        self.assertTrue(
            repository._notice_already_queued_today(cursor, "13800138000")
        )
        self.assertIn("CURDATE()", cursor.sql)
        self.assertIn("INTERVAL 1 DAY", cursor.sql)
        self.assertEqual(cursor.params, ("13800138000",))


if __name__ == "__main__":
    unittest.main()
