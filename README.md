# notify

`notify` 是一个基于 Python 标准库的邮件通知命令行工具，适合在脚本、定时任务和训练任务结束时发送邮件。支持纯文本、HTML、附件、管道输入、多收件人和抄送，不需要安装第三方 Python 包。

## 环境要求

- Python 3.10 或更高版本
- 一个已开通 SMTP 服务的邮箱账号
- 邮箱服务要求的 SMTP 密码或授权码。QQ、163 等邮箱通常要求使用授权码，而不是网页登录密码

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

[mail]
receiver_email = recipient@example.com
```

将示例邮箱替换为自己的地址，并将 `sender_pass` 替换为邮箱服务商生成的 SMTP 授权码。多个默认收件人用英文逗号分隔。

### SMTP 加密方式

`ssl = auto` 会按端口选择加密方式：465 使用 SSL，25 或 587 使用 STARTTLS；其他端口默认使用 SSL。也可以明确指定 `ssl`、`starttls` 或 `none`。

QQ 邮箱通常使用 `smtp.qq.com:465`（SSL）或 `smtp.qq.com:587`（STARTTLS）。请以邮箱服务商提供的服务器地址、端口和加密要求为准。

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

例如，在 CI 或临时任务中可以通过环境变量提供配置，而不创建配置文件。请使用 CI 的密钥管理功能保存授权码，不要把真实授权码写入仓库或脚本。

## 命令用法

### 查看帮助

```bash
./notify.py --help
./notify.py send --help
./notify.py test --help
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

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 邮件已提交发送 |
| `1` | SMTP 认证、连接、通信或收件人处理失败 |
| `2` | 配置不完整、参数错误、正文为空或文件不存在 |

## 常见问题

- **认证失败**：确认使用 SMTP 授权码而非邮箱登录密码；确认 SMTP 服务已开启、授权码仍有效，且发件人地址与授权账号一致。
- **连接失败或超时**：核对 SMTP 服务器、端口和加密方式，并确认当前网络允许连接该端口。
- **配置看起来没有生效**：运行 `./notify.py config show` 查看每项配置的来源；环境变量会覆盖配置文件。
- **需要换邮箱服务商**：调整 `smtp.server`、`smtp.port` 和 `smtp.ssl`，并按该服务商要求填写授权信息。
- **测试命令成功但收件箱暂时没有邮件**：成功表示 SMTP 已接受发送请求；再检查收件箱的垃圾邮件、归档规则和投递延迟。

## 安全提示

- 不要将真实配置文件、邮箱密码或授权码提交到 Git。
- 默认配置文件放在用户目录并设为 `600`；示例配置只使用占位值。
- 分享运行日志前检查其中是否包含邮箱地址、主机名或其他不希望公开的信息。
