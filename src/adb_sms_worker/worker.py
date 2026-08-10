from __future__ import annotations

import logging
import threading
import time

from .adb import AdbClient, AdbError, SmsUiSender
from .config import Settings
from .db import DatabaseError, SmsRepository
from .models import SmsJob


logger = logging.getLogger(__name__)


class DeviceWorker:
    def __init__(self, settings: Settings, repository: SmsRepository, adb: AdbClient, serial: str):
        self.settings = settings
        self.repository = repository
        self.adb = adb
        self.serial = serial
        self.sender = SmsUiSender(adb, settings.ui_wait_seconds)

    def process(self, job: SmsJob) -> None:
        armed = False
        try:
            logger.info("设备 %s 开始处理消息 id=%s", self.serial, job.id)
            target = self.sender.prepare(
                self.serial,
                job.receiver_phone,
                job.content,
                self.settings.wake_and_dismiss_keyguard,
                self.settings.sim_slot,
            )
            self.repository.mark_composer_open(job)
            logger.info("消息 id=%s 已识别发送按钮 %s", job.id, target.label)
            # 先持久化 ARMED，再点击。崩溃时宁可人工确认，也绝不自动重复发送。
            if not self.repository.arm_send(job):
                logger.info("消息 id=%s 因收件人拉黑已取消", job.id)
                return
            armed = True
            evidence = self.sender.click_and_verify(
                self.serial,
                target,
                self.settings.sim_slot,
                on_clicked=lambda: self.repository.mark_clicked(job),
            )
            self.repository.mark_success(job)
            logger.info(
                "消息 id=%s 已由设备 %s 执行发送，确认依据=%s",
                job.id,
                self.serial,
                evidence,
            )
        except (AdbError, DatabaseError, ValueError) as exc:
            logger.error("消息 id=%s 处理失败: %s", job.id, exc)
            try:
                self.repository.mark_failed(job, str(exc), armed)
            except DatabaseError:
                logger.exception("消息 id=%s 的失败状态也未能写回数据库", job.id)

    def run(self, stop: threading.Event) -> None:
        logger.info("设备工作线程启动: %s", self.serial)
        while not stop.is_set():
            try:
                if self.adb.get_state(self.serial) != "device":
                    stop.wait(self.settings.poll_interval_seconds)
                    continue
                job = self.repository.claim_next(self.serial)
                if job is None:
                    stop.wait(self.settings.poll_interval_seconds)
                    continue
                self.process(job)
            except (AdbError, DatabaseError):
                logger.exception("设备 %s 的轮询发生错误", self.serial)
                stop.wait(max(self.settings.poll_interval_seconds, 3.0))
        logger.info("设备工作线程停止: %s", self.serial)


class DevicePool:
    def __init__(self, settings: Settings, repository: SmsRepository, adb: AdbClient):
        self.settings = settings
        self.repository = repository
        self.adb = adb
        self.stop = threading.Event()
        self.workers: dict[str, tuple[threading.Thread, threading.Event]] = {}

    def _desired_devices(self) -> set[str]:
        connected = {device.serial for device in self.adb.devices() if device.state == "device"}
        if self.settings.device_serials:
            return connected.intersection(self.settings.device_serials)
        return connected

    def _refresh(self) -> None:
        desired = self._desired_devices()
        for serial in sorted(desired - self.workers.keys()):
            worker_stop = threading.Event()
            worker = DeviceWorker(self.settings, self.repository, self.adb, serial)
            thread = threading.Thread(
                target=worker.run,
                args=(worker_stop,),
                name=f"adb-sms-{serial}",
                daemon=True,
            )
            self.workers[serial] = (thread, worker_stop)
            thread.start()
        for serial in list(self.workers.keys() - desired):
            thread, worker_stop = self.workers.pop(serial)
            worker_stop.set()
            thread.join(timeout=2.0)
            logger.warning("设备离线，停止分配任务: %s", serial)

    def run_forever(self) -> None:
        logger.info("ADB 短信服务启动；每台在线手机拥有一个独立工作线程")
        while not self.stop.is_set():
            try:
                self._refresh()
                if not self.workers:
                    logger.warning("当前没有可用的已授权 Android 设备")
            except AdbError:
                logger.exception("刷新 ADB 设备列表失败")
            self.stop.wait(self.settings.device_refresh_seconds)

    def shutdown(self) -> None:
        self.stop.set()
        for thread, worker_stop in self.workers.values():
            worker_stop.set()
        for thread, _ in self.workers.values():
            thread.join(timeout=5.0)
