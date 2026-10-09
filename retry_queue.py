#!/usr/bin/env python3
"""Retry queued notification emails from Berry's persistent queue.

Each subdirectory of pending/ is one queue. A message file starts with a
"Subject: ..." line, optionally followed by an "X-Notify-Format: html"
header line and a blank line, then the body (HTML or plain text). The
producer only writes files; this worker never interprets queued text as
shell commands.
"""

import argparse
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

import notify


ROOT = Path(__file__).resolve().parent
PENDING = ROOT / "pending"
LOCK_PATH = PENDING / ".retry.lock"

QUEUE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
GENERIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# 与 notify.py enqueue 写入的标识完全一致；仅允许出现在 Subject 行后的第二行。
FORMAT_HEADER = "X-Notify-Format: html"
HTML_FALLBACK_TEXT = "（本邮件包含 HTML 内容，请使用支持 HTML 的邮件客户端查看）"

MAX_PER_QUEUE = 10
SEND_TIMEOUT = 60
MIN_AGE_SECONDS = 30


def make_logger():
    logger = logging.getLogger("email-retry-queue")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # 幂等安装：进程内多次调用（或测试中 logger 被临时接管）不重复挂载文件 handler。
    if not any(isinstance(h, RotatingFileHandler) and getattr(h, "_queue_owner", False)
               for h in logger.handlers):
        handler = RotatingFileHandler(
            ROOT / "retry_queue.log", maxBytes=512_000, backupCount=3, encoding="utf-8"
        )
        handler._queue_owner = True
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def discover_queues() -> list[Path]:
    if not PENDING.is_dir():
        return []
    return [p for p in sorted(PENDING.iterdir())
            if p.is_dir() and QUEUE_NAME.fullmatch(p.name)]


def parse_message(path: Path) -> tuple[str | None, str, str, str]:
    """解析队列消息文件，返回 (subject, body, format, error)。

    format 为 "text" / "html"；error 非空表示格式异常，消息必须保留。
    解析规则严格：格式标识只认 Subject 行后第二行的精确匹配，
    正文其他位置出现相似文本不会被误判为 HTML。
    """
    text = path.read_text(encoding="utf-8")
    first_line, _, rest = text.partition("\n")
    if not first_line.startswith("Subject:"):
        return None, text, "error", "缺少 Subject 首行"
    subject = first_line[len("Subject:"):].strip()
    if not subject:
        return None, text, "error", "Subject 为空"

    second_line, _, after_second = rest.partition("\n")
    if second_line == FORMAT_HEADER:
        # 标识行之后必须紧跟空行，正文从第四行开始。
        if after_second != "" and not after_second.startswith("\n"):
            return subject, rest, "error", "格式标识后缺少空行"
        body = after_second[1:]
        if not body.strip():
            return subject, rest, "error", "正文为空"
        return subject, body, "html", ""
    if second_line.startswith("X-Notify-Format:"):
        return subject, rest, "error", "格式标识无效（应为 'X-Notify-Format: html'）"

    if not rest.strip():
        return subject, rest, "error", "正文为空"
    return subject, rest, "text", ""


def html_to_text(html: str) -> str:
    """从 HTML 生成纯文本回退内容；失败时退回静态提示。"""
    try:
        extractor = notify.HTMLTextExtractor()
        extractor.feed(html)
        lines = [line.strip() for line in "".join(extractor.parts).splitlines()]
        plain = "\n".join(line for line in lines if line)
    except Exception:
        plain = ""
    return plain or HTML_FALLBACK_TEXT


def send_queued(subject: str, body: str, fmt: str) -> subprocess.CompletedProcess:
    """调用 notify.py send 投递；正文经 stdin / 临时文件传递，不进入 argv。"""
    base = [sys.executable, str(ROOT / "notify.py"), "send", "-s", subject]
    if fmt == "html":
        with tempfile.TemporaryDirectory(prefix="notify-queue-") as tmpdir:
            html_path = Path(tmpdir) / "body.html"
            html_path.write_text(body, encoding="utf-8")
            return subprocess.run(
                base + ["--html-file", str(html_path)],
                input=html_to_text(body), cwd=ROOT, text=True, capture_output=True,
                timeout=SEND_TIMEOUT, check=False,
            )
    return subprocess.run(
        base, input=body, cwd=ROOT, text=True, capture_output=True,
        timeout=SEND_TIMEOUT, check=False,
    )


def describe_for_dry_run(path: Path) -> str:
    """dry-run 输出用的只读描述：显示格式或格式异常原因。"""
    try:
        _, _, fmt, error = parse_message(path)
    except (OSError, UnicodeDecodeError) as exc:
        return f"[format=error: 读取失败 {type(exc).__name__}]"
    return f"[format={fmt}]" if not error else f"[format=error: {error}]"


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Inspect only; do not send")
    args = parser.parse_args(argv)
    logger = make_logger()
    PENDING.mkdir(parents=True, exist_ok=True)

    with LOCK_PATH.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Skipped: another retry worker is running")
            return 0

        for queue_dir in discover_queues():
            sent_file = queue_dir / "sent_ids.txt"
            sent_ids = (set(sent_file.read_text(encoding="utf-8").splitlines())
                        if sent_file.exists() else set())
            queued = [
                p for p in sorted(queue_dir.glob("*.txt"))
                if p.name != sent_file.name and GENERIC_ID.fullmatch(p.stem)
            ]
            if args.dry_run:
                print(f"queue={queue_dir.name} queued={len(queued)} sent_ids={len(sent_ids)}")
                for p in queued:
                    print(f"pending: {queue_dir.name}/{p.name} {describe_for_dry_run(p)}")
                continue

            for body_file in queued[:MAX_PER_QUEUE]:
                status_id = body_file.stem
                if status_id in sent_ids:
                    body_file.unlink(missing_ok=True)
                    logger.info("Already sent, cleaned pending queue=%s ID=%s",
                                queue_dir.name, status_id)
                    continue

                # Producer may still be writing the file; let the next run handle it.
                if time.time() - body_file.stat().st_mtime < MIN_AGE_SECONDS:
                    logger.info("Deferred recently written queue=%s ID=%s",
                                queue_dir.name, status_id)
                    continue

                subject, body, fmt, error = parse_message(body_file)
                if error:
                    logger.warning("Format error retained queue=%s ID=%s: %s",
                                   queue_dir.name, status_id, error)
                    continue

                try:
                    outcome = send_queued(subject, body, fmt)
                except subprocess.TimeoutExpired:
                    # 超时无法判断 SMTP 是否已接受：结果不确定。保留消息，
                    # 下轮重试可能造成重复投递（at-least-once 语义）。
                    logger.warning("Delivery outcome unknown (timeout) queue=%s ID=%s: "
                                   "SMTP 可能已接受，消息保留，重试可能重复投递",
                                   queue_dir.name, status_id)
                    continue
                except OSError as exc:
                    logger.warning("Delivery not attempted queue=%s ID=%s: %s",
                                   queue_dir.name, status_id, type(exc).__name__)
                    continue

                # A zero exit code alone is insufficient when confirming delivery.
                if outcome.returncode != 0 or "已发送至" not in outcome.stdout:
                    logger.warning("Delivery not confirmed queue=%s ID=%s exit=%s",
                                   queue_dir.name, status_id, outcome.returncode)
                    continue

                with sent_file.open("a", encoding="utf-8") as ledger:
                    ledger.write(status_id + "\n")
                    ledger.flush()
                    os.fsync(ledger.fileno())
                sent_ids.add(status_id)
                body_file.unlink(missing_ok=True)
                logger.info("Delivery confirmed and dequeued queue=%s ID=%s",
                            queue_dir.name, status_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
