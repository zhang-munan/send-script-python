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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ADB短信发送审计与幂等保护';

