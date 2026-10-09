# Berry 邮件 ChatGPT 插件

基于 upload_server 的单用户 OAuth 2.1（DCR + PKCE S256）和无状态 Streamable HTTP 实现。SMTP/IMAP 配置继续由 notify.py 管理，不写入插件包。

- 公网 MCP 地址：`https://email.luxinyi.top/mcp`
- Berry 本地监听：`127.0.0.1:8771`。8770 已用于 EMAS 看板，不能占用。
- Cloudflare 现有 email 路由需将服务 URL 改成 `http://127.0.0.1:8771`。
- OAuth 用户登录复用已有文件上传服务用户名/密码。邮件 OAuth 状态和令牌独立存储在 `~/.local/state/berry-email/`，不共享文件上传授权。
- 全程代办可通过已授权的 Berry SSH 身份确认一次浏览器会话，无需提取现有密码。`approve_connection.py` 只连接用户私有 Unix socket（0600），确认精确匹配的 client_id 和 OAuth state；确认有效期 120 秒。随后在同一浏览器访问 `/oauth/ssh-consent` 并同意权限，仍校验浏览器 HttpOnly cookie、CSRF、回调地址及 PKCE，授权会话使用一次后销毁。公网 HTTP 不提供管理员确认入口。
- `email:access` 允许收信、发信和通知入队。所有工具调用都在服务端校验 OAuth；发现及初始化不返回私人信息。

## 工具

| 工具 | 行为 |
| --- | --- |
| read_email_instructions | 返回操作说明 |
| send_email | 发送纯文本、HTML 或 5 套现有模板，默认配置收件人，可指定最多 5 名主收件人和 5 名抄送 |
| receive_email | 只读最新邮件或指定 UID，可筛选未读，返回附件名称；不改变已读状态 |
| enqueue_email | 使用现有 notify.py enqueue 提交通知队列，默认收件人，Berry cron 统一投递 |

当前版本不提供邮件删除、持续监听、附件上传/下载或批量发送接口。

send_email 的 request_id 持久化在 SQLite；SMTP 调用前先记录 unknown。服务中断或发送异常后同一 ID 不会再次发送。submitted 只表示 SMTP 接受；unknown 禁止自动重发或入队。修改同一 ID 的正文会被拒绝。

## 部署

同步 email_service、templates 和 notify.py 到 `/home/berry/auto_notification/`。将 berry-email.service 放到 Berry 的 `~/.config/systemd/user/`，执行：

```sh
systemctl --user daemon-reload
systemctl --user enable --now berry-email.service
curl http://127.0.0.1:8771/health
```

服务配置通过 systemd 的现有 credentials.env 注入。禁止读取或显示该文件或 notify 配置内容。

## ChatGPT 接入

通过 ChatGPT 插件页「添加 → 创建自定义 MCP 服务器」，名称「Berry 邮件」，地址 `https://email.luxinyi.top/mcp`，认证选择 OAuth，使用动态客户端注册（DCR），无需填写静态 Client ID/Secret。完成创建、安装、OAuth 连接后，验证 read_email_instructions 和只读收信；最后发送用户授权的验收通知。

本账号已创建的私有插件：https://chatgpt.com/plugins/plugin_asdk_app_6ac86c7ae7e88191a3d93558adc7ceae 。2026-10-09 已完成公网路由切换与 OAuth 连接。实际验收聊天：https://chatgpt.com/c/6ac86d9d-b2d4-83ec-9b34-3f400cc86e53 。

可移植私有插件包在 `artifacts/berry-email-1.1.0.zip`。服务部署、路由连通和 ChatGPT 安装分别验证，创建包不代表完成安装。

测试：`python3 -m unittest discover -s tests -v`。

## 最终验收结果（2026-10-09）

44 项自动测试通过；公网初始化、工具发现和未授权拒绝通过；ChatGPT 实际调用 read_email_instructions 与 receive_email(limit=1) 成功，返回 1 封邮件。简约模板完成通知已通过 notify.py 提交发送（SMTP 已接受）。Berry 邮件服务和原 8770 看板均 active；Linger=yes，服务随用户后台管理器运行。验收记录及截图位于 artifacts/。

## 五套模板（1.1.0）

发送与通知入队均支持 `format`：

| format | 用途 |
| --- | --- |
| minimal | 默认简约通知、简短汇报 |
| apple | 苹果风产品通知、高层摘要 |
| business-report | 企业报告、周报、质量汇报 |
| alert | 异常、风险、监控告警 |
| dark-tech | 深色科技、系统状态、技术报告 |

保留 `html` 自定义 HTML 与 `text` 纯文本。模板保留原有背景、颜色、标题样式、表格布局和手机端适配，用实际主题和正文替换所有示例内容。body 是纯文本，模板模式会自动转义，支持换行；任意自定义排版使用 html。
