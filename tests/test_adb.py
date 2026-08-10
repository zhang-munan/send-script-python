import unittest

from adb_sms_worker.adb import (
    UiTargetNotFound,
    find_send_target,
    find_send_target_from_activity_dump,
    find_sim_prompt_target,
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


if __name__ == "__main__":
    unittest.main()
