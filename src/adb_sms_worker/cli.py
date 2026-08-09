from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from pathlib import Path

from .adb import AdbClient, AdbError, SmsUiSender, find_send_target
from .config import Settings
from .db import DatabaseError, SmsRepository
from .worker import DevicePool


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="通过多台 Android 手机发送数据库中的待发短信")
    parser.add_argument("--env-file", default=".env", help="配置文件，默认当前目录 .env")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("devices", help="列出 ADB 设备")
    doctor = sub.add_parser("doctor", help="检查设备和数据库（数据库可选）")
    doctor.add_argument("--serial", help="只检查指定设备")
    doctor.add_argument("--with-db", action="store_true", help="同时测试数据库")
    sub.add_parser("migrate", help="创建 adb_sms_dispatch 审计表")
    sub.add_parser("run", help="启动多设备数据库轮询服务")
    dump_ui = sub.add_parser("dump-ui", help="导出当前手机 UI XML，供适配机型")
    dump_ui.add_argument("--serial", required=True)
    dump_ui.add_argument("--output", default="ui-dump.xml")
    test = sub.add_parser("send-test", help="发送一条真实测试短信（不操作数据库）")
    test.add_argument("--serial", required=True)
    test.add_argument("--phone", required=True)
    test.add_argument("--content", required=True)
    test.add_argument("--yes", action="store_true", help="确认产生真实短信发送")
    sub.add_parser("unknown", help="列出点击阶段结果不确定、禁止自动重试的任务")
    resolve = sub.add_parser("resolve", help="人工确认不确定任务")
    resolve.add_argument("message_id", type=int)
    group = resolve.add_mutually_exclusive_group(required=True)
    group.add_argument("--sent", action="store_true", help="确认手机已发送")
    group.add_argument("--retry", action="store_true", help="确认未发送并重新排队")
    resolve.add_argument("--yes", action="store_true")
    return parser


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = Settings.from_env(args.env_file)
    except (ValueError, OSError) as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    )
    adb = AdbClient(settings.adb_path, settings.adb_command_timeout_seconds)

    try:
        if args.command == "devices":
            _json([device.__dict__ for device in adb.devices()])
            return 0

        if args.command == "doctor":
            devices = adb.devices()
            if args.serial:
                devices = [device for device in devices if device.serial == args.serial]
            report: dict[str, object] = {
                "devices": [adb.device_report(device.serial) for device in devices if device.state == "device"],
                "unavailable_devices": [device.__dict__ for device in devices if device.state != "device"],
            }
            if args.with_db:
                settings.validate_database()
                report["database"] = SmsRepository(settings).ping()
            _json(report)
            return 0 if report["devices"] else 1

        if args.command == "dump-ui":
            xml = adb.dump_ui(args.serial)
            output = Path(args.output).resolve()
            output.write_text(xml, encoding="utf-8")
            try:
                target = find_send_target(xml, settings.sim_slot)
                print(f"UI 已保存到 {output}；识别到发送按钮: {target.label} @ ({target.x},{target.y})")
            except AdbError as exc:
                print(f"UI 已保存到 {output}；{exc}")
            return 0

        if args.command == "send-test":
            if not args.yes:
                print("这会真实发送短信并可能产生运营商费用；确认后请添加 --yes", file=sys.stderr)
                return 2
            sender = SmsUiSender(adb, settings.ui_wait_seconds)
            target = sender.prepare(
                args.serial,
                args.phone,
                args.content,
                settings.wake_and_dismiss_keyguard,
                settings.sim_slot,
            )
            print(f"识别到发送按钮: {target.label} @ ({target.x},{target.y})")
            sender.click_and_verify(args.serial, target, settings.sim_slot)
            print("手机端已执行发送；运营商最终送达状态无法由纯 ADB UI 获得")
            return 0

        settings.validate_database()
        repository = SmsRepository(settings)

        if args.command == "migrate":
            repository.migrate()
            repository.assert_schema()
            print("数据库审计表 adb_sms_dispatch 已就绪")
            return 0

        if args.command == "unknown":
            repository.assert_schema()
            _json(repository.unknown_jobs())
            return 0

        if args.command == "resolve":
            if not args.yes:
                print("该操作会改变任务状态；确认后请添加 --yes", file=sys.stderr)
                return 2
            repository.resolve_unknown(args.message_id, "sent" if args.sent else "retry")
            print(f"消息 {args.message_id} 已处理")
            return 0

        if args.command == "run":
            repository.assert_schema()
            recovered = repository.recover_stale_pre_send(settings.stale_claim_seconds)
            if recovered:
                logging.warning("已安全恢复 %s 个尚未进入点击阶段的超时任务", recovered)
            pool = DevicePool(settings, repository, adb)

            def stop_handler(_signum, _frame):
                pool.shutdown()

            signal.signal(signal.SIGINT, stop_handler)
            signal.signal(signal.SIGTERM, stop_handler)
            pool.run_forever()
            return 0

    except (AdbError, DatabaseError, ValueError) as exc:
        logging.error("%s", exc)
        return 1
    return 2
