import unittest
from datetime import datetime
from types import SimpleNamespace

from adb_sms_worker.models import SmsJob
from adb_sms_worker.worker import DeviceWorker


class WorkerBlacklistTests(unittest.TestCase):
    def test_does_not_click_when_atomic_arm_check_reports_blocked(self):
        calls: list[str] = []

        class Repository:
            def mark_composer_open(self, _job):
                calls.append("composer")

            def arm_send(self, _job):
                calls.append("blocked")
                return False

            def mark_success(self, _job):
                calls.append("success")

            def mark_clicked(self, _job):
                calls.append("db-clicked")

            def mark_failed(self, _job, _reason, _armed):
                calls.append("failed")

        class Sender:
            def prepare(self, *_args):
                return SimpleNamespace(label="send")

            def click_and_verify(self, *_args):
                calls.append("clicked")

        settings = SimpleNamespace(
            ui_wait_seconds=0,
            wake_and_dismiss_keyguard=False,
            sim_slot=None,
        )
        worker = DeviceWorker(settings, Repository(), object(), "device-1")
        worker.sender = Sender()
        worker.process(
            SmsJob(
                id=1,
                receiver_phone="13800138000",
                content="测试",
                attempt_token="token",
                device_serial="device-1",
                send_type=1,
                scheduled_at=datetime.now(),
            )
        )

        self.assertEqual(calls, ["composer", "blocked"])

    def test_persists_click_before_success(self):
        calls: list[str] = []

        class Repository:
            def mark_composer_open(self, _job):
                calls.append("composer")

            def arm_send(self, _job):
                calls.append("armed")
                return True

            def mark_clicked(self, _job):
                calls.append("db-clicked")

            def mark_success(self, _job):
                calls.append("success")

            def mark_failed(self, _job, _reason, _armed):
                calls.append("failed")

        class Sender:
            def prepare(self, *_args):
                return SimpleNamespace(label="send")

            def click_and_verify(self, *_args, on_clicked=None):
                calls.append("tap")
                on_clicked()
                return "ADB_TAP"

        settings = SimpleNamespace(
            ui_wait_seconds=0,
            wake_and_dismiss_keyguard=False,
            sim_slot=None,
        )
        worker = DeviceWorker(settings, Repository(), object(), "device-1")
        worker.sender = Sender()
        worker.process(
            SmsJob(
                id=2,
                receiver_phone="13800138000",
                content="测试",
                attempt_token="token",
                device_serial="device-1",
                send_type=1,
                scheduled_at=datetime.now(),
            )
        )

        self.assertEqual(
            calls,
            ["composer", "armed", "tap", "db-clicked", "success"],
        )


if __name__ == "__main__":
    unittest.main()
