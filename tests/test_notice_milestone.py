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


if __name__ == "__main__":
    unittest.main()
