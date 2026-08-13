SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS `adb_sms_dispatch` (
      `message_id` BIGINT NOT NULL COMMENT 'message_info.id',
      `attempt_token` CHAR(32) NOT NULL,
      `device_serial` VARCHAR(128) NOT NULL,
      `state` VARCHAR(32) NOT NULL,
      `attempt_count` INT UNSIGNED NOT NULL DEFAULT 1,
      `claimed_at` DATETIME NOT NULL,
      `composer_opened_at` DATETIME NULL,
      `armed_at` DATETIME NULL COMMENT '写入后才允许点击发送，防止自动重复',
      `send_clicked_at` DATETIME NULL,
      `finished_at` DATETIME NULL,
      `last_error` VARCHAR(500) NULL,
      `create_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      `update_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (`message_id`),
      UNIQUE KEY `uk_adb_sms_attempt_token` (`attempt_token`),
      KEY `idx_adb_sms_state_update` (`state`, `update_time`),
      KEY `idx_adb_sms_device` (`device_serial`, `update_time`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ADB短信发送审计与幂等保护'
    """,
    """
    CREATE TABLE IF NOT EXISTS `message_receiver_sms_stat` (
      `id` BIGINT NOT NULL AUTO_INCREMENT,
      `phone` VARCHAR(20) NOT NULL,
      `receivedCount` INT UNSIGNED NOT NULL DEFAULT 0,
      `lastNoticeTriggerCount` INT UNSIGNED NOT NULL DEFAULT 0,
      `lastMessageId` BIGINT NULL,
      `createTime` VARCHAR(255) NOT NULL,
      `updateTime` VARCHAR(255) NOT NULL,
      `tenantId` INT NULL,
      PRIMARY KEY (`id`),
      UNIQUE KEY `uk_receiver_sms_stat_phone` (`phone`),
      KEY `idx_receiver_sms_stat_create_time` (`createTime`),
      KEY `idx_receiver_sms_stat_update_time` (`updateTime`),
      KEY `idx_receiver_sms_stat_tenant_id` (`tenantId`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='收件手机号业务短信累计'
    """,
    """
    CREATE TABLE IF NOT EXISTS `message_receiver_notice` (
      `id` BIGINT NOT NULL AUTO_INCREMENT,
      `phone` VARCHAR(20) NOT NULL,
      `triggerCount` INT UNSIGNED NOT NULL,
      `sourceMessageId` BIGINT NOT NULL,
      `status` TINYINT NOT NULL DEFAULT 0,
      `attempts` INT UNSIGNED NOT NULL DEFAULT 0,
      `nextRetryAt` DATETIME NULL,
      `providerMsgId` VARCHAR(128) NULL,
      `lastError` VARCHAR(500) NULL,
      `sentAt` DATETIME NULL,
      `createTime` VARCHAR(255) NOT NULL,
      `updateTime` VARCHAR(255) NOT NULL,
      `tenantId` INT NULL,
      PRIMARY KEY (`id`),
      UNIQUE KEY `uk_receiver_notice_phone_count` (`phone`, `triggerCount`),
      UNIQUE KEY `uk_receiver_notice_source_message` (`sourceMessageId`),
      KEY `idx_receiver_notice_phone` (`phone`),
      KEY `idx_receiver_notice_status_retry` (`status`, `nextRetryAt`),
      KEY `idx_receiver_notice_create_time` (`createTime`),
      KEY `idx_receiver_notice_update_time` (`updateTime`),
      KEY `idx_receiver_notice_tenant_id` (`tenantId`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='腾讯云收件人引导短信任务'
    """,
    """
    INSERT INTO message_receiver_sms_stat
      (phone, receivedCount, lastNoticeTriggerCount, lastMessageId,
       createTime, updateTime)
    SELECT receiverPhone, COUNT(*), 0, MAX(id), NOW(), NOW()
    FROM message_info
    WHERE status=5 AND receiverPhone IS NOT NULL AND receiverPhone<>''
    GROUP BY receiverPhone
    ON DUPLICATE KEY UPDATE phone=VALUES(phone)
    """,
)
