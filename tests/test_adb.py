import unittest

from adb_sms_worker.adb import (
    AdbError,
    SmsUiSender,
    UiTargetNotFound,
    find_send_target,
    find_send_target_from_activity_dump,
    find_sim_prompt_target,
    ui_has_send_failure,
)


def hierarchy(*nodes: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><hierarchy>' + "".join(nodes) + "</hierarchy>"


class SendTargetTests(unittest.TestCase):
    def test_prefers_known_google_messages_resource(self):
        xml = hierarchy(
            '<node resource-id="x:id/send_later" text="Send later" content-desc="" clickable="true" enabled="true" bounds="[0,0][50,50]" />',
            '<node resource-id="com.google.android.apps.messaging:id/send_message_button_icon" text="" content-desc="Send SMS" clickable="true" enabled="true" bounds="[100,200][200,300]" />',
        )
        target = find_send_target(xml)
        self.assertEqual((target.x, target.y), (150, 250))

    def test_supports_chinese_send_description(self):
        xml = hierarchy(
            '<node resource-id="com.vendor:id/action" text="" content-desc="发送短信" clickable="true" enabled="true" bounds="[20,40][80,100]" />'
        )
        self.assertEqual(find_send_target(xml).label, "com.vendor:id/action")

    def test_disabled_send_is_rejected(self):
        xml = hierarchy(
            '<node resource-id="com.android.mms:id/send_button" text="" content-desc="发送" clickable="true" enabled="false" bounds="[20,40][80,100]" />'
        )
        with self.assertRaises(UiTargetNotFound):
            find_send_target(xml)

    def test_sim_prompt_requires_prompt_context(self):
        ordinary = hierarchy(
            '<node resource-id="x" text="SIM 1" content-desc="" clickable="true" enabled="true" bounds="[0,0][100,100]" />'
        )
        self.assertIsNone(find_sim_prompt_target(ordinary, 1))
        prompt = hierarchy(
            '<node resource-id="title" text="选择 SIM" content-desc="" clickable="false" enabled="true" bounds="[0,0][100,40]" />',
            '<node resource-id="sim1" text="SIM 1" content-desc="" clickable="true" enabled="true" bounds="[0,40][100,100]" />',
        )
        self.assertEqual(find_sim_prompt_target(prompt, 1).label, "sim1")

    def test_activity_dump_accumulates_relative_coordinates(self):
        dump = """    View Hierarchy:
      DecorView@abc[ComposeMessageActivity]
        android.widget.LinearLayout{abc V.E...... ........ 0,0-1080,2340}
          android.widget.FrameLayout{def V.E...... ........ 0,84-1080,2340}
            android.widget.LinearLayout{123 V.E...... ........ 0,144-1080,2256}
              android.widget.LinearLayout{456 V.E...... ........ 0,1943-1080,2112 #7f0900e8 app:id/composeBottomFragment}
                android.widget.LinearLayout{789 V........ ........ 885,62-1080,127 #7f0903a1 app:id/send_button_with_counter}
                  android.widget.TextView{aaa V..D..C.. ........ 49,0-145,65 #7f09039f app:id/send_button}
"""
        target = find_send_target_from_activity_dump(dump)
        self.assertEqual(target.label, "app:id/send_button")
        self.assertEqual((target.x, target.y), (982, 2265))


class SendResultTests(unittest.TestCase):
    def test_ignores_failure_label_already_visible_before_click(self):
        before = hierarchy(
            '<node resource-id="old" text="未发送" content-desc="" bounds="[0,0][10,10]" />'
        )
        after = hierarchy(
            '<node resource-id="old-moved" text="未发送" content-desc="" bounds="[0,20][10,30]" />',
            '<node resource-id="new" text="刚刚发送的消息" content-desc="" bounds="[0,40][10,50]" />',
        )
        self.assertFalse(ui_has_send_failure(after, before))

    def test_detects_failure_label_newly_added_after_click(self):
        before = hierarchy(
            '<node resource-id="message" text="待发送" content-desc="" bounds="[0,0][10,10]" />'
        )
        after = hierarchy(
            '<node resource-id="message" text="待发送" content-desc="" bounds="[0,0][10,10]" />',
            '<node resource-id="error" text="发送失败" content-desc="" bounds="[0,20][10,30]" />',
        )
        self.assertTrue(ui_has_send_failure(after, before))

    def test_sender_does_not_reject_success_because_of_historical_failure(self):
        old_failure = hierarchy(
            '<node resource-id="old" text="未发送" content-desc="" bounds="[0,0][10,10]" />',
            '<node resource-id="send" text="发送" content-desc="" clickable="true" enabled="true" bounds="[20,20][40,40]" />',
        )
        after = hierarchy(
            '<node resource-id="old" text="未发送" content-desc="" bounds="[0,0][10,10]" />',
            '<node resource-id="sent" text="本次消息" content-desc="" bounds="[0,50][10,60]" />',
        )

        class FakeAdb:
            def __init__(self):
                self.dumps = [old_failure, after]

            def open_sms_composer(self, *_args):
                pass

            def dump_ui(self, *_args):
                return self.dumps.pop(0)

            def tap(self, *_args):
                pass

        sender = SmsUiSender(FakeAdb(), ui_wait_seconds=0)
        target = sender.prepare("new-phone", "13800138000", "本次消息", False, None)
        self.assertEqual(sender.click_and_verify("new-phone", target, None), "UI_CHECKED")

    def test_sender_reports_new_failure_after_click(self):
        before = hierarchy(
            '<node resource-id="send" text="发送" content-desc="" clickable="true" enabled="true" bounds="[20,20][40,40]" />'
        )
        after = hierarchy(
            '<node resource-id="error" text="发送失败" content-desc="" bounds="[0,50][10,60]" />'
        )

        class FakeAdb:
            def __init__(self):
                self.dumps = [before, after]

            def open_sms_composer(self, *_args):
                pass

            def dump_ui(self, *_args):
                return self.dumps.pop(0)

            def tap(self, *_args):
                pass

        sender = SmsUiSender(FakeAdb(), ui_wait_seconds=0)
        target = sender.prepare("new-phone", "13800138000", "本次消息", False, None)
        with self.assertRaisesRegex(AdbError, "短信应用显示发送失败"):
            sender.click_and_verify("new-phone", target, None)

    def test_sender_accepts_sim_tap_when_vendor_hides_ui_afterward(self):
        before = hierarchy(
            '<node resource-id="send" text="发送" content-desc="" clickable="true" enabled="true" bounds="[20,20][40,40]" />'
        )
        sim_prompt = hierarchy(
            '<node resource-id="title" text="选择 SIM" content-desc="" bounds="[0,0][100,20]" />',
            '<node resource-id="sim1" text="SIM 1" content-desc="" clickable="true" enabled="true" bounds="[0,20][100,50]" />',
        )

        class FakeAdb:
            def __init__(self):
                self.dumps = [before, sim_prompt]
                self.tap_count = 0

            def open_sms_composer(self, *_args):
                pass

            def dump_ui(self, *_args):
                if self.dumps:
                    return self.dumps.pop(0)
                raise AdbError("vendor UI is no longer readable")

            def tap(self, *_args):
                self.tap_count += 1

        adb = FakeAdb()
        sender = SmsUiSender(adb, ui_wait_seconds=0)
        target = sender.prepare("dual-sim-phone", "13800138000", "本次消息", False, 1)
        self.assertEqual(sender.click_and_verify("dual-sim-phone", target, 1), "ADB_TAP")
        self.assertEqual(adb.tap_count, 2)


if __name__ == "__main__":
    unittest.main()
