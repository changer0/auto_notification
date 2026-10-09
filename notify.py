#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notify —— 邮件通知 CLI 工具（纯 Python 标准库，零第三方依赖）

配置字段优先级（高 → 低）:
    1. 环境变量            NOTIFY_SMTP_SERVER / NOTIFY_SENDER_PASS / ...
    2. 配置文件            ~/.notify/config.ini（可用 --config 或 NOTIFY_CONFIG 选择）
    3. 内置默认值

快速上手:
    notify config init                 # 生成配置模板，填入邮箱与授权码
    notify config show                 # 查看当前生效配置（密码打码）
    notify test                        # 发一封测试邮件验证配置
    notify send -s "标题" -b "内容"     # 发送通知
    notify receive --limit 10           # 通过 IMAP 查看最近邮件

退出码:
    0 成功    1 邮件服务失败    2 配置/参数错误
    3 监听超时（watch）
"""

from __future__ import annotations

import argparse
import base64
import configparser
import email.message
import email.policy
import imaplib
import math
import mimetypes
import os
import smtplib
import socket
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

PROG = "notify"
VERSION = "1.1.0"

EXIT_OK = 0
EXIT_SEND_FAILED = 1
EXIT_CONFIG_ERROR = 2
EXIT_TIMEOUT = 3

DEFAULT_CONFIG_PATH = Path.home() / ".notify" / "config.ini"

# ---------------------------------------------------------------------------
# 配置定义
# ---------------------------------------------------------------------------

@dataclass
class Option:
    """一项配置的元信息：INI 位置、对应环境变量、默认值、说明。"""
    section: str
    key: str
    env: str
    default: str
    help: str
    secret: bool = False          # show 时打码
    value: str = ""
    source: str = "默认值"        # 默认值 / 配置文件 / 环境变量

    @property
    def ini_name(self) -> str:
        return f"{self.section}.{self.key}"


OPTIONS: list[Option] = [
    Option("smtp", "server",    "NOTIFY_SMTP_SERVER",    "smtp.qq.com", "SMTP 服务器地址"),
    Option("smtp", "port",      "NOTIFY_SMTP_PORT",      "465",         "SMTP 端口"),
    Option("smtp", "ssl",       "NOTIFY_SMTP_SSL",       "auto",        "加密方式: auto/ssl/starttls/none"),
    Option("smtp", "timeout",   "NOTIFY_SMTP_TIMEOUT",   "15",          "连接/读写超时（秒）"),
    Option("auth", "sender_name",   "NOTIFY_SENDER_NAME",   "",       "发件人显示名，留空用工具名"),
    Option("auth", "sender_email",  "NOTIFY_SENDER_EMAIL",  "",        "发件人邮箱账号", ),
    Option("auth", "sender_pass",   "NOTIFY_SENDER_PASS",   "",        "SMTP 密码（QQ/163 等填授权码）", secret=True),
    Option("auth", "imap_user",     "NOTIFY_IMAP_USER",     "",        "IMAP 登录账号（留空时使用发件人账号）"),
    Option("auth", "imap_pass",     "NOTIFY_IMAP_PASS",     "",        "IMAP 密码/授权码（留空时使用 SMTP 密码）", secret=True),
    Option("mail", "receiver_email", "NOTIFY_RECEIVER_EMAIL", "",      "默认收件人，多个用英文逗号分隔"),
    Option("imap", "server",    "NOTIFY_IMAP_SERVER",    "imap.qq.com", "IMAP 服务器地址"),
    Option("imap", "port",      "NOTIFY_IMAP_PORT",      "993",         "IMAP 端口"),
    Option("imap", "ssl",       "NOTIFY_IMAP_SSL",       "auto",        "加密方式: auto/ssl/starttls/none"),
    Option("imap", "timeout",   "NOTIFY_IMAP_TIMEOUT",   "15",          "连接/读写超时（秒）"),
]

CONFIG_TEMPLATE = """\
# notify 邮件通知工具配置
# 修改后无需重启，每次命令执行时重新读取。
# 所有配置均可被同名环境变量覆盖（见 `notify config show` 输出）。

[smtp]
server = smtp.qq.com
port = 465
# auto: 按端口自动判断（465→SSL，587→STARTTLS）；也可强制 ssl / starttls / none
ssl = auto
# 连接与读写超时（秒）
timeout = 15

[auth]
# 发件人显示名，留空则显示 "notify"
sender_name =
# 发件人邮箱账号
sender_email =
# ⚠ QQ 邮箱此处不是 QQ 密码，而是「设置 → 账户 → 开启 SMTP」生成的授权码
sender_pass =
# IMAP 默认使用相同账号与授权码；需要单独账号时填写下面两项
imap_user =
imap_pass =

[mail]
# 默认收件人，多个用英文逗号分隔；发送时可用 --to 临时覆盖
receiver_email =

[imap]
server = imap.qq.com
port = 993
# auto: 按端口自动判断（993→SSL，143→STARTTLS）；也可强制 ssl / starttls / none
ssl = auto
timeout = 15
"""


# ---------------------------------------------------------------------------
# 配置加载与合并
# ---------------------------------------------------------------------------

def find_config_path(cli_path: str | None) -> Path | None:
    """按 优先级 定位配置文件：--config > NOTIFY_CONFIG > ~/.notify/config.ini"""
    candidates = [
        cli_path,
        os.environ.get("NOTIFY_CONFIG"),
        DEFAULT_CONFIG_PATH,
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


def load_options(cli_path: str | None) -> tuple[list[Option], Path | None, list[str]]:
    """合并 默认值 ← 配置文件 ← 环境变量，返回 (配置列表, 配置文件路径, 缺失项提示)。"""
    opts = [Option(o.section, o.key, o.env, o.default, o.help, o.secret) for o in OPTIONS]
    for o in opts:
        o.value = o.default                      # 基底：内置默认值
    by_ini = {o.ini_name: o for o in opts}

    cfg_path = find_config_path(cli_path)

    loaded_sections: set[str] = set()
    if cfg_path:
        parser = configparser.ConfigParser()
        try:
            parser.read(cfg_path, encoding="utf-8")
        except configparser.Error as e:
            die(EXIT_CONFIG_ERROR, f"配置文件解析失败: {cfg_path}\n  {e}")
        for o in opts:
            if parser.has_option(o.section, o.key):
                val = parser.get(o.section, o.key).strip()
                if val:
                    o.value = val
                    o.source = "配置文件"
        loaded_sections = set(parser.sections())

    for o in opts:                                   # 环境变量最后覆盖
        env_val = os.environ.get(o.env)
        if env_val:
            o.value = env_val.strip()
            o.source = "环境变量"

    missing = []
    if not get(opts, "auth.sender_email"):
        missing.append("auth.sender_email（发件人账号）")
    if not get(opts, "auth.sender_pass"):
        missing.append("auth.sender_pass（密码/授权码）")
    if not get(opts, "mail.receiver_email"):
        missing.append("mail.receiver_email（默认收件人）")

    # 配置文件里有未知 section 时提醒一句（key 不逐一提醒，避免噪音）
    unknown = loaded_sections - {o.section for o in opts}
    if unknown:
        print(f"提示: 配置文件中的 section {sorted(unknown)} 不会被本工具使用", file=sys.stderr)

    return opts, cfg_path, missing


def get(opts: list[Option], ini_name: str) -> str:
    return next(o.value for o in opts if o.ini_name == ini_name)


def display_width(s: str) -> int:
    """中文等全角字符占 2 列，用于 show 输出对齐。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def pad(s: str, width: int) -> str:
    return s + " " * max(1, width - display_width(s))


def mask(value: str) -> str:
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


# ---------------------------------------------------------------------------
# 邮件构建与发送
# ---------------------------------------------------------------------------

def parse_addresses(raw: str | list[str] | None) -> list[str]:
    """解析收件人输入：支持单个字符串、逗号分隔、以及 -t 多次传入的列表。"""
    if not raw:
        return []
    parts = raw if isinstance(raw, list) else [raw]
    out = []
    for part in parts:
        for p in part.split(","):
            addr = parseaddr(p.strip())[1]
            if addr:
                out.append(addr)
    return out


def build_message(opts: list[Option], subject: str, *,
                  text: str = "", html: str = "",
                  to_addrs: list[str] | None = None,
                  cc_addrs: list[str] | None = None,
                  attachments: list[Path] | None = None) -> tuple[EmailMessage, list[str]]:
    """构建邮件。返回 (邮件对象, 实际收件人列表[to+cc])。"""
    msg = EmailMessage()
    sender_email = get(opts, "auth.sender_email")
    sender_name = get(opts, "auth.sender_name") or PROG
    msg["From"] = formataddr((sender_name, sender_email))
    msg["Subject"] = subject

    to = to_addrs or parse_addresses(get(opts, "mail.receiver_email"))
    if not to:
        die(EXIT_CONFIG_ERROR, "没有收件人：请用 --to 指定，或在配置中设置 mail.receiver_email")
    msg["To"] = ", ".join(to)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)

    # 正文：纯文本 + 可选 HTML（双 part，客户端优先显示 HTML）
    if not text and html:
        text = "（本邮件包含 HTML 内容，请使用支持 HTML 的邮件客户端查看）"
    if not text and not html:
        die(EXIT_CONFIG_ERROR, "正文为空：请用 --body / --html / --body-file 提供内容，或通过管道传入")
    if html:
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(text)

    for path in attachments or []:
        p = Path(path).expanduser()
        if not p.is_file():
            die(EXIT_CONFIG_ERROR, f"附件不存在或不是文件: {p}")
        ctype, _ = mimetypes.guess_type(p.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)

    return msg, to + (cc_addrs or [])


def smtp_login(server: smtplib.SMTP, user: str, password: str) -> None:
    """优先用单次 AUTH PLAIN 认证。

    不直接用 login()：它在服务器拒绝第一种机制后会换机制重试，
    而 QQ 等服务器此时会直接断连，把真实的 535 认证失败
    掩盖成 SMTPServerDisconnected（连接错误），误导排障方向。
    """
    if "PLAIN" in server.esmtp_features.get("auth", "").upper():
        # SMTP.auth() performs the Base64 encoding itself. Return the raw
        # NUL-delimited PLAIN payload to avoid double-encoding credentials.
        def plain_response(_challenge: bytes | None = None) -> str:
            return f"\0{user}\0{password}"

        server.auth("PLAIN", plain_response)
    else:
        server.login(user, password)


def smtp_connect(opts: list[Option]) -> smtplib.SMTP:
    host = get(opts, "smtp.server")
    port = int(get(opts, "smtp.port"))
    ssl_mode = get(opts, "smtp.ssl").lower()
    timeout = float(get(opts, "smtp.timeout"))

    if ssl_mode == "auto":
        ssl_mode = "ssl" if port == 465 else ("starttls" if port in (25, 587) else "ssl")

    try:
        if ssl_mode == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=timeout)
        elif ssl_mode == "starttls":
            server = smtplib.SMTP(host, port, timeout=timeout)
            server.starttls()
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
        server.ehlo_or_helo_if_needed()
        smtp_login(server, get(opts, "auth.sender_email"), get(opts, "auth.sender_pass"))
        return server
    except smtplib.SMTPAuthenticationError as e:
        detail = (e.smtp_error or b"").decode("utf-8", "replace").strip()
        die(EXIT_SEND_FAILED,
            f"SMTP 认证失败（{host}）。常见原因：\n"
            "  - QQ/163 等邮箱必须使用「授权码」而非登录密码\n"
            "  - SMTP 服务未开启（QQ 邮箱: 设置 → 账户 → 开启 SMTP 服务）\n"
            "  - 授权码输入有误或已被重置\n"
            "  - 发件人地址与授权账号不一致\n"
            + (f"服务器原话: {detail}" if detail else ""))
    except smtplib.SMTPConnectError as e:
        die(EXIT_SEND_FAILED, f"无法连接 {host}:{port} —— {e}\n请检查 smtp.server / smtp.port 配置与网络")
    except socket.timeout:
        die(EXIT_SEND_FAILED, f"连接 {host}:{port} 超时（{timeout:.0f}s），检查网络或调大 smtp.timeout")
    except (ConnectionRefusedError, smtplib.SMTPException, OSError) as e:
        die(EXIT_SEND_FAILED, f"SMTP 连接/通信失败（{host}:{port}）: {e}")


def send_message(opts: list[Option], msg: EmailMessage, recipients: list[str]) -> None:
    server = smtp_connect(opts)
    try:
        refused = server.send_message(msg, to_addrs=recipients)
    finally:
        server.quit()
    if refused:
        # send_message 返回被服务器拒绝的地址 {addr: (code, reason)}
        detail = "\n".join(f"  {a}: {r[0]} {r[1]}" for a, r in refused.items())
        die(EXIT_SEND_FAILED, f"以下收件人被服务器拒绝:\n{detail}")


class HTMLTextExtractor(HTMLParser):
    """将 HTML 邮件正文转成适合终端阅读的纯文本。"""

    BLOCK_TAGS = {"address", "article", "blockquote", "br", "div", "h1", "h2", "h3",
                  "h4", "h5", "h6", "hr", "li", "p", "pre", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"}:
            self.ignored_depth = max(0, self.ignored_depth - 1)
            return
        if self.ignored_depth:
            return
        if tag.lower() in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def safe_terminal_text(value: str) -> str:
    """转义邮件中的控制字符，避免邮件内容执行终端控制序列。"""
    out = []
    for char in value:
        if char in "\n\t":
            out.append(char)
        elif unicodedata.category(char) == "Cc":
            out.append(f"\\x{ord(char):02x}" if ord(char) <= 0xff else f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return "".join(out)


def part_text(part: email.message.Message) -> str:
    """读取 MIME 文本部分，并兼容未知字符集。"""
    try:
        content = part.get_content()
    except (LookupError, UnicodeError, KeyError):
        content = part.get_payload(decode=True)
        if content is None:
            return ""
        content = content.decode(part.get_content_charset() or "utf-8", errors="replace")
    if isinstance(content, bytes):
        content = content.decode(part.get_content_charset() or "utf-8", errors="replace")
    return content if isinstance(content, str) else str(content)


def message_body(message: email.message.Message) -> str:
    """优先取 text/plain；仅有 HTML 时去除标签后返回文本。"""
    plain = ""
    html = ""
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment" or part.get_filename():
            continue
        content_type = part.get_content_type().lower()
        if content_type == "text/plain" and not plain:
            plain = part_text(part)
        elif content_type == "text/html" and not html:
            html = part_text(part)
    if plain:
        return plain.strip()
    if html:
        extractor = HTMLTextExtractor()
        try:
            extractor.feed(html)
        except Exception:
            # 不完整 HTML 仍尽量保留已解析到的文本。
            pass
        return "\n".join(line.strip() for line in "".join(extractor.parts).splitlines() if line.strip())
    return "（没有可显示的文本正文）"


def save_attachments(message: email.message.Message, directory: Path, uid: str) -> list[Path]:
    """保存邮件附件，使用 UID 前缀并剥除目录路径，避免覆盖和路径穿越。"""
    saved: list[Path] = []
    index = 0
    for part in message.walk():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        index += 1
        filename = filename.replace("\\", "/").split("/")[-1].strip()
        filename = "".join(ch for ch in filename if unicodedata.category(ch) != "Cc")
        if filename in ("", ".", ".."):
            filename = "attachment"
        candidate = directory / f"{uid}-{index}-{filename}"
        suffix = 2
        while candidate.exists():
            candidate = directory / f"{uid}-{index}-{suffix}-{filename}"
            suffix += 1
        candidate.write_bytes(payload)
        saved.append(candidate)
    return saved


def attachment_names(message: email.message.Message) -> list[str]:
    return [str(part.get_filename()) for part in message.walk() if part.get_filename()]


def display_received_message(message: email.message.Message, uid: str,
                             attachment_dir: Path | None = None) -> None:
    names = attachment_names(message)
    saved = save_attachments(message, attachment_dir, uid) if attachment_dir else []
    sender = safe_terminal_text(str(message.get("From", "（未知）")))
    recipient = safe_terminal_text(str(message.get("To", "（未知）")))
    subject = safe_terminal_text(str(message.get("Subject", "（无主题）")))
    date = safe_terminal_text(format_message_date(str(message.get("Date", ""))))
    body = safe_terminal_text(message_body(message))

    print(f"UID: {uid}\n发件人: {sender}\n收件人: {recipient}\n时间: {date}\n主题: {subject}")
    if saved:
        print("附件已保存: " + ", ".join(safe_terminal_text(str(path)) for path in saved))
    elif names:
        print("附件（未下载）: " + ", ".join(safe_terminal_text(name) for name in names))
    print("--- 正文 ---")
    print(body)
    print("\n" + "=" * 72)


def imap_search_uids(server: imaplib.IMAP4, criteria: str) -> list[str]:
    status, data = server.uid("search", None, criteria)
    if status != "OK":
        raise imaplib.IMAP4.error(f"IMAP 搜索失败: {criteria}")
    raw = data[0] if data and data[0] else b""
    return raw.decode("ascii", "ignore").split()


def imap_fetch_message(server: imaplib.IMAP4, uid: str, *, peek: bool = False
                       ) -> email.message.Message | None:
    query = "(BODY.PEEK[])" if peek else "(RFC822)"
    status, data = server.uid("fetch", uid, query)
    if status != "OK":
        raise imaplib.IMAP4.error(f"UID {uid} 获取失败")
    raw = next((item[1] for item in data or []
                if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes)), None)
    if raw is None:
        return None
    return email.message_from_bytes(raw, policy=email.policy.default)


def watch_message_matches(message: email.message.Message, args: argparse.Namespace) -> bool:
    sender = str(message.get("From", "")).casefold()
    subject = str(message.get("Subject", "")).casefold()
    return (not args.from_contains or args.from_contains.casefold() in sender) and (
        not args.subject_contains or args.subject_contains.casefold() in subject
    )


def imap_connect(opts: list[Option], *, die_on_error: bool = True) -> imaplib.IMAP4:
    host = get(opts, "imap.server")
    ssl_mode = get(opts, "imap.ssl").lower()
    try:
        port = int(get(opts, "imap.port"))
        timeout = float(get(opts, "imap.timeout"))
    except ValueError as e:
        die(EXIT_CONFIG_ERROR, f"IMAP 端口或超时配置无效: {e}")
    if not 1 <= port <= 65535 or timeout <= 0:
        die(EXIT_CONFIG_ERROR, "IMAP 端口必须在 1-65535 之间，超时必须大于 0")

    if ssl_mode == "auto":
        ssl_mode = "ssl" if port == 993 else ("starttls" if port == 143 else "ssl")
    if ssl_mode not in {"ssl", "starttls", "none"}:
        die(EXIT_CONFIG_ERROR, "imap.ssl 仅支持 auto、ssl、starttls 或 none")

    username = get(opts, "auth.imap_user") or get(opts, "auth.sender_email")
    password = get(opts, "auth.imap_pass") or get(opts, "auth.sender_pass")
    if not username or not password:
        die(EXIT_CONFIG_ERROR,
            "IMAP 账号或授权码未设置：填写 auth.imap_user/auth.imap_pass，"
            "或使用已设置的 auth.sender_email/auth.sender_pass")

    server: imaplib.IMAP4 | None = None
    try:
        if ssl_mode == "ssl":
            server = imaplib.IMAP4_SSL(host, port, timeout=timeout)
        else:
            server = imaplib.IMAP4(host, port, timeout=timeout)
            if ssl_mode == "starttls":
                server.starttls()
    except (socket.timeout, TimeoutError):
        if die_on_error:
            die(EXIT_SEND_FAILED, f"连接 IMAP 服务器 {host}:{port} 超时（{timeout:g}s）")
        raise
    except (imaplib.IMAP4.error, OSError) as e:
        if die_on_error:
            die(EXIT_SEND_FAILED, f"IMAP 连接失败（{host}:{port}）: {e}")
        raise

    try:
        server.login(username, password)
    except imaplib.IMAP4.error as e:
        try:
            server.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
        if die_on_error:
            die(EXIT_SEND_FAILED,
                f"IMAP 认证失败（{host}）：检查 IMAP 服务是否已开启，以及账号和授权码是否正确。\n{e}")
        raise
    except (socket.timeout, TimeoutError, OSError) as e:
        try:
            server.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
        if die_on_error:
            die(EXIT_SEND_FAILED, f"IMAP 登录通信失败（{host}:{port}）: {e}")
        raise
    return server


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("请输入正整数") from e
    if parsed < 1:
        raise argparse.ArgumentTypeError("请输入正整数")
    return parsed


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("请输入大于 0 的数字") from e
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("请输入大于 0 的数字")
    return parsed


def nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("请输入大于或等于 0 的数字") from e
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("请输入大于或等于 0 的数字")
    return parsed


def format_message_date(value: str | None) -> str:
    if not value:
        return "（未知）"
    try:
        return parsedate_to_datetime(value).isoformat(sep=" ", timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return value


def encode_imap_mailbox(name: str) -> str:
    """按 IMAP modified UTF-7 编码文件夹名中的非 ASCII 字符。"""
    out: list[str] = []
    encoded_run: list[str] = []

    def flush() -> None:
        if encoded_run:
            raw = "".join(encoded_run).encode("utf-16-be")
            token = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
            out.append(f"&{token}-")
            encoded_run.clear()

    for char in name:
        if 0x20 <= ord(char) <= 0x7e and char != "&":
            flush()
            out.append(char)
        elif char == "&":
            flush()
            out.append("&-")
        else:
            encoded_run.append(char)
    flush()
    return "".join(out)


def cmd_receive(args: argparse.Namespace) -> int:
    opts, _, _ = load_options(getattr(args, "config", None))
    if args.id and args.unseen:
        die(EXIT_CONFIG_ERROR, "--id 和 --unseen 不能同时使用")

    attachment_dir = Path(args.attachments_dir).expanduser() if args.attachments_dir else None
    if attachment_dir:
        try:
            attachment_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            die(EXIT_CONFIG_ERROR, f"无法创建附件目录 {attachment_dir}: {e}")

    server = imap_connect(opts)
    count = 0
    try:
        status, data = server.select(encode_imap_mailbox(args.mailbox), readonly=not args.mark_seen)
        if status != "OK":
            detail = data[0].decode("utf-8", "replace") if data and data[0] else ""
            die(EXIT_SEND_FAILED, f"无法打开邮箱文件夹 {args.mailbox}: {detail}")

        if args.id:
            uids = [str(args.id)]
        else:
            criteria = "UNSEEN" if args.unseen else "ALL"
            uids = imap_search_uids(server, criteria)
            uids = list(reversed(uids))[:args.limit]

        for uid in uids:
            message = imap_fetch_message(server, uid)
            if message is None:
                print(f"提示: UID {uid} 没有邮件内容，已跳过。", file=sys.stderr)
                continue
            try:
                display_received_message(message, uid, attachment_dir)
            except OSError as e:
                die(EXIT_CONFIG_ERROR, f"保存 UID {uid} 的附件失败: {e}")
            count += 1
    except (imaplib.IMAP4.error, OSError, socket.timeout) as e:
        die(EXIT_SEND_FAILED, f"接收邮件时 IMAP 通信失败: {e}")
    finally:
        try:
            server.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    if count == 0:
        print("没有找到匹配的邮件。")
    else:
        print(f"已读取 {count} 封邮件。")
    return EXIT_OK


def cmd_watch(args: argparse.Namespace) -> int:
    opts, _, _ = load_options(getattr(args, "config", None))
    attachment_dir = Path(args.attachments_dir).expanduser() if args.attachments_dir else None
    if attachment_dir:
        try:
            attachment_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            die(EXIT_CONFIG_ERROR, f"无法创建附件目录 {attachment_dir}: {e}")

    started = time.monotonic()
    deadline = started + args.timeout if args.timeout else None
    server: imaplib.IMAP4 | None = None

    def select_mailbox(connection: imaplib.IMAP4) -> None:
        status, data = connection.select(
            encode_imap_mailbox(args.mailbox), readonly=not args.mark_seen)
        if status != "OK":
            detail = data[0].decode("utf-8", "replace") if data and data[0] else ""
            raise imaplib.IMAP4.error(f"无法打开邮箱文件夹 {args.mailbox}: {detail}")

    def emit_if_matching(message: email.message.Message, uid: str,
                         connection: imaplib.IMAP4) -> bool:
        if not watch_message_matches(message, args):
            return False
        if args.mark_seen:
            status, _ = connection.uid("store", uid, "+FLAGS.SILENT", r"(\Seen)")
            if status != "OK":
                raise imaplib.IMAP4.error(f"无法将 UID {uid} 标为已读")
        try:
            display_received_message(message, uid, attachment_dir)
        except OSError as e:
            die(EXIT_CONFIG_ERROR, f"保存 UID {uid} 的附件失败: {e}")
        return True

    try:
        server = imap_connect(opts)
        select_mailbox(server)
        current_uids = imap_search_uids(server, "ALL")
        cursor = max((int(uid) for uid in current_uids), default=0)
        print(f"开始监听 {args.mailbox}，每 {args.interval:g} 秒检查一次。按 Ctrl+C 停止。")

        if args.include_unseen:
            initial_unseen = imap_search_uids(server, "UNSEEN")
            cursor = max(cursor, max((int(uid) for uid in initial_unseen), default=0))
            for uid in initial_unseen:
                message = imap_fetch_message(server, uid, peek=True)
                if message and emit_if_matching(message, uid, server) and args.once:
                    return EXIT_OK

        while True:
            if deadline is not None and time.monotonic() >= deadline:
                print("监听超时，未收到匹配的邮件。", file=sys.stderr)
                return EXIT_TIMEOUT

            try:
                if server is None:
                    server = imap_connect(opts, die_on_error=False)
                    select_mailbox(server)
                next_uids = imap_search_uids(server, f"UID {cursor + 1}:*")
                for uid in sorted(next_uids, key=int):
                    uid_num = int(uid)
                    if uid_num <= cursor:
                        continue
                    message = imap_fetch_message(server, uid, peek=True)
                    if message is not None and emit_if_matching(message, uid, server) and args.once:
                        return EXIT_OK
                    cursor = uid_num
            except (imaplib.IMAP4.error, OSError, socket.timeout) as e:
                print(f"IMAP 连接中断，将在 {args.interval:g} 秒后重连: {e}", file=sys.stderr)
                if server is not None:
                    try:
                        server.logout()
                    except (imaplib.IMAP4.error, OSError):
                        pass
                    server = None

            remaining = deadline - time.monotonic() if deadline is not None else args.interval
            if remaining <= 0:
                print("监听超时，未收到匹配的邮件。", file=sys.stderr)
                return EXIT_TIMEOUT
            time.sleep(min(args.interval, remaining))
    except KeyboardInterrupt:
        print("监听已停止。")
        return EXIT_OK
    except (imaplib.IMAP4.error, OSError, socket.timeout) as e:
        die(EXIT_SEND_FAILED, f"无法启动收信监听: {e}")
    finally:
        if server is not None:
            try:
                server.logout()
            except (imaplib.IMAP4.error, OSError):
                pass


def read_stdin_if_piped() -> str:
    """stdin 是管道/重定向时读取其内容作为正文，否则返回空串。"""
    if sys.stdin is not None and not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_send(args: argparse.Namespace) -> int:
    opts, _, missing = load_options(getattr(args, "config", None))
    if missing:
        die(EXIT_CONFIG_ERROR, missing_message(missing))

    text = args.body or ""
    if args.body_file:
        p = Path(args.body_file).expanduser()
        if not p.is_file():
            die(EXIT_CONFIG_ERROR, f"文件不存在: {p}")
        text = (text + "\n" if text else "") + p.read_text(encoding="utf-8")
    if not text:
        text = read_stdin_if_piped()

    html = args.html or ""
    if args.html_file:
        p = Path(args.html_file).expanduser()
        if not p.is_file():
            die(EXIT_CONFIG_ERROR, f"文件不存在: {p}")
        html = p.read_text(encoding="utf-8")

    to = parse_addresses(args.to) if args.to else None
    cc = parse_addresses(args.cc) if args.cc else None
    msg, recipients = build_message(
        opts, args.subject, text=text.strip("\n"), html=html,
        to_addrs=to, cc_addrs=cc, attachments=args.attach,
    )
    send_message(opts, msg, recipients)
    print(f"✔ 已发送至 {', '.join(recipients)}")
    return EXIT_OK


def cmd_test(args: argparse.Namespace) -> int:
    opts, cfg_path, missing = load_options(getattr(args, "config", None))
    if missing:
        die(EXIT_CONFIG_ERROR, missing_message(missing))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    host = socket.gethostname()
    text = (
        "这是一封来自 notify CLI 的测试邮件。\n\n"
        f"发送时间: {now}\n"
        f"主机名:   {host}\n"
        f"SMTP:     {get(opts, 'smtp.server')}:{get(opts, 'smtp.port')}\n"
        f"配置来源: {cfg_path or '环境变量/默认值'}\n\n"
        "收到本邮件说明你的配置完全可用。"
    )
    html = (
        "<html><body style=\"font-family: -apple-system, sans-serif; line-height: 1.6\">"
        "<h3>✔ notify 测试邮件</h3>"
        f"<p>发送时间: {now}<br>主机名: {host}<br>"
        f"SMTP: {get(opts, 'smtp.server')}:{get(opts, 'smtp.port')}</p>"
        "<p style=\"color: #666\">收到本邮件说明你的配置完全可用。</p>"
        "</body></html>"
    )
    to = parse_addresses(args.to) if args.to else None
    msg, recipients = build_message(opts, "[notify] 测试邮件", text=text, html=html, to_addrs=to)
    send_message(opts, msg, recipients)
    print(f"✔ 测试邮件已发送至 {', '.join(recipients)}")
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    config_arg = getattr(args, "config", None)
    if args.config_cmd == "init":
        target = Path(config_arg).expanduser() if config_arg else DEFAULT_CONFIG_PATH
        if target.exists() and not args.force:
            die(EXIT_CONFIG_ERROR, f"{target} 已存在，如需覆盖请加 --force")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        os.chmod(target, 0o600)                       # 含密码，限制为仅所有者可读写
        print(f"✔ 已生成配置模板: {target}\n  请填写 sender_email、sender_pass 和 receiver_email 后运行: notify test")
        return EXIT_OK

    # config show
    opts, cfg_path, _ = load_options(config_arg)
    src = str(cfg_path) if cfg_path else "（未找到，使用环境变量/默认值）"
    print(f"配置文件: {src}\n")
    cur_section = None
    for o in opts:
        if o.section != cur_section:
            print(f"[{o.section}]")
            cur_section = o.section
        val = mask(o.value) if (o.secret and o.value) else o.value
        if not val:
            val = "（未设置）"
        key_w = max(display_width(o.key) for o in opts) + 2
        val_w = 32
        print(f"  {pad(o.key, key_w)} = {pad(val, val_w)} # {o.source} | {o.help}")
    print(f"\n优先级: 环境变量 > 配置文件 > 默认值；环境变量名见 `notify --help`")
    return EXIT_OK


def missing_message(missing: list[str]) -> str:
    return (
        "配置不完整，缺少:\n  - " + "\n  - ".join(missing) +
        "\n\n解决方式（任选其一）:\n"
        "  1. notify config init  然后编辑 ~/.notify/config.ini\n"
        "  2. 设置环境变量，例如 export NOTIFY_SENDER_PASS=xxxx\n"
        "  3. 本次命令直接用 --config 指定其他配置文件"
    )


def die(code: int, message: str) -> None:
    print(f"✘ {message}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # default 用 SUPPRESS：顶层与子命令同时定义 --config 时，
    # 未出现的那一层不会把 None 写回 namespace，两层可任选其一使用。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="FILE", default=argparse.SUPPRESS,
                        help="指定配置文件路径（默认 ~/.notify/config.ini 或 $NOTIFY_CONFIG）")

    parser = argparse.ArgumentParser(
        prog=PROG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="notify —— 邮件通知 CLI 工具。基于 Python 标准库，零第三方依赖。\n"
                    "配置字段优先级: 环境变量 > 配置文件 > 默认值；--config 用于选择配置文件。",
        epilog=(
            "环境变量:\n"
            "  NOTIFY_CONFIG          配置文件路径\n"
            "  NOTIFY_SMTP_SERVER     SMTP 服务器\n"
            "  NOTIFY_SMTP_PORT       SMTP 端口\n"
            "  NOTIFY_SMTP_SSL        加密方式 auto/ssl/starttls/none\n"
            "  NOTIFY_SMTP_TIMEOUT    超时秒数\n"
            "  NOTIFY_SENDER_NAME     发件人显示名\n"
            "  NOTIFY_SENDER_EMAIL    发件人账号\n"
            "  NOTIFY_SENDER_PASS     密码/授权码\n"
            "  NOTIFY_IMAP_SERVER     IMAP 服务器\n"
            "  NOTIFY_IMAP_PORT       IMAP 端口\n"
            "  NOTIFY_IMAP_SSL        加密方式 auto/ssl/starttls/none\n"
            "  NOTIFY_IMAP_TIMEOUT    IMAP 超时秒数\n"
            "  NOTIFY_IMAP_USER       IMAP 登录账号（默认使用发件人账号）\n"
            "  NOTIFY_IMAP_PASS       IMAP 密码/授权码（默认使用 SMTP 密码）\n"
            "  NOTIFY_RECEIVER_EMAIL  默认收件人（逗号分隔）\n"
            "\n"
            "示例:\n"
            "  notify config init                          # 生成配置模板\n"
            "  notify config show                          # 查看生效配置\n"
            "  notify test                                 # 发测试邮件\n"
            '  notify send -s "任务完成" -b "loss=0.05"      # 纯文本\n'
            '  notify send -s "报告" --body-file report.txt  # 正文读文件\n'
            '  cat log.txt | notify send -s "日志"          # 正文读管道\n'
            '  notify send -s "页面" --html-file page.html   # HTML 正文\n'
            '  notify send -s "附件" -b "见附件" -a a.pdf -a b.zip\n'
            '  notify send -s "多人" -b "hi" -t a@x.com,b@y.com --cc c@z.com\n'
            '  notify receive --limit 5                   # 收取最近 5 封邮件\n'
            '  notify receive --unseen --mark-seen         # 读取未读邮件并标为已读\n'
            '  notify watch --once --timeout 1800          # 等待新邮件，最长 30 分钟\n'
            "  notify --config /tmp/other.ini send -s hi -b hi   # 临时换配置"
        ),
    )
    parser.add_argument("-V", "--version", action="version", version=f"{PROG} {VERSION}")
    parser.add_argument("--config", metavar="FILE", default=argparse.SUPPRESS,
                        help="指定配置文件路径（默认 ~/.notify/config.ini 或 $NOTIFY_CONFIG）")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    # ---- send ----
    p_send = sub.add_parser(
        "send", parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter,
        help="发送邮件",
        description="发送一封邮件。正文支持纯文本 / HTML / 管道输入，可加附件。",
        epilog=(
            "示例:\n"
            '  notify send -s "训练完成" -b "loss=0.05, 用时 2h"\n'
            '  notify send -s "日报" --body-file daily.txt\n'
            '  grep ERROR app.log | notify send -s "报错日志"\n'
            '  notify send -s "结果" --html "<h1>OK</h1><p>acc=0.98</p>"\n'
            '  notify send -s "结果" --html-file result.html\n'
            '  notify send -s "打包" -b "见附件" -a result.csv -a model.bin\n'
            "收件人默认取配置 mail.receiver_email；-t 可多次使用，也支持逗号分隔。"
        ),
    )
    p_send.add_argument("-s", "--subject", default="[notify] 通知", help="邮件主题（默认: [notify] 通知）")
    p_send.add_argument("-b", "--body", help="纯文本正文（也可通过管道/文件传入）")
    p_send.add_argument("--body-file", metavar="FILE", help="从文件读取纯文本正文")
    p_send.add_argument("--html", help="HTML 正文（直接给内容）")
    p_send.add_argument("--html-file", metavar="FILE", help="从文件读取 HTML 正文")
    p_send.add_argument("-t", "--to", action="append", metavar="ADDR",
                        help="收件人，可多次使用或逗号分隔（默认取配置）")
    p_send.add_argument("--cc", action="append", metavar="ADDR", help="抄送，可多次使用或逗号分隔")
    p_send.add_argument("-a", "--attach", action="append", metavar="FILE",
                        help="附件路径，可多次使用")
    p_send.set_defaults(func=cmd_send)

    # ---- test ----
    p_test = sub.add_parser(
        "test", parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter,
        help="发送测试邮件，验证配置是否可用",
        description="用当前配置发一封固定内容的测试邮件（含纯文本+HTML 双格式），"
                    "用来验证服务器、账号、授权码和收件人是否正确。",
        epilog="示例:\n  notify test\n  notify test -t someone@example.com",
    )
    p_test.add_argument("-t", "--to", action="append", metavar="ADDR", help="临时收件人（默认取配置）")
    p_test.set_defaults(func=cmd_test)

    # ---- receive ----
    p_receive = sub.add_parser(
        "receive", parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter,
        help="通过 IMAP 接收并查看邮件",
        description="通过 IMAP 读取邮件正文。默认读取收件箱最新 10 封，不会标为已读。",
        epilog=(
            "示例:\n"
            "  notify receive                              # 最近 10 封\n"
            "  notify receive --limit 5 --unseen           # 最近 5 封未读\n"
            "  notify receive --id 123456                  # 查看指定 IMAP UID\n"
            "  notify receive --mark-seen                  # 读取时标为已读\n"
            "  notify receive --attachments-dir ./files    # 同时保存附件"
        ),
    )
    p_receive.add_argument("-n", "--limit", type=positive_int, default=10,
                           help="最多读取邮件数（默认: 10；指定 --id 时忽略）")
    p_receive.add_argument("--unseen", action="store_true", help="只读取未读邮件")
    p_receive.add_argument("--id", type=positive_int, metavar="UID", help="读取指定邮件的 IMAP UID")
    p_receive.add_argument("--mailbox", default="INBOX", metavar="NAME", help="邮箱文件夹（默认: INBOX）")
    p_receive.add_argument("--mark-seen", action="store_true", help="读取后将邮件标为已读")
    p_receive.add_argument("--attachments-dir", metavar="DIR", help="保存邮件附件到指定目录")
    p_receive.set_defaults(func=cmd_receive)

    # ---- watch ----
    p_watch = sub.add_parser(
        "watch", parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter,
        help="持续监听 IMAP 新邮件",
        description="持续轮询 IMAP 收件箱，发现新邮件后输出内容。适合等待邮件回复。",
        epilog=(
            "示例:\n"
            "  notify watch --once --timeout 1800\n"
            "  notify watch --once --from sender@example.com --subject 回复 --timeout 3600\n"
            "  notify watch --include-unseen --once --from sender@example.com\n"
            "  notify watch --interval 3 --mark-seen"
        ),
    )
    p_watch.add_argument("--mailbox", default="INBOX", metavar="NAME", help="邮箱文件夹（默认: INBOX）")
    p_watch.add_argument("--interval", type=positive_float, default=10,
                         help="轮询间隔秒数（默认: 10）")
    p_watch.add_argument("--timeout", type=nonnegative_float, default=0,
                         help="最长等待秒数；0 表示一直监听（默认: 0）")
    p_watch.add_argument("--once", action="store_true", help="收到第一封符合筛选条件的邮件后退出")
    p_watch.add_argument("--include-unseen", action="store_true",
                         help="启动时也检查当前未读邮件，以免错过已到达的回复")
    p_watch.add_argument("--from", dest="from_contains", metavar="TEXT", help="只匹配发件人中包含的文字")
    p_watch.add_argument("--subject", dest="subject_contains", metavar="TEXT", help="只匹配主题中包含的文字")
    p_watch.add_argument("--mark-seen", action="store_true", help="匹配到邮件后将其标为已读")
    p_watch.add_argument("--attachments-dir", metavar="DIR", help="将匹配邮件的附件保存到指定目录")
    p_watch.set_defaults(func=cmd_watch)

    # ---- config ----
    p_cfg = sub.add_parser(
        "config", parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter,
        help="初始化 / 查看配置",
        description="管理配置文件。--config 同时决定 init 的写入位置与 show 的读取位置。",
        epilog="示例:\n  notify config init\n  notify config init --config /tmp/notify.ini --force\n  notify config show",
    )
    cfg_sub = p_cfg.add_subparsers(dest="config_cmd", metavar="<action>", required=True)
    p_init = cfg_sub.add_parser(
        "init", parents=[common],
        help="生成配置模板（默认 ~/.notify/config.ini，权限 600）")
    p_init.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    p_show = cfg_sub.add_parser(
        "show", parents=[common],
        help="显示当前生效的配置及各值的来源（密码打码）")
    p_cfg.set_defaults(func=cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
