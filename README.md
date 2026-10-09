# notify

`notify` 是一个基于 Python 标准库的邮件命令行工具，适合在脚本、定时任务和训练任务结束时发送邮件，也可以通过 IMAP 接收并查看邮件。发送支持纯文本、HTML、附件、管道输入、多收件人和抄送；接收支持查看正文、筛选未读邮件和保存附件，不需要安装第三方 Python 包。

## 环境要求

- Python 3.10 或更高版本
- 发送邮件时，需要已开通 SMTP 服务的邮箱账号及 SMTP 密码或授权码
- 接收邮件时，需要已开通 IMAP 服务的邮箱账号及 IMAP 密码或授权码
- QQ、163 等邮箱通常要求使用授权码，而不是网页登录密码

## 快速开始

```bash
# 1. 生成本机配置文件，默认位置为 ~/.notify/config.ini
./notify.py config init

# 2. 编辑配置
#    填写发件邮箱、SMTP 授权码和默认收件人
${EDITOR:-vi} ~/.notify/config.ini

# 3. 检查生效配置（授权码会打码显示）
./notify.py config show

# 4. 发送测试邮件
./notify.py test
```

`config init` 会将配置文件权限设为 `600`，仅当前用户可读写。若文件已存在，命令会拒绝覆盖；确认需要重建时才使用 `--force`。

也可以先复制 [`config.example.ini`](config.example.ini) 再填写：

```bash
cp config.example.ini ~/.notify/config.ini
chmod 600 ~/.notify/config.ini
${EDITOR:-vi} ~/.notify/config.ini
```

## 配置说明

默认配置路径为 `~/.notify/config.ini`。可通过 `--config FILE` 或 `NOTIFY_CONFIG` 指定其他配置文件。字段值按以下顺序合并：**环境变量 > 配置文件 > 内置默认值**；`--config` 用来选择配置文件，不是逐字段的命令行覆盖参数。

```ini
[smtp]
server = smtp.qq.com
port = 465
ssl = auto
timeout = 15

[auth]
sender_name = notify
sender_email = your-account@qq.com
# 将下一项设为邮箱服务商生成的 SMTP 授权码
sender_pass =
# 如收件账号不同，再填写这两项；留空则复用发件账号和授权码
imap_user =
imap_pass =

[mail]
receiver_email = recipient@example.com

[imap]
server = imap.qq.com
port = 993
ssl = auto
timeout = 15
```

将示例邮箱替换为自己的地址，并将 `sender_pass` 替换为邮箱服务商生成的 SMTP 授权码。多个默认收件人用英文逗号分隔。接收邮件时，IMAP 默认复用 `sender_email` 和 `sender_pass`；如果收件账号不同，在 `[auth]` 中填写 `imap_user` 和 `imap_pass`。QQ 邮箱通常可以复用同一授权码，但需要在邮箱设置中分别开启 SMTP 和 IMAP 服务。

### SMTP 加密方式

`ssl = auto` 会按端口选择加密方式：465 使用 SSL，25 或 587 使用 STARTTLS；其他端口默认使用 SSL。也可以明确指定 `ssl`、`starttls` 或 `none`。

QQ 邮箱通常使用 `smtp.qq.com:465`（SSL）或 `smtp.qq.com:587`（STARTTLS）。请以邮箱服务商提供的服务器地址、端口和加密要求为准。

接收邮件使用邮箱服务商提供的 IMAP 地址。QQ 邮箱通常使用 `imap.qq.com:993`（SSL）；支持 `ssl`、`starttls` 和 `none`，`auto` 会按端口判断：993 使用 SSL，143 使用 STARTTLS。

### 环境变量

| 配置项 | 环境变量 |
| --- | --- |
| 配置文件路径 | `NOTIFY_CONFIG` |
| SMTP 服务器 | `NOTIFY_SMTP_SERVER` |
| SMTP 端口 | `NOTIFY_SMTP_PORT` |
| 加密方式 | `NOTIFY_SMTP_SSL` |
| 超时秒数 | `NOTIFY_SMTP_TIMEOUT` |
| 发件人显示名 | `NOTIFY_SENDER_NAME` |
| 发件人邮箱 | `NOTIFY_SENDER_EMAIL` |
| SMTP 密码或授权码 | `NOTIFY_SENDER_PASS` |
| 默认收件人 | `NOTIFY_RECEIVER_EMAIL` |
| IMAP 服务器 | `NOTIFY_IMAP_SERVER` |
| IMAP 端口 | `NOTIFY_IMAP_PORT` |
| IMAP 加密方式 | `NOTIFY_IMAP_SSL` |
| IMAP 超时秒数 | `NOTIFY_IMAP_TIMEOUT` |
| IMAP 登录账号 | `NOTIFY_IMAP_USER` |
| IMAP 密码或授权码 | `NOTIFY_IMAP_PASS` |

`NOTIFY_IMAP_USER` 和 `NOTIFY_IMAP_PASS` 未设置时，接收命令会使用 `NOTIFY_SENDER_EMAIL` 和 `NOTIFY_SENDER_PASS`。如果用环境变量配置不同的收件账号，请同时设置这两个 IMAP 环境变量。

例如，在 CI 或临时任务中可以通过环境变量提供配置，而不创建配置文件。请使用 CI 的密钥管理功能保存授权码，不要把真实授权码写入仓库或脚本。

## 命令用法

### 查看帮助

```bash
./notify.py --help
./notify.py send --help
./notify.py test --help
./notify.py receive --help
./notify.py watch --help
./notify.py config --help
```

### 发送测试邮件

```bash
# 发到配置中的默认收件人
./notify.py test

# 临时指定收件人，可重复传入或用逗号分隔
./notify.py test -t person@example.com
./notify.py test -t first@example.com,second@example.com
```

### 发送通知

```bash
# 纯文本
./notify.py send -s "任务完成" -b "训练结束，loss=0.05"

# 从文件读取正文
./notify.py send -s "日报" --body-file daily.txt

# 从标准输入读取正文
cat train.log | ./notify.py send -s "训练日志"

# HTML 正文
./notify.py send -s "结果" --html '<h1>完成</h1><p>acc=0.98</p>'
./notify.py send -s "结果" --html-file result.html

# 添加一个或多个附件
./notify.py send -s "打包结果" -b "请查收附件" -a result.csv -a model.bin

# 多位收件人和抄送
./notify.py send -s "通知" -b "任务已完成" \
  -t first@example.com,second@example.com --cc copy@example.com
```

`-t/--to` 可重复使用，未指定时使用配置中的 `mail.receiver_email`。`--cc` 指定抄送人；抄送人也会收到邮件。正文可使用纯文本、HTML、文件或管道输入。

### 接收邮件

接收命令通过 IMAP 从邮箱读取邮件，默认查看 `INBOX` 最新 10 封；读取过程默认不修改邮件的已读状态。邮件主题、发件人、日期和纯文本正文会打印到终端。仅有 HTML 正文时会尝试提取为纯文本。

```bash
# 查看收件箱最新 10 封邮件
./notify.py receive

# 查看最新 5 封未读邮件
./notify.py receive --unseen --limit 5

# 读取指定邮件的 IMAP UID（UID 可从前一次输出中查看）
./notify.py receive --id 123456

# 读取时将邮件标为已读
./notify.py receive --unseen --mark-seen

# 指定邮箱文件夹，并将附件保存到本地目录
./notify.py receive --mailbox INBOX --attachments-dir ./received-files
```

`--id` 使用 IMAP UID（每个邮箱文件夹内的邮件 UID），不是收件箱列表中的序号。附件只有指定 `--attachments-dir` 后才会下载；文件名会添加 UID 前缀以便区分。不同邮箱文件夹可以通过 `--mailbox` 指定，例如 `--mailbox "已发送"`；文件夹名称需与邮箱服务器显示的名称一致，支持中文名称。

### 监听新邮件

`watch` 会持续轮询 IMAP 收件箱，默认每 10 秒检查一次新邮件；收到后会把发件人、主题、正文和附件信息打印出来。默认只监听命令启动后的新邮件，不会修改已读状态。可用 `--once` 等到第一封符合条件的邮件后退出，适合 Agent 等待回复；`--timeout` 设置最长等待秒数，超时退出码为 `3`。按 `Ctrl+C` 可停止持续监听。

```bash
# 等待新邮件，最多 30 分钟；收到第一封后退出
./notify.py watch --once --timeout 1800

# 只等指定发件人发来的、主题包含“回复”的邮件
./notify.py watch --once --from sender@example.com --subject "回复" --timeout 3600

# 命令启动前已到达但仍未读的回复也纳入匹配
./notify.py watch --include-unseen --once --from sender@example.com --timeout 1800

# 持续监听，每 3 秒检查一次，并将匹配到的邮件标为已读
./notify.py watch --interval 3 --mark-seen
```

`--from` 和 `--subject` 是不区分大小写的包含匹配。`--include-unseen` 可避免回复在监听启动前刚好到达而被错过；若邮箱里有其他未读邮件，建议同时指定筛选条件。监听时使用 `--attachments-dir DIR` 可保存匹配邮件中的附件。

## HTML 邮件模板

预置模板位于 `templates/` 目录：

所有模板均按移动端邮件阅读场景优化，优先兼容 iPhone / iOS 邮件客户端：使用响应式单栏布局、移动端缩小留白、显式文字颜色、长路径与代码自动换行、窄屏表格压缩以及更大的按钮触控区域。

| 模板名称 | 文件 | 适用场景 |
| --- | --- | --- |
| Apple 风格 | `templates/apple.html` | 产品通知、高层摘要、正式状态同步 |
| Minimal 极简风 | `templates/minimal.html` | 日常通知、任务结果、简短汇报 |
| Business Report 企业报告风 | `templates/business-report.html` | 周报、质量报告、指标汇报、正式汇报 |
| Alert 告警通知风 | `templates/alert.html` | 异常、风险、监控告警、处置通知 |
| Dark Tech 深色科技风 | `templates/dark-tech.html` | 系统状态、Agent 输出、技术报告、运维通知 |

模板均为独立 HTML 文件，可直接复制后替换示例内容，也可通过 `--html-file` 直接发送：

```bash
./notify.py send -s "模板测试" -b "请查看 HTML 正文" --html-file templates/apple.html
```

另外，`templates/template-showcase.html` 用于集中预览上述 5 套模板风格。

### 使用其他配置文件

```bash
# 初始化到指定位置
./notify.py config init --config ./config.local.ini

# 对该命令使用指定配置
./notify.py --config ./config.local.ini test
./notify.py send --config ./config.local.ini -s "通知" -b "完成"
```

## 同步部署到 berry

在本机项目目录运行一键同步脚本：

```bash
./sync_to_berry.sh
```

脚本通过 SSH 主机别名 `berry` 将项目同步到 `/home/berry/auto_notification`，需要本机已配置该 SSH 别名，并安装 `rsync`。同步前会在 berry 上创建目标目录；同步完成后会确认远端 `notify.py` 和 `README.md` 存在。

脚本不会删除 berry 上的文件，并会跳过 `.git`、缓存、`.video_agent`、`.env` 及本地/私密配置文件，因此 berry 上已有的运行日志、专用脚本和配置会保留。

## Berry 邮件补发队列（retry_thsottiaux）

`retry_thsottiaux.py` 是部署在 Berry 上的独立补发工作程序，用于 @thsottiaux 的 ChatGPT / Codex 额度重置通知。它把「发现事件」与「发送邮件」拆开：上游定时任务只负责把待发正文写入本地队列，实际投递由 Berry 的 cron 每 10 分钟驱动本程序完成。这样即使上游发起发送的工具调用偶发失败，只要正文已入队，邮件仍会被投出。整体语义是**至少一次投递 + 已发送 ID 去重**，不是严格的 exactly-once。

### pending 文件协议

| 路径 | 作用 |
| --- | --- |
| `pending/thsottiaux_reset/<status_id>.txt` | 待发邮件正文；文件名必须是 16~22 位纯数字的 X status ID |
| `pending/thsottiaux_reset/sent_ids.txt` | 已成功发送的 status ID 台账，每行一个 |
| `pending/thsottiaux_reset/.retry.lock` | 单实例运行锁（flock） |

生产者约定：

- 正文为 UTF-8 纯文本，写入 `pending/thsottiaux_reset/<status_id>.txt` 即视为入队；邮件主题固定为「ChatGPT 额度重置通知 - @thsottiaux」。
- 同一 status ID 已发送或已入队时，不得重复入队。
- 入队成功后由本程序负责发送与清理；生产者不直接调用 `notify.py` 发送，也不负责清理 sent ID。

### 工作逻辑

1. 只接受文件名为 16~22 位纯数字的 `*.txt`，跳过 `sent_ids.txt`。
2. 已发送过的 ID 直接清理对应文件；写入不足 30 秒的文件本轮暂缓，避免读到写了一半的正文；空正文保留并记日志。
3. 通过 `subprocess.run` 参数数组调用 `notify.py send --body-file`，不把正文拼进 shell 命令，也不执行正文中的内容；单封发送超时 60 秒。
4. 退出码为 0 且标准输出包含「已发送至」才确认成功；成功后先把 ID 追加进 `sent_ids.txt` 并 `fsync`，再删除 pending 文件。失败则保留文件，等下一轮重试。
5. `flock` 防并发，单次最多处理 10 个文件；日志写入自动轮转的 `retry_thsottiaux.log`。

### Cron 部署

以下条目部署在 Berry 当前用户的 crontab 中（每 10 分钟一次）。**Cron 属于服务器环境配置，不会随代码同步自动迁移**，在其他主机部署时需单独创建：

```cron
*/10 * * * * /usr/bin/python3 /home/berry/auto_notification/retry_thsottiaux.py >/dev/null 2>&1 # thsottiaux-email-retry
```

同一队列只允许一个消费者主机。不要在开发机和 Berry 上同时运行补发程序，否则可能重复发送。

### 运维命令

在 Berry 上执行：

```bash
cd /home/berry/auto_notification
python3 retry_thsottiaux.py --dry-run   # 只检查队列，不发送
tail -n 50 retry_thsottiaux.log         # 查看补发日志
crontab -l | grep thsottiaux-email-retry
```

### 故障边界

- SMTP 成功但 `sent_ids.txt` 写入完成前进程异常时，理论上可能重发一次；本机制是至少一次 + 去重，不保证 exactly-once。
- SMTP 持续失败时会每 10 分钟无限重试，目前没有最大重试次数、死信队列或独立告警；必要时需人工检查队列与日志。
- 若生产者连 pending 文件都无法写入，通知不会凭空产生，上游任务必须如实报告「未入队」。
- 本机制仅用于降低正常工具调用偶发失败的影响，不能用来绕过平台明确的安全限制。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 操作成功；监听可由收到邮件或按 `Ctrl+C` 正常结束 |
| `1` | SMTP/IMAP 认证、连接、通信或收件人处理失败 |
| `2` | 配置不完整、参数错误、正文为空或文件不存在 |
| `3` | `watch --timeout` 到期，未收到匹配邮件 |

## 常见问题

- **认证失败**：确认使用 SMTP 授权码而非邮箱登录密码；确认 SMTP 服务已开启、授权码仍有效，且发件人地址与授权账号一致。
- **无法接收邮件**：确认邮箱已开启 IMAP 服务、IMAP 服务器和端口填写正确；QQ/163 等邮箱通常要求使用授权码。若收件账号与发件账号不同，请设置 `auth.imap_user` 和 `auth.imap_pass`。
- **连接失败或超时**：核对 SMTP 服务器、端口和加密方式，并确认当前网络允许连接该端口。
- **配置看起来没有生效**：运行 `./notify.py config show` 查看每项配置的来源；环境变量会覆盖配置文件。
- **需要换邮箱服务商**：调整 `smtp.server`、`smtp.port` 和 `smtp.ssl`，并按该服务商要求填写授权信息。
- **测试命令成功但收件箱暂时没有邮件**：成功表示 SMTP 已接受发送请求；再检查收件箱的垃圾邮件、归档规则和投递延迟。

## 安全提示

- 不要将真实配置文件、邮箱密码或授权码提交到 Git。
- 默认配置文件放在用户目录并设为 `600`；示例配置只使用占位值。
- 分享运行日志前检查其中是否包含邮箱地址、主机名或其他不希望公开的信息。
