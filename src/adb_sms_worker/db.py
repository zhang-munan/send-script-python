from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Iterator

import pymysql
from pymysql.connections import Connection

from .config import Settings
from .models import SmsJob
from .schema import SCHEMA_STATEMENTS


class DatabaseError(RuntimeError):
    pass


class SmsRepository:
    def __init__(self, settings: Settings):
        self.settings = settings

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        try:
            conn = pymysql.connect(
                host=self.settings.db_host,
                port=self.settings.db_port,
                user=self.settings.db_username,
                password=self.settings.db_password,
                database=self.settings.db_database,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False,
                connect_timeout=10,
                read_timeout=20,
                write_timeout=20,
            )
        except pymysql.MySQLError as exc:
            raise DatabaseError(f"数据库连接失败: {exc}") from exc
        try:
            # 定时筛选依赖 NOW()；显式设置每个会话的时区，避免跟随 MySQL
            # 服务器默认时区而出现提前或延后发送。
            with conn.cursor() as cursor:
                cursor.execute("SET time_zone=%s", (self.settings.db_timezone,))
        except pymysql.MySQLError as exc:
            conn.close()
            raise DatabaseError(f"设置数据库会话时区失败: {exc}") from exc
        try:
            yield conn
        except pymysql.MySQLError as exc:
            try:
                conn.rollback()
            except pymysql.MySQLError:
                pass
            raise DatabaseError(f"数据库操作失败: {exc}") from exc
        finally:
            conn.close()

    def ping(self) -> dict[str, object]:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT DATABASE() AS db, VERSION() AS version, "
                "NOW() AS now, @@session.time_zone AS session_timezone"
            )
            return cursor.fetchone()

    def migrate(self) -> None:
        with self.connection() as conn, conn.cursor() as cursor:
            try:
                for statement in SCHEMA_STATEMENTS:
                    cursor.execute(statement)
                conn.commit()
            except pymysql.MySQLError as exc:
                conn.rollback()
                raise DatabaseError(f"初始化发送审计表失败: {exc}") from exc

    def assert_schema(self) -> None:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SHOW TABLES LIKE 'message_info'")
            if cursor.fetchone() is None:
                raise DatabaseError("找不到 message_info 表，请检查 DB_DATABASE")
            cursor.execute("SHOW TABLES LIKE 'adb_sms_dispatch'")
            if cursor.fetchone() is None:
                raise DatabaseError("找不到 adb_sms_dispatch 表，请先执行 migrate")
            cursor.execute("SHOW TABLES LIKE 'message_blacklist'")
            if cursor.fetchone() is None:
                raise DatabaseError("找不到 message_blacklist 表，请先执行后端拉黑功能 SQL")
            cursor.execute("SHOW TABLES LIKE 'message_receiver_notice'")
            if cursor.fetchone() is None:
                raise DatabaseError("找不到 message_receiver_notice 表，请先执行 migrate")
            cursor.execute("SHOW TABLES LIKE 'message_receiver_sms_stat'")
            if cursor.fetchone() is None:
                raise DatabaseError("找不到 message_receiver_sms_stat 表，请先执行 migrate")

    @staticmethod
    def _is_notice_milestone(received_count: int) -> bool:
        return received_count == 1 or (received_count > 0 and received_count % 5 == 0)

    @staticmethod
    def _enabled_param_value(value: object) -> bool:
        if value is True or value == 1:
            return True
        return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}

    def _recipient_notice_enabled(self, cursor) -> bool:
        cursor.execute(
            "SELECT data FROM base_sys_param WHERE keyName=%s LIMIT 1",
            ("recipientNoticeSmsEnabled",),
        )
        row = cursor.fetchone()
        return bool(row) and self._enabled_param_value(row["data"])

    def _notice_already_queued_today(self, cursor, receiver_phone: str) -> bool:
        """Use the database session date so the daily limit follows DB_TIMEZONE."""
        cursor.execute(
            """
            SELECT 1
            FROM message_receiver_notice
            WHERE phone=%s
              AND createTime >= CONCAT(CURDATE(), ' 00:00:00')
              AND createTime < CONCAT(
                DATE_ADD(CURDATE(), INTERVAL 1 DAY), ' 00:00:00'
              )
            LIMIT 1
            """,
            (receiver_phone,),
        )
        return cursor.fetchone() is not None

    def _record_success_and_maybe_queue_notice(
        self, cursor, message_id: int, receiver_phone: str
    ) -> None:
        """Must run in the same transaction as the message success transition."""
        cursor.execute(
            """
            INSERT INTO message_receiver_sms_stat
              (phone, receivedCount, lastNoticeTriggerCount, lastMessageId,
               createTime, updateTime)
            VALUES (%s, 1, 0, %s, NOW(), NOW())
            ON DUPLICATE KEY UPDATE
              receivedCount=receivedCount+1,
              lastMessageId=VALUES(lastMessageId), updateTime=NOW()
            """,
            (receiver_phone, message_id),
        )
        cursor.execute(
            "SELECT receivedCount FROM message_receiver_sms_stat WHERE phone=%s FOR UPDATE",
            (receiver_phone,),
        )
        received_count = int(cursor.fetchone()["receivedCount"])
        if not self._is_notice_milestone(received_count):
            return

        # 开关关闭时仍累计正文短信次数，但不创建腾讯云告知任务。
        if not self._recipient_notice_enabled(cursor):
            return

        # user_info 中保留已注销账号；只要手机号曾经进入系统就不再发送引导。
        cursor.execute(
            "SELECT 1 FROM user_info WHERE phone=%s LIMIT 1",
            (receiver_phone,),
        )
        if cursor.fetchone() is not None:
            return

        # The per-phone statistics row is locked above, so concurrent successful
        # business messages for the same number cannot both pass this daily guard.
        if self._notice_already_queued_today(cursor, receiver_phone):
            return

        cursor.execute(
            """
            INSERT IGNORE INTO message_receiver_notice
              (phone, triggerCount, sourceMessageId, status, attempts,
               createTime, updateTime)
            VALUES (%s, %s, %s, 0, 0, NOW(), NOW())
            """,
            (receiver_phone, received_count, message_id),
        )
        if cursor.rowcount == 1:
            cursor.execute(
                """
                UPDATE message_receiver_sms_stat
                SET lastNoticeTriggerCount=%s, updateTime=NOW()
                WHERE phone=%s
                """,
                (received_count, receiver_phone),
            )

    def recover_stale_pre_send(self, stale_seconds: int) -> int:
        """Requeue only states in which the send button was definitely not armed."""
        with self.connection() as conn, conn.cursor() as cursor:
            try:
                conn.begin()
                cursor.execute(
                    """
                    SELECT d.message_id, m.receiverPhone
                    FROM adb_sms_dispatch d
                    JOIN message_info m ON m.id = d.message_id
                    WHERE d.state IN ('CLAIMED', 'COMPOSER_OPEN')
                      AND m.status = 4
                      AND d.update_time < DATE_SUB(NOW(), INTERVAL %s SECOND)
                    FOR UPDATE
                    """,
                    (stale_seconds,),
                )
                rows = list(cursor.fetchall())
                ids = [int(row["message_id"]) for row in rows]
                if not ids:
                    conn.commit()
                    return 0
                placeholders = ",".join(["%s"] * len(ids))
                cursor.execute(
                    f"UPDATE adb_sms_dispatch SET state='STALE_REQUEUED', last_error='进程在点击发送前中断，已安全重新排队' WHERE message_id IN ({placeholders})",
                    ids,
                )
                cursor.execute(
                    f"UPDATE message_info SET status=3, failReason=NULL, updateTime=NOW() WHERE status=4 AND id IN ({placeholders})",
                    ids,
                )
                conn.commit()
                return len(ids)
            except pymysql.MySQLError as exc:
                conn.rollback()
                raise DatabaseError(f"恢复超时任务失败: {exc}") from exc

    def finalize_clicked(self) -> int:
        """Finish phone-submitted jobs after a crash or transient DB failure."""
        with self.connection() as conn, conn.cursor() as cursor:
            try:
                conn.begin()
                cursor.execute(
                    """
                    SELECT d.message_id, m.receiverPhone
                    FROM adb_sms_dispatch d
                    JOIN message_info m ON m.id=d.message_id
                    WHERE d.state='CLICKED' AND m.status=4
                    FOR UPDATE
                    """
                )
                rows = list(cursor.fetchall())
                ids = [int(row["message_id"]) for row in rows]
                if not ids:
                    conn.commit()
                    return 0
                placeholders = ",".join(["%s"] * len(ids))
                cursor.execute(
                    f"""
                    UPDATE message_info
                    SET status=5, deliveredAt=COALESCE(deliveredAt, NOW()),
                        failReason=NULL, updateTime=NOW()
                    WHERE status=4 AND id IN ({placeholders})
                    """,
                    ids,
                )
                cursor.execute(
                    f"""
                    UPDATE adb_sms_dispatch
                    SET state='DONE', finished_at=NOW(), last_error=NULL
                    WHERE state='CLICKED' AND message_id IN ({placeholders})
                    """,
                    ids,
                )
                for row in rows:
                    self._record_success_and_maybe_queue_notice(
                        cursor,
                        int(row["message_id"]),
                        str(row["receiverPhone"]),
                    )
                conn.commit()
                return len(ids)
            except pymysql.MySQLError as exc:
                conn.rollback()
                raise DatabaseError(f"补写已点击任务状态失败: {exc}") from exc

    def claim_next(self, device_serial: str) -> SmsJob | None:
        token = uuid.uuid4().hex
        channel = f"adb:{device_serial[-12:]}"[:20]
        with self.connection() as conn, conn.cursor() as cursor:
            try:
                conn.begin()
                cursor.execute(
                    """
                    SELECT id, receiverPhone, content, sendType, scheduledAt, status
                    FROM message_info
                    WHERE auditStatus = 1
                      AND status IN (1, 3)
                      AND NOT EXISTS (
                        SELECT 1
                        FROM user_info receiver
                        JOIN message_blacklist blacklist
                          ON blacklist.blockerUserId = receiver.id
                         AND blacklist.blockedUserId = message_info.userId
                         AND blacklist.status = 1
                        WHERE receiver.phone = message_info.receiverPhone
                          AND receiver.status = 1
                      )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM user_info receiver
                        JOIN setting_user setting
                          ON setting.userId = receiver.id
                         AND setting.blockAllSms = 1
                        WHERE receiver.phone = message_info.receiverPhone
                          AND receiver.status = 1
                      )
                      AND (
                        sendType = 1
                        OR (sendType = 2 AND scheduledAt IS NOT NULL AND scheduledAt <= NOW())
                      )
                    ORDER BY
                      CASE WHEN sendType = 2 THEN scheduledAt ELSE createTime END ASC,
                      id ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """
                )
                row = cursor.fetchone()
                if row is None:
                    conn.commit()
                    return None
                message_id = int(row["id"])
                cursor.execute(
                    """
                    INSERT INTO adb_sms_dispatch
                      (message_id, attempt_token, device_serial, state, attempt_count, claimed_at, last_error)
                    VALUES (%s, %s, %s, 'CLAIMED', 1, NOW(), NULL)
                    ON DUPLICATE KEY UPDATE
                      attempt_token=VALUES(attempt_token),
                      device_serial=VALUES(device_serial),
                      state='CLAIMED',
                      attempt_count=attempt_count+1,
                      claimed_at=NOW(), composer_opened_at=NULL, armed_at=NULL,
                      send_clicked_at=NULL, finished_at=NULL, last_error=NULL
                    """,
                    (message_id, token, device_serial),
                )
                cursor.execute(
                    """
                    UPDATE message_info
                    SET status=4, smsChannel=%s, smsMsgId=%s, failReason=NULL, updateTime=NOW()
                    WHERE id=%s AND status=%s AND auditStatus=1
                    """,
                    (channel, f"adb:{token}", message_id, row["status"]),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return None
                conn.commit()
                return SmsJob(
                    id=message_id,
                    receiver_phone=str(row["receiverPhone"]),
                    content=str(row["content"]),
                    attempt_token=token,
                    device_serial=device_serial,
                    send_type=int(row["sendType"]),
                    scheduled_at=row["scheduledAt"],
                )
            except pymysql.MySQLError as exc:
                conn.rollback()
                raise DatabaseError(f"抢占短信任务失败: {exc}") from exc

    def _set_dispatch_state(self, job: SmsJob, state: str, timestamp_column: str) -> None:
        allowed = {"composer_opened_at", "armed_at", "send_clicked_at", "finished_at"}
        if timestamp_column not in allowed:
            raise ValueError("非法时间字段")
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                f"UPDATE adb_sms_dispatch SET state=%s, {timestamp_column}=NOW() WHERE message_id=%s AND attempt_token=%s",
                (state, job.id, job.attempt_token),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise DatabaseError(f"任务 {job.id} 的发送令牌已失效")
            conn.commit()

    def mark_composer_open(self, job: SmsJob) -> None:
        self._set_dispatch_state(job, "COMPOSER_OPEN", "composer_opened_at")

    def arm_send(self, job: SmsJob) -> bool:
        """Atomically recheck all recipient blocks before enabling the click."""
        with self.connection() as conn, conn.cursor() as cursor:
            try:
                conn.begin()
                cursor.execute(
                    """
                    SELECT CASE
                             WHEN setting.blockAllSms = 1 THEN 'ALL'
                             WHEN blacklist.id IS NOT NULL THEN 'SENDER'
                           END AS block_type
                    FROM message_info message
                    JOIN user_info receiver
                      ON receiver.phone = message.receiverPhone
                     AND receiver.status = 1
                    LEFT JOIN setting_user setting
                      ON setting.userId = receiver.id
                    LEFT JOIN message_blacklist blacklist
                      ON blacklist.blockerUserId = receiver.id
                     AND blacklist.blockedUserId = message.userId
                     AND blacklist.status = 1
                    WHERE message.id=%s
                      AND (setting.blockAllSms = 1 OR blacklist.id IS NOT NULL)
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (job.id,),
                )
                blocked = cursor.fetchone()
                if blocked is not None:
                    blocks_all = blocked["block_type"] == "ALL"
                    dispatch_error = (
                        "收件人已屏蔽所有短信" if blocks_all else "收件人已拉黑发送者"
                    )
                    message_error = f"{dispatch_error}，系统自动取消"
                    cursor.execute(
                        """
                        UPDATE adb_sms_dispatch
                        SET state='BLOCKED', finished_at=NOW(), last_error=%s
                        WHERE message_id=%s AND attempt_token=%s AND state IN ('CLAIMED', 'COMPOSER_OPEN')
                        """,
                        (dispatch_error, job.id, job.attempt_token),
                    )
                    cursor.execute(
                        """
                        UPDATE message_info
                        SET status=7, failReason=%s, updateTime=NOW()
                        WHERE id=%s AND status=4 AND smsMsgId=%s
                        """,
                        (message_error, job.id, f"adb:{job.attempt_token}"),
                    )
                    conn.commit()
                    return False

                cursor.execute(
                    """
                    UPDATE adb_sms_dispatch
                    SET state='ARMED', armed_at=NOW()
                    WHERE message_id=%s AND attempt_token=%s AND state='COMPOSER_OPEN'
                    """,
                    (job.id, job.attempt_token),
                )
                if cursor.rowcount != 1:
                    raise DatabaseError(f"任务 {job.id} 的发送令牌已失效")
                conn.commit()
                return True
            except (pymysql.MySQLError, DatabaseError) as exc:
                conn.rollback()
                if isinstance(exc, DatabaseError):
                    raise
                raise DatabaseError(f"发送前拉黑校验失败: {exc}") from exc

    def mark_clicked(self, job: SmsJob) -> None:
        """Persist that adb accepted the tap before doing best-effort UI checks."""
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE adb_sms_dispatch
                SET state='CLICKED', send_clicked_at=NOW(), last_error=NULL
                WHERE message_id=%s AND attempt_token=%s AND state='ARMED'
                """,
                (job.id, job.attempt_token),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise DatabaseError(f"任务 {job.id} 不在 ARMED 状态，无法记录点击")
            conn.commit()

    def mark_success(self, job: SmsJob) -> None:
        with self.connection() as conn, conn.cursor() as cursor:
            try:
                conn.begin()
                cursor.execute(
                    """
                    UPDATE adb_sms_dispatch
                    SET state='DONE', send_clicked_at=COALESCE(send_clicked_at, NOW()),
                        finished_at=NOW(), last_error=NULL
                    WHERE message_id=%s AND attempt_token=%s AND state IN ('ARMED', 'CLICKED')
                    """,
                    (job.id, job.attempt_token),
                )
                if cursor.rowcount != 1:
                    raise DatabaseError(f"任务 {job.id} 不在 ARMED 状态")
                # 项目现有状态 5 名为“已送达”，ADB 实际只能证明手机端已执行发送。
                cursor.execute(
                    """
                    UPDATE message_info
                    SET status=5, deliveredAt=NOW(), failReason=NULL, updateTime=NOW()
                    WHERE id=%s AND status=4 AND smsMsgId=%s
                    """,
                    (job.id, f"adb:{job.attempt_token}"),
                )
                if cursor.rowcount != 1:
                    raise DatabaseError(f"任务 {job.id} 的 message_info 状态已变化")
                self._record_success_and_maybe_queue_notice(
                    cursor, job.id, job.receiver_phone
                )
                conn.commit()
            except (pymysql.MySQLError, DatabaseError) as exc:
                conn.rollback()
                if isinstance(exc, DatabaseError):
                    raise
                raise DatabaseError(f"完成任务 {job.id} 失败: {exc}") from exc

    def mark_failed(self, job: SmsJob, reason: str, send_was_armed: bool) -> None:
        reason = reason[:200]
        dispatch_reason = reason[:500]
        state = "UNKNOWN" if send_was_armed else "FAILED"
        with self.connection() as conn, conn.cursor() as cursor:
            try:
                conn.begin()
                cursor.execute(
                    """
                    UPDATE adb_sms_dispatch
                    SET state=%s, finished_at=NOW(), last_error=%s
                    WHERE message_id=%s AND attempt_token=%s
                    """,
                    (state, dispatch_reason, job.id, job.attempt_token),
                )
                if send_was_armed:
                    # 不自动重试：点击可能已被手机接受，重试可能发送两遍。
                    cursor.execute(
                        "UPDATE message_info SET failReason=%s, updateTime=NOW() WHERE id=%s AND status=4",
                        (f"发送结果待人工确认：{reason}"[:200], job.id),
                    )
                else:
                    cursor.execute(
                        "UPDATE message_info SET status=6, failReason=%s, updateTime=NOW() WHERE id=%s AND status=4",
                        (reason, job.id),
                    )
                conn.commit()
            except pymysql.MySQLError as exc:
                conn.rollback()
                raise DatabaseError(f"记录任务失败状态失败: {exc}") from exc

    def unknown_jobs(self) -> list[dict[str, object]]:
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.message_id, d.device_serial, d.state, d.armed_at,
                       d.send_clicked_at, d.last_error, d.update_time,
                       m.receiverPhoneMask, m.status
                FROM adb_sms_dispatch d
                JOIN message_info m ON m.id=d.message_id
                WHERE d.state IN ('ARMED', 'CLICKED', 'UNKNOWN')
                ORDER BY d.update_time ASC
                """
            )
            return list(cursor.fetchall())

    def resolve_unknown(self, message_id: int, resolution: str) -> None:
        if resolution not in {"sent", "retry"}:
            raise ValueError("resolution 必须是 sent 或 retry")
        with self.connection() as conn, conn.cursor() as cursor:
            try:
                conn.begin()
                cursor.execute(
                    "SELECT state FROM adb_sms_dispatch WHERE message_id=%s FOR UPDATE",
                    (message_id,),
                )
                row = cursor.fetchone()
                if row is None or row["state"] not in {"ARMED", "CLICKED", "UNKNOWN"}:
                    raise DatabaseError("该任务不是待人工确认状态")
                if resolution == "sent":
                    cursor.execute(
                        "UPDATE adb_sms_dispatch SET state='MANUAL_SENT', finished_at=NOW(), last_error=NULL WHERE message_id=%s",
                        (message_id,),
                    )
                    cursor.execute(
                        "UPDATE message_info SET status=5, deliveredAt=NOW(), failReason=NULL, updateTime=NOW() WHERE id=%s AND status=4",
                        (message_id,),
                    )
                    message_updated = cursor.rowcount == 1
                    if message_updated:
                        cursor.execute(
                            "SELECT receiverPhone FROM message_info WHERE id=%s",
                            (message_id,),
                        )
                        message = cursor.fetchone()
                        self._record_success_and_maybe_queue_notice(
                            cursor, message_id, str(message["receiverPhone"])
                        )
                else:
                    cursor.execute(
                        "UPDATE adb_sms_dispatch SET state='MANUAL_RETRY', finished_at=NOW(), last_error='人工确认未发送，重新排队' WHERE message_id=%s",
                        (message_id,),
                    )
                    cursor.execute(
                        "UPDATE message_info SET status=3, retryCount=retryCount+1, failReason=NULL, updateTime=NOW() WHERE id=%s AND status=4",
                        (message_id,),
                    )
                    message_updated = cursor.rowcount == 1
                if not message_updated:
                    raise DatabaseError("message_info 状态不是发送中，未作修改")
                conn.commit()
            except (pymysql.MySQLError, DatabaseError) as exc:
                conn.rollback()
                if isinstance(exc, DatabaseError):
                    raise
                raise DatabaseError(f"人工处理任务失败: {exc}") from exc

    def resolve_all_unknown(self, resolution: str) -> int:
        jobs = self.unknown_jobs()
        resolved = 0
        for row in jobs:
            self.resolve_unknown(int(row["message_id"]), resolution)
            resolved += 1
        return resolved
