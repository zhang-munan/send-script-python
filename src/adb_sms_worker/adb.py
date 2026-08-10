from __future__ import annotations

import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict

from .models import Device, UiTarget


class AdbError(RuntimeError):
    pass


class UiTargetNotFound(AdbError):
    pass


_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_ACTIVITY_VIEW_RE = re.compile(
    r"^(?P<indent>\s+)(?P<class>[\w.$]+)\{[^}]*?\s"
    r"(?P<x1>-?\d+),(?P<y1>-?\d+)-(?P<x2>-?\d+),(?P<y2>-?\d+)"
    r"(?:\s+#[0-9a-fA-F]+\s+(?P<resource>\S+:id/\S+))?\s*\}$"
)
_SEND_WORDS = (
    "send sms",
    "send message",
    "send",
    "发送短信",
    "发送信息",
    "发送",
)
_FAILURE_WORDS = ("发送失败", "未发送", "not sent", "failed to send", "couldn't send")


class AdbClient:
    def __init__(self, adb_path: str = "adb", timeout: float = 20.0):
        self.adb_path = adb_path
        self.timeout = timeout

    def _run(self, args: list[str], serial: str | None = None, timeout: float | None = None) -> str:
        command = [self.adb_path]
        if serial:
            command += ["-s", serial]
        command += args
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AdbError(f"找不到 ADB: {self.adb_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError("ADB 命令超时") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise AdbError(detail or f"ADB 返回状态码 {result.returncode}")
        return result.stdout

    def devices(self) -> list[Device]:
        output = self._run(["devices", "-l"])
        devices: list[Device] = []
        for line in output.splitlines()[1:]:
            line = line.strip()
            if not line or line.startswith("*"):
                continue
            parts = line.split(maxsplit=2)
            if len(parts) >= 2:
                devices.append(Device(parts[0], parts[1], parts[2] if len(parts) > 2 else ""))
        return devices

    def shell(self, serial: str, *args: str, timeout: float | None = None) -> str:
        # A single quoted remote command preserves Chinese text, spaces and punctuation.
        remote_command = shlex.join(args)
        return self._run(["shell", remote_command], serial=serial, timeout=timeout)

    def get_state(self, serial: str) -> str:
        return self._run(["get-state"], serial=serial).strip()

    def getprop(self, serial: str, name: str) -> str:
        return self.shell(serial, "getprop", name).strip()

    def device_report(self, serial: str) -> dict[str, object]:
        report: dict[str, object] = {
            "serial": serial,
            "state": self.get_state(serial),
            "manufacturer": self.getprop(serial, "ro.product.manufacturer"),
            "model": self.getprop(serial, "ro.product.model"),
            "android": self.getprop(serial, "ro.build.version.release"),
            "sdk": self.getprop(serial, "ro.build.version.sdk"),
        }
        default_sms = self.shell(
            serial, "settings", "get", "secure", "sms_default_application"
        ).strip()
        report["default_sms_app"] = None if default_sms in {"", "null"} else default_sms
        report["device"] = asdict(next((d for d in self.devices() if d.serial == serial), Device(serial, "unknown")))
        return report

    def wake_and_dismiss_keyguard(self, serial: str) -> None:
        self.shell(serial, "input", "keyevent", "KEYCODE_WAKEUP")
        # This only dismisses an unsecured/swipe lock screen. It never bypasses credentials.
        try:
            self.shell(serial, "wm", "dismiss-keyguard")
        except AdbError:
            # Older Android versions do not expose `wm dismiss-keyguard`.
            self.shell(serial, "input", "keyevent", "KEYCODE_MENU")

    def open_sms_composer(self, serial: str, phone: str, content: str) -> None:
        if not re.fullmatch(r"\+?\d{5,20}", phone):
            raise ValueError("手机号格式不合法")
        output = self.shell(
            serial,
            "am",
            "start",
            "-W",
            "-a",
            "android.intent.action.SENDTO",
            "-d",
            f"smsto:{phone}",
            "--es",
            "sms_body",
            content,
            timeout=max(self.timeout, 30.0),
        )
        lowered = output.lower()
        if "error:" in lowered or "unable to resolve intent" in lowered:
            raise AdbError("手机没有成功打开默认短信应用")

    def dump_ui(self, serial: str) -> str:
        remote_path = f"/sdcard/window-{re.sub(r'[^A-Za-z0-9_.-]', '_', serial)}.xml"
        last_error: AdbError | None = None
        for _attempt in range(2):
            try:
                self.shell(serial, "uiautomator", "dump", remote_path, timeout=max(self.timeout, 30.0))
                last_error = None
                break
            except AdbError as exc:
                last_error = exc
                time.sleep(0.5)
        if last_error is not None:
            raise last_error
        xml = self.shell(serial, "cat", remote_path)
        try:
            self.shell(serial, "rm", remote_path)
        except AdbError:
            pass
        start = xml.find("<?xml")
        return xml[start:] if start >= 0 else xml

    def dump_activity_top(self, serial: str) -> str:
        return self.shell(serial, "dumpsys", "activity", "top", timeout=max(self.timeout, 30.0))

    def tap(self, serial: str, x: int, y: int) -> None:
        self.shell(serial, "input", "tap", str(x), str(y))


def _center(bounds: str) -> tuple[int, int] | None:
    match = _BOUNDS_RE.fullmatch(bounds)
    if not match:
        return None
    x1, y1, x2, y2 = map(int, match.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1 + x2) // 2, (y1 + y2) // 2


def _send_score(node: ET.Element, sim_slot: int | None) -> int:
    resource_id = node.attrib.get("resource-id", "").lower()
    text = node.attrib.get("text", "").strip().lower()
    desc = node.attrib.get("content-desc", "").strip().lower()
    combined = " ".join((resource_id, text, desc))
    score = 0
    if any(fragment in resource_id for fragment in ("send_message_button", "send_button", "send_sms")):
        score += 120
    elif "send" in resource_id:
        score += 70
    if any(word == text or word == desc for word in _SEND_WORDS):
        score += 100
    elif any(word in desc for word in _SEND_WORDS):
        score += 60
    if node.attrib.get("clickable") == "true":
        score += 20
    if node.attrib.get("enabled", "true") != "true":
        score -= 1000
    if any(word in combined for word in ("schedule", "定时", "resend", "重新发送")):
        score -= 500
    if sim_slot is not None:
        sim_hints = (f"sim {sim_slot}", f"sim{sim_slot}", f"卡 {sim_slot}", f"卡{sim_slot}")
        if any(hint in combined for hint in sim_hints):
            score += 45
        other = 2 if sim_slot == 1 else 1
        if any(hint in combined for hint in (f"sim {other}", f"sim{other}", f"卡 {other}", f"卡{other}")):
            score -= 45
    return score


def find_send_target(xml: str, sim_slot: int | None = None) -> UiTarget:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise UiTargetNotFound("无法解析手机 UI；请保持屏幕点亮并解锁") from exc
    candidates: list[UiTarget] = []
    for node in root.iter("node"):
        center = _center(node.attrib.get("bounds", ""))
        if center is None:
            continue
        score = _send_score(node, sim_slot)
        if score < 60:
            continue
        candidates.append(
            UiTarget(
                x=center[0],
                y=center[1],
                resource_id=node.attrib.get("resource-id", ""),
                text=node.attrib.get("text", ""),
                content_description=node.attrib.get("content-desc", ""),
                score=score,
            )
        )
    if not candidates:
        raise UiTargetNotFound("没有识别到短信应用的发送按钮；请执行 dump-ui 并补充机型选择器")
    return max(candidates, key=lambda item: item.score)


def find_send_target_from_activity_dump(
    dump: str, sim_slot: int | None = None
) -> UiTarget:
    """Parse Android's indented View Hierarchy and calculate absolute bounds.

    Some vendor SMS apps (notably older vivo builds) return a null root to
    UIAutomator while still exposing their view tree through `dumpsys activity
    top`. Coordinates in that tree are relative to the parent, so every matched
    node is accumulated through the indentation stack instead of being treated
    as a fixed screen coordinate.
    """
    stack: list[tuple[int, int, int]] = []
    candidates: list[UiTarget] = []
    for line in dump.splitlines():
        match = _ACTIVITY_VIEW_RE.match(line)
        if not match:
            continue
        indent = len(match.group("indent"))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent_x = stack[-1][1] if stack else 0
        parent_y = stack[-1][2] if stack else 0
        x1, y1, x2, y2 = (
            int(match.group("x1")),
            int(match.group("y1")),
            int(match.group("x2")),
            int(match.group("y2")),
        )
        absolute_x = parent_x + x1
        absolute_y = parent_y + y1
        stack.append((indent, absolute_x, absolute_y))
        resource = match.group("resource") or ""
        if x2 <= x1 or y2 <= y1 or "send" not in resource.lower():
            continue
        # Reuse the XML scoring rules by creating the minimal equivalent node.
        node = ET.Element(
            "node",
            {
                "resource-id": resource,
                "text": "",
                "content-desc": "",
                "clickable": "true" if "C" in line.split("{", 1)[1].split()[1] else "false",
                "enabled": "true",
            },
        )
        score = _send_score(node, sim_slot)
        if score < 60:
            continue
        candidates.append(
            UiTarget(
                x=absolute_x + (x2 - x1) // 2,
                y=absolute_y + (y2 - y1) // 2,
                resource_id=resource,
                text="",
                content_description="",
                score=score,
            )
        )
    if not candidates:
        raise UiTargetNotFound("Activity View 层级中也没有识别到可用的发送按钮")
    return max(candidates, key=lambda item: item.score)


def ui_has_send_failure(xml: str) -> bool:
    lowered = xml.lower()
    return any(word in lowered for word in _FAILURE_WORDS)


def find_sim_prompt_target(xml: str, sim_slot: int) -> UiTarget | None:
    """Find a SIM choice only when the screen text looks like a SIM selection dialog."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    all_text = " ".join(
        f"{node.attrib.get('text', '')} {node.attrib.get('content-desc', '')}" for node in root.iter("node")
    ).lower()
    if not any(hint in all_text for hint in ("choose sim", "select sim", "选择sim", "选择 sim", "选择电话卡")):
        return None
    wanted = (f"sim {sim_slot}", f"sim{sim_slot}", f"卡 {sim_slot}", f"卡{sim_slot}")
    for node in root.iter("node"):
        combined = f"{node.attrib.get('text', '')} {node.attrib.get('content-desc', '')}".lower()
        center = _center(node.attrib.get("bounds", ""))
        if center and any(hint in combined for hint in wanted):
            return UiTarget(center[0], center[1], node.attrib.get("resource-id", ""), node.attrib.get("text", ""), node.attrib.get("content-desc", ""), 100)
    return None


class SmsUiSender:
    def __init__(self, adb: AdbClient, ui_wait_seconds: float = 1.2):
        self.adb = adb
        self.ui_wait_seconds = ui_wait_seconds

    def prepare(self, serial: str, phone: str, content: str, wake: bool, sim_slot: int | None) -> UiTarget:
        if wake:
            self.adb.wake_and_dismiss_keyguard(serial)
        self.adb.open_sms_composer(serial, phone, content)
        time.sleep(self.ui_wait_seconds)
        try:
            return find_send_target(self.adb.dump_ui(serial), sim_slot)
        except (AdbError, UiTargetNotFound):
            return find_send_target_from_activity_dump(
                self.adb.dump_activity_top(serial), sim_slot
            )

    def click_and_verify(self, serial: str, target: UiTarget, sim_slot: int | None) -> None:
        self.adb.tap(serial, target.x, target.y)
        time.sleep(self.ui_wait_seconds)
        try:
            xml = self.adb.dump_ui(serial)
        except AdbError:
            # Vendor fallback: after a successful send the composer clears and
            # the active send control disappears. If it remains, keep the job
            # UNKNOWN rather than risking an automatic duplicate.
            try:
                find_send_target_from_activity_dump(
                    self.adb.dump_activity_top(serial), sim_slot
                )
            except UiTargetNotFound:
                return
            raise AdbError("点击后发送按钮仍处于可用状态，无法确认短信是否已发出")
        if sim_slot is not None:
            sim_target = find_sim_prompt_target(xml, sim_slot)
            if sim_target:
                self.adb.tap(serial, sim_target.x, sim_target.y)
                time.sleep(self.ui_wait_seconds)
                xml = self.adb.dump_ui(serial)
        if ui_has_send_failure(xml):
            raise AdbError("短信应用显示发送失败")
