# notify AI 使用手册

本手册面向 AI Agent（定时任务、自动化脚本、编码助手），是 `notify.py` 的操作性指令，基于 [README.md](README.md) 精简而成。原理说明、配置详解、邮件模板见 README.md；两者冲突时，对 AI 以本手册为准。

## 1. 环境事实

| 主机 | 工具路径 | 配置文件 |
| --- | --- | --- |
| 开发机（Mac） | `/Users/lemon/Documents/MyProjects/02-owner/auto_notification/notify.py` | `/Users/lemon/.notify/config.ini` |
| Berry | `/home/berry/auto_notification/notify.py` | `/home/berry/.notify/config.ini` |

- Python ≥ 3.10，纯标准库，无需安装依赖。
- 两台主机配置均已存在（权限 600）。配置含授权码，禁止读取或输出其内容；验证配置只用 `./notify.py config show`（输出自动打码）。
- 配置优先级：环境变量 > 配置文件 > 默认值；`--config FILE` 指定其他配置文件。

## 2. 发送邮件

以下命令在工作目录为 auto_notification 时执行：

```bash
./notify.py test                                  # 测试邮件
./notify.py send -s "主题" -b "正文"               # 纯文本
./notify.py send -s "主题" --body-file 正文.txt    # 文件正文
cat 日志 | ./notify.py send -s "主题"              # 管道正文
./notify.py send -s "主题" --html-file 页面.html   # HTML 正文
./notify.py send -s "主题" -b "正文" -a 附件1 -a 附件2
```

收件人规则：`-t a@x.com,b@x.com` 覆盖默认收件人，可省略；省略时使用配置的 `mail.receiver_email`。`--cc c@x.com` 抄送。正文来源四选一：`-b`、`--body-file`、`--html`/`--html-file`、标准输入。

## 3. 邮件模板

HTML 邮件优先使用 `templates/` 下的预置模板（均按手机端阅读优化，兼容 iPhone 邮件客户端）：

| 场景 | 模板文件 |
| --- | --- |
| 产品通知、高层摘要、正式状态同步 | `templates/apple.html` |
| 日常通知、任务结果、简短汇报 | `templates/minimal.html` |
| 周报、质量报告、指标汇报 | `templates/business-report.html` |
| 异常、风险、监控告警、处置通知 | `templates/alert.html` |
| 系统状态、Agent 输出、技术报告、运维通知 | `templates/dark-tech.html` |

使用规则：

1. 模板内是示例占位内容。正式通知必须先复制模板、替换为实际内容，再发送或入队；禁止将示例内容原样发给用户。
2. 直发：`./notify.py send -s "主题" --html-file 改写后的文件.html`；入队：`./notify.py enqueue --queue <队列> --id <ID> -s "主题" --html-file 改写后的文件.html`。
3. 预览全部风格用 `templates/template-showcase.html`，该文件不用于发送。

## 4. 接收与监听

接收（IMAP，默认 INBOX 最新 10 封，不改变已读状态）：

```bash
./notify.py receive                              # 最新 10 封
./notify.py receive --unseen --limit 5           # 最新 5 封未读
./notify.py receive --id <UID>                   # 读指定 UID（UID 见上次输出）
./notify.py receive --attachments-dir ./files    # 同时保存附件
```

监听（等待回复场景，收到第一封匹配邮件即退出）：

```bash
./notify.py watch --once --from sender@example.com --subject "回复" --timeout 1800
```

- `--from`、`--subject` 是不区分大小写的包含匹配。
- 回复可能早于监听启动到达：加 `--include-unseen`，并配合筛选条件使用。

## 5. 退出码处理（强制）

| 退出码 | 含义 | 必须执行的动作 |
| --- | --- | --- |
| `0` | SMTP 已接受 | 报告成功；禁止对同一内容重复发送 |
| `1` | SMTP/IMAP 服务失败 | 如实报告错误输出；标准兜底是按第 6 节入队，未经用户同意不得直接重发 |
| `2` | 配置或参数错误 | 修正后可重试一次；再失败即停止并报告 |
| `3` | watch 超时 | 报告「超时，未收到匹配邮件」 |

- 退出码 `0` 只表示 SMTP 已接受，不保证已进入收件箱；向用户表述用「已提交发送」。
- 任何情况下，命令失败时禁止向用户声称已发送。

## 6. 邮件补发队列与主动入队

队列支持两种模式：

- **模式 A（直接发送失败兜底）**：`send` 明确失败（退出码 1）时，把邮件内容写入队列等 Berry 重试。
- **模式 B（主动入队，推荐给重要通知）**：不执行 SMTP，直接把邮件（纯文本或 HTML 模板）入队，由 Berry cron 统一投递。入队后禁止再直接发送同一邮件；入队成功不等于发送成功。

### 标准入队命令（优先使用）

```bash
# 纯文本
./notify.py enqueue --queue task_reports --id run-42 -s "任务完成" -b "loss=0.05"

# HTML：先按第 3 节复制模板并替换示例内容，再入队
./notify.py enqueue --queue task_reports --id run-43 -s "报告" --html-file report.html
```

规则：

1. `<queue>`：1～64 位、字母数字开头，可含 `-`、`_`；按通知类别命名（如 `server_alerts`、`task_reports`）。
2. `<id>`：1～64 位、字母数字开头，可含 `.`、`_`、`-`；同一队列内唯一。使用可读且唯一的 ID（如 `run-42`、`deploy-20261009-001`）。
3. 正文纯文本或 HTML 二选一，不得为空。
4. 同一 ID 已入队或已发送（在该队列 `sent_ids.txt` 中）时，命令拒绝并退出码 2；此时不得改换 ID 重复提交同一内容。
5. 命令成功输出「已入队（未发送）」即完成本方职责；据此向用户报告，禁止声称已发送。

### 手写队列文件（仅当 enqueue 命令不可用时）

路径：`/home/berry/auto_notification/pending/<queue>/<id>.txt`，格式二选一：

```text
Subject: 主题
纯文本正文
```

```text
Subject: 主题
X-Notify-Format: html

<!doctype html>…（完整 HTML，注意标识行后必须空行）
```

必须：写入前查重（文件已存在或 ID 在 `sent_ids.txt` 中则不写）；先写 `.tmp-` 前缀临时文件再改名；写入后读回核对。格式错误的文件会被消费者判为格式异常而滞留，不会发送。

### 发送状态与处置

| 状态 | 判定 | 处置 |
| --- | --- | --- |
| 已入队 | enqueue 输出确认 | 向用户报告「已入队，Berry 每 10 分钟自动投递」 |
| 待发送 | 消息在队列中 | 无需干预 |
| 已发送 | ID 在 `sent_ids.txt`，日志 `Delivery confirmed` | 报告成功 |
| 发送失败 | 日志 `Delivery not confirmed` / `not attempted` | 保留待下轮自动重试；持续失败报告用户 |
| 结果不确定 | 日志 `Delivery outcome unknown (timeout)` | **禁止立即重发或重复入队**（可能造成重复投递），如实报告，由用户决定 |
| 格式异常 | 日志 `Format error retained` | 报告用户人工修正文件 |

直发（`send`）超时同样属于结果不确定：不得自动重发，不得对结果不确定的邮件改换 ID 入队。

### 补发结果排查

```bash
# 所有队列当前状态（只读，不发送；显示每条消息格式/格式异常）
ssh berry "cd /home/berry/auto_notification && python3 retry_queue.py --dry-run"
# 消费者日志
ssh berry "tail -n 50 /home/berry/auto_notification/retry_queue.log"
# 某队列已发送 ID 台账
ssh berry "cat /home/berry/auto_notification/pending/<queue>/sent_ids.txt"
```

### 禁止

- 入队后禁止再直接调用 `notify.py send` 发送该通知。
- 禁止创建、修改或清理任何 `sent_ids.txt` 及 `pending/` 内已有文件（查重只读除外）。
- 消费者只在 Berry 运行；禁止在其他主机运行 `retry_queue.py` 实发。
- 禁止把邮件正文/HTML 拼进 shell 命令字符串；一律通过 `enqueue` 命令或 `send` 的参数/标准输入传递。

## 7. 硬性禁止（全局）

1. 禁止读取、显示、复制配置文件内容或任何授权码。
2. 禁止将真实配置或授权码写入 Git、日志、笔记或任何对外输出。
3. 禁止把邮件正文拼进 shell 命令字符串；正文一律经 `-b`、`--body-file` 或标准输入传递。
4. 禁止用本工具群发、滥发，或用于绕过任何平台的安全限制。
