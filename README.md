# ADB 多手机短信执行服务

这个 Python 服务定时读取现有 MySQL `message_info` 表，把审核通过且到达发送时间的任务分配给 USB 连接的 Android 手机。每台在线手机有一个独立工作线程，数据库使用 `SELECT ... FOR UPDATE SKIP LOCKED` 原子抢占，因此同一条任务不会同时分配给两台手机。

数据库需为 MySQL 8.0 或兼容 `SKIP LOCKED` 的版本。

> 合规提示：只能向已同意接收的号码发送合法内容，并遵守运营商频率、营销短信退订、个人信息与当地通信法规。请先用测试号码和低频率验证。

## 设计边界

纯 ADB 不能调用 Android 的公开“无界面发送并返回运营商送达回执”能力。本项目采用所有非 Root 安卓机都能使用的流程：打开系统默认短信应用、填入号码和正文、识别并点击发送按钮。

- 数据库状态 `5` 在这个执行器中表示“手机短信应用已执行发送”，不是运营商确认送达。
- 真正精准的 `SENT` / `DELIVERED` 回执需要另做一个安装在手机上的 Android Helper App，通过 `SmsManager` 和广播接收器回传结果。Python/ADB 层的调度、审计表和多设备架构可以继续复用。
- 不同厂商的主要差异是默认短信应用的 UI、锁屏策略、后台限制和双卡选择。发送按钮使用资源 ID、文字和无障碍描述综合识别，不使用固定坐标。
- 进程在点击阶段异常时，任务会进入 `UNKNOWN`，不会自动重发，避免收件人收到两遍。
- ADB 成功执行精确按钮点击后会立即持久化 `CLICKED`，随后事务回写业务状态 `5`；vivo 等拒绝 UIAutomator 读取的机型不再因为按钮 View 保留而误报失败。
- 若进程在 `CLICKED` 后、业务状态回写前中断，下次启动会自动补写为状态 `5`，不会再次发送。

## 1. 手机准备

1. Android 手机打开“开发者选项”和“USB 调试”。
2. 数据线连接电脑，手机上选择“允许这台电脑进行 USB 调试”，最好勾选始终允许。
3. 保持手机有可用 SIM、能正常手工发短信，并设置好默认短信应用和默认发送 SIM。
4. 测试阶段关闭锁屏密码或保持屏幕解锁。程序只能滑过无密码锁屏，不能也不会绕过 PIN/指纹/图案。
5. 部分国产系统需额外打开“USB 调试（安全设置）”或“允许通过 USB 模拟点击”。

检查连接：

```bash
cd /Users/zhangmunan/project/bangni-shuochukou/adb_sms_worker
adb devices -l
```

状态必须是 `device`。`unauthorized` 表示还未在手机上确认授权。

## 2. 安装

支持 Python 3.9 及以上版本。建议优先使用系统中稳定的 Python 3.10–3.13：

```bash
cd /Users/zhangmunan/project/bangni-shuochukou/adb_sms_worker
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
cp .env.example .env
```

编辑 `.env`，填写和后端相同的数据库地址。密码只放 `.env`，该文件已被 Git 忽略。
`DB_TIMEZONE` 默认使用 `+08:00`；后端和 worker 必须保持一致，定时发送的
`scheduledAt` 才会和 MySQL `NOW()` 在同一时区比较。

发布拉黑功能时，必须先执行后端提供的
`backend/sql/20260808_add_message_blacklist.sql`。发送器启动时会检查
`message_blacklist` 表；领取任务和点击发送前都会再次校验有效拉黑关系。

初始化幂等审计表：

```bash
adb-sms-worker migrate
```

该命令会创建 `adb_sms_dispatch`、`message_receiver_sms_stat` 和
`message_receiver_notice`，不会修改或删除已有业务记录。它会把历史状态为 `5`
的正文短信汇总为收件次数，但不会补发历史告知短信。

## 3. 真机诊断与单条验证

```bash
adb-sms-worker devices
adb-sms-worker doctor --serial 设备序列号 --with-db
```

先用自己的测试号码真实发送一条：

先执行只打开草稿、绝不点击发送的安全识别测试：

```bash
adb-sms-worker prepare-test --serial 设备序列号
```

识别成功后，再用自己的测试号码真实发送一条：

```bash
adb-sms-worker send-test \
  --serial 设备序列号 \
  --phone 13800138000 \
  --content 'ADB短信联调测试' \
  --yes
```

`--yes` 是防止误发的强制确认。若程序打开短信编辑页但没有识别按钮：

```bash
adb-sms-worker dump-ui --serial 设备序列号 --output ui-dump.xml
```

把以下内容发给开发者即可继续适配：手机品牌型号、Android 版本、默认短信应用名称、是否双卡，以及 `ui-dump.xml`。XML 可能含当前屏幕文字，发送前请先检查并移除隐私内容。

## 4. 启动正式轮询

```bash
adb-sms-worker run
```

任务筛选条件：

- `auditStatus = 1`（审核通过）；
- `status IN (1, 3)`（新消息统一使用 `3`，`1` 仅用于兼容历史记录）；
- 立即发送，或 `scheduledAt <= NOW()`。
- 收件账号没有拉黑该消息的发送账号。

正文短信成功后，worker 会在同一个数据库事务中累计收件手机号次数：第 1 次以及
第 5、10、15……次时，若手机号从未出现在 `user_info`，写入一条
`message_receiver_notice` 告知任务。Node 后端每 10 秒消费该任务并通过
`sms-tx` 腾讯云插件发送；发送前会再次检查手机号是否已经进入系统。
同一手机号同一自然日最多创建一条告知任务；后端发送前还会检查当天是否已成功发送，
避免前一天任务的延迟重试导致同一天收到多条告知短信。自然日按 `DB_TIMEZONE` 对应的
数据库会话日期计算。
只有 `base_sys_param.recipientNoticeSmsEnabled` 的参数值为 `1` 时才创建和发送告知
任务；默认值为 `0`，关闭时正文短信次数仍会累计。

抢占后 `message_info.status` 变为 `4`。手机端完成点击后变为 `5`；在点击前失败变为 `6`。多台手机可同时连接，留空 `ADB_DEVICE_SERIALS` 会自动使用全部在线且已授权的设备；也可填逗号分隔的白名单。

## 5. 不确定任务处理

查询因点击阶段异常而禁止自动重试的任务：

```bash
adb-sms-worker unknown
```

先查看对应手机的短信会话，再二选一：

```bash
# 手机上能看到已发出的短信
adb-sms-worker resolve 123 --sent --yes

# 手机上确认没有发出，重新排队
adb-sms-worker resolve 123 --retry --yes

# 已逐条在手机会话中确认全部发送后，可批量回写
adb-sms-worker resolve-all --sent --yes
```

## 6. 多机与机型注意事项

- 每台手机同一时间只处理一条，手机数量决定并发数。
- 双卡优先在系统短信应用里设置固定默认卡；也可在 `.env` 设置 `SIM_SLOT=1` 或 `2`。
- 华为/荣耀、小米/红米、OPPO/一加、vivo、三星和 Google Messages 的资源 ID 可能不同。当前选择器覆盖常见 `send_button` / `send_message_button`，具体以真机 `dump-ui` 为准。
- 短信应用升级后应重新跑一次 `send-test`。
- 电脑睡眠、数据线松动、手机锁屏、欠费、无信号、系统弹窗都会影响发送。生产电脑应禁用自动睡眠，并使用带独立供电的 USB Hub。

## 7. 测试

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

单元测试不会发送短信，也不会访问数据库。
