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


if __name__ == "__main__":
    unittest.main()
