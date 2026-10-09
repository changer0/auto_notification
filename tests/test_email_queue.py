#!/usr/bin/env python3
"""邮件补发队列系统测试：协议解析、入队、消费、MIME 构造。

全部使用 Mock，不连接 SMTP/IMAP，不读写真实配置，不触碰真实队列。
"""

import io
import os
import subprocess
import sys
import threading
import unittest
import unittest.mock
import uuid
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import notify  # noqa: E402
import retry_queue  # noqa: E402

OLD_MTIME = 1_000_000_000  # 足够旧，跳过 MIN_AGE_SECONDS 暂缓


def write_msg(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.utime(path, (OLD_MTIME, OLD_MTIME))
    return path


def fake_success_run(args, **kwargs):
    """模拟 notify.py send 成功：退出码 0 且输出包含「已发送至」。"""
    # HTML 路径的临时文件在调用后即被清理，先取出内容供断言。
    captured = {"html_file": None, "html_content": None, "args": args, "kwargs": kwargs}
    html_args = [a for a in args if str(a).endswith(".html")]
    if html_args:
        captured["html_file"] = html_args[-1]
        captured["html_content"] = Path(html_args[-1]).read_text(encoding="utf-8")
    fake_success_run.captured.append(captured)
    return unittest.mock.Mock(returncode=0, stdout="✔ 已发送至 r@x.com")


fake_success_run.captured = []


class QueueTestBase(unittest.TestCase):
    """把 retry_queue / notify 的队列根目录重定向到临时目录。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._patches = [
            unittest.mock.patch.object(retry_queue, "ROOT", self.root),
            unittest.mock.patch.object(retry_queue, "PENDING", self.root / "pending"),
            unittest.mock.patch.object(retry_queue, "LOCK_PATH", self.root / "pending" / ".retry.lock"),
            unittest.mock.patch.object(notify, "QUEUE_ROOT", self.root / "pending"),
        ]
        for p in self._patches:
            p.start()
        fake_success_run.captured.clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# 协议一致性
# ---------------------------------------------------------------------------

class TestProtocolConsistency(unittest.TestCase):
    def test_constants_match_between_producer_and_consumer(self):
        self.assertEqual(retry_queue.FORMAT_HEADER, notify.QUEUE_FORMAT_HEADER)
        self.assertEqual(retry_queue.QUEUE_NAME.pattern, notify.QUEUE_NAME_RE.pattern)
        self.assertEqual(retry_queue.GENERIC_ID.pattern, notify.QUEUE_ID_RE.pattern)


# ---------------------------------------------------------------------------
# parse_message：队列消息解析
# ---------------------------------------------------------------------------

class TestParseMessage(QueueTestBase):
    def setUp(self):
        super().setUp()
        self.dir = self.root / "pending" / "q"
        self.dir.mkdir(parents=True)

    def parse(self, name: str, content: str):
        path = write_msg(self.dir / name, content)
        return retry_queue.parse_message(path)

    def test_plain_text(self):
        subject, body, fmt, err = self.parse("a1.txt", "Subject: 普通通知\n这里是纯文本正文\n")
        self.assertEqual((subject, fmt, err), ("普通通知", "text", ""))
        self.assertEqual(body, "这里是纯文本正文\n")

    def test_legacy_style_file_still_text(self):
        _, _, fmt, err = self.parse("2107913674593644711.txt", "Subject: s\n旧协议纯正文\n")
        self.assertEqual((fmt, err), ("text", ""))

    def test_html_with_chinese_and_table(self):
        html = "<!doctype html><html><body><h1>中文标题</h1><table><tr><td>单元格</td></tr></table></body></html>"
        subject, body, fmt, err = self.parse("h1.txt", f"Subject: HTML 通知\nX-Notify-Format: html\n\n{html}\n")
        self.assertEqual((subject, fmt, err), ("HTML 通知", "html", ""))
        self.assertIn("<table><tr><td>单元格</td></tr></table>", body)

    def test_missing_subject_line(self):
        subject, _, fmt, err = self.parse("bad1.txt", "没有主题行\n正文\n")
        self.assertIsNone(subject)
        self.assertEqual(fmt, "error")
        self.assertIn("Subject", err)

    def test_empty_subject(self):
        _, _, fmt, err = self.parse("bad2.txt", "Subject:   \n正文\n")
        self.assertEqual(fmt, "error")
        self.assertIn("Subject 为空", err)

    def test_html_header_without_blank_line_is_error(self):
        _, _, fmt, err = self.parse("bad3.txt", "Subject: s\nX-Notify-Format: html\n<html>…</html>")
        self.assertEqual(fmt, "error")
        self.assertIn("缺少空行", err)

    def test_invalid_header_value_is_error(self):
        _, _, fmt, err = self.parse("bad4.txt", "Subject: s\nX-Notify-Format: plain\n正文\n")
        self.assertEqual(fmt, "error")
        self.assertIn("格式标识无效", err)

    def test_empty_text_body_is_error(self):
        _, _, fmt, err = self.parse("bad5.txt", "Subject: s\n\n\n")
        self.assertEqual(fmt, "error")
        self.assertIn("正文为空", err)

    def test_empty_html_body_is_error(self):
        _, _, fmt, err = self.parse("bad6.txt", "Subject: s\nX-Notify-Format: html\n\n")
        self.assertEqual(fmt, "error")
        self.assertIn("正文为空", err)

    def test_body_containing_header_text_not_misjudged(self):
        content = "Subject: s\n第一行正文\nX-Notify-Format: html\n第三行正文\n"
        _, _, fmt, err = self.parse("ok7.txt", content)
        self.assertEqual((fmt, err), ("text", ""))


# ---------------------------------------------------------------------------
# notify.enqueue：生产者入队
# ---------------------------------------------------------------------------

class TestEnqueue(QueueTestBase):
    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = notify.main(list(argv))
        return rc, out.getvalue()

    def expect_exit_2(self, *argv):
        with self.assertRaises(SystemExit) as ctx:
            notify.main(list(argv))
        self.assertEqual(ctx.exception.code, 2)

    def test_enqueue_text(self):
        rc, out = self.run_main("enqueue", "--queue", "task_reports", "--id", "run-42",
                                "-s", "任务完成", "-b", "loss=0.05")
        self.assertEqual(rc, 0)
        self.assertIn("已入队（未发送）", out)
        path = self.root / "pending" / "task_reports" / "run-42.txt"
        self.assertEqual(path.read_text(encoding="utf-8"), "Subject: 任务完成\nloss=0.05\n")
        subject, body, fmt, err = retry_queue.parse_message(path)
        self.assertEqual((subject, fmt, err), ("任务完成", "text", ""))

    def test_enqueue_html_from_file(self):
        html = "<h1>标题</h1><p>正文</p>"
        src = self.root / "mail.html"
        src.write_text(html, encoding="utf-8")
        rc, _ = self.run_main("enqueue", "--queue", "task_reports", "--id", "run-43",
                              "-s", "报告", "--html-file", str(src))
        self.assertEqual(rc, 0)
        path = self.root / "pending" / "task_reports" / "run-43.txt"
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "Subject: 报告\nX-Notify-Format: html\n\n<h1>标题</h1><p>正文</p>\n")
        _, body, fmt, err = retry_queue.parse_message(path)
        self.assertEqual((fmt, err, body), ("html", "", "<h1>标题</h1><p>正文</p>\n"))

    def test_duplicate_id_rejected(self):
        self.run_main("enqueue", "--queue", "q", "--id", "dup-1", "-s", "s", "-b", "b")
        self.expect_exit_2("enqueue", "--queue", "q", "--id", "dup-1", "-s", "s2", "-b", "b2")

    def test_already_sent_id_rejected(self):
        sent = self.root / "pending" / "q" / "sent_ids.txt"
        sent.parent.mkdir(parents=True)
        sent.write_text("old-9\n", encoding="utf-8")
        self.expect_exit_2("enqueue", "--queue", "q", "--id", "old-9", "-s", "s", "-b", "b")

    def test_invalid_queue_or_id_rejected(self):
        self.expect_exit_2("enqueue", "--queue", "bad queue", "--id", "x1", "-s", "s", "-b", "b")
        self.expect_exit_2("enqueue", "--queue", ".hidden", "--id", "x1", "-s", "s", "-b", "b")
        self.expect_exit_2("enqueue", "--queue", "q", "--id", "-bad", "-s", "s", "-b", "b")
        self.expect_exit_2("enqueue", "--queue", "q", "--id", "a b", "-s", "s", "-b", "b")

    def test_empty_subject_or_body_rejected(self):
        self.expect_exit_2("enqueue", "--queue", "q", "--id", "x2", "-s", "  ", "-b", "b")
        self.expect_exit_2("enqueue", "--queue", "q", "--id", "x3", "-s", "s")

    def test_text_and_html_both_rejected(self):
        self.expect_exit_2("enqueue", "--queue", "q", "--id", "x4", "-s", "s",
                           "-b", "文本", "--html", "<p>html</p>")

    def test_concurrent_same_id_only_one_wins(self):
        results = []

        def worker():
            try:
                with redirect_stdout(io.StringIO()):
                    results.append(("ok", notify.main(
                        ["enqueue", "--queue", "cq", "--id", "race-1", "-s", "s", "-b", "b"])))
            except SystemExit as e:
                results.append(("exit", e.code))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ok_count = sum(1 for kind, _ in results if kind == "ok")
        files = list((self.root / "pending" / "cq").glob("*.txt"))
        self.assertEqual(ok_count, 1)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "race-1.txt")

    def test_tmp_files_not_left_behind(self):
        self.run_main("enqueue", "--queue", "q", "--id", "clean-1", "-s", "s", "-b", "b")
        leftovers = [p for p in (self.root / "pending" / "q").iterdir()
                     if p.name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])


# ---------------------------------------------------------------------------
# retry_queue 消费者：投递、重试与状态
# ---------------------------------------------------------------------------

class TestConsumer(QueueTestBase):
    def setUp(self):
        super().setUp()
        self.queue_dir = self.root / "pending" / "q"
        self.queue_dir.mkdir(parents=True)

    def run_consumer(self, argv=None):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = retry_queue.main(list(argv) if argv is not None else [])
        return rc, out.getvalue()

    def test_successful_delivery_records_ledger_and_dequeues(self):
        write_msg(self.queue_dir / "m1.txt", "Subject: 通知一\n正文一\n")
        with unittest.mock.patch("retry_queue.subprocess.run", side_effect=fake_success_run):
            with self.assertLogs("email-retry-queue", level="INFO") as logs:
                self.run_consumer()
        self.assertIn("Delivery confirmed and dequeued queue=q ID=m1", "\n".join(logs.output))
        self.assertFalse((self.queue_dir / "m1.txt").exists())
        self.assertEqual((self.queue_dir / "sent_ids.txt").read_text(encoding="utf-8"), "m1\n")

    def test_repeat_run_does_not_reconsume(self):
        write_msg(self.queue_dir / "m2.txt", "Subject: 通知二\n正文二\n")
        with unittest.mock.patch("retry_queue.subprocess.run", side_effect=fake_success_run):
            self.run_consumer()
            fake_success_run.captured.clear()
            self.run_consumer()
        self.assertEqual(fake_success_run.captured, [])  # 第二轮无可消费消息

    def test_smtp_failure_retains_message(self):
        write_msg(self.queue_dir / "m3.txt", "Subject: 通知三\n正文三\n")
        fail = unittest.mock.Mock(returncode=1, stdout="")
        with unittest.mock.patch("retry_queue.subprocess.run", return_value=fail):
            with self.assertLogs("email-retry-queue", level="WARNING") as logs:
                self.run_consumer()
        self.assertIn("Delivery not confirmed queue=q ID=m3 exit=1", "\n".join(logs.output))
        self.assertTrue((self.queue_dir / "m3.txt").exists())
        self.assertFalse((self.queue_dir / "sent_ids.txt").exists())

    def test_smtp_timeout_marks_outcome_unknown_and_retains(self):
        write_msg(self.queue_dir / "m4.txt", "Subject: 通知四\n正文四\n")
        with unittest.mock.patch("retry_queue.subprocess.run",
                                 side_effect=subprocess.TimeoutExpired("cmd", 60)):
            with self.assertLogs("email-retry-queue", level="WARNING") as logs:
                self.run_consumer()
        joined = "\n".join(logs.output)
        self.assertIn("outcome unknown (timeout)", joined)
        self.assertIn("可能已接受", joined)
        self.assertTrue((self.queue_dir / "m4.txt").exists())

    def test_spawn_error_not_attempted(self):
        write_msg(self.queue_dir / "m5.txt", "Subject: 通知五\n正文五\n")
        with unittest.mock.patch("retry_queue.subprocess.run", side_effect=OSError("boom")):
            with self.assertLogs("email-retry-queue", level="WARNING") as logs:
                self.run_consumer()
        self.assertIn("Delivery not attempted", "\n".join(logs.output))
        self.assertTrue((self.queue_dir / "m5.txt").exists())

    def test_format_error_retained_and_logged(self):
        write_msg(self.queue_dir / "bad.txt", "没有主题\n正文\n")
        with unittest.mock.patch("retry_queue.subprocess.run", side_effect=fake_success_run):
            with self.assertLogs("email-retry-queue", level="WARNING") as logs:
                self.run_consumer()
        self.assertIn("Format error retained queue=q ID=bad", "\n".join(logs.output))
        self.assertTrue((self.queue_dir / "bad.txt").exists())
        self.assertEqual(fake_success_run.captured, [])

    def test_already_sent_id_gets_cleaned_without_sending(self):
        (self.queue_dir / "sent_ids.txt").write_text("m6\n", encoding="utf-8")
        write_msg(self.queue_dir / "m6.txt", "Subject: 旧消息\n正文\n")
        with unittest.mock.patch("retry_queue.subprocess.run", side_effect=fake_success_run):
            self.run_consumer()
        self.assertFalse((self.queue_dir / "m6.txt").exists())
        self.assertEqual(fake_success_run.captured, [])

    def test_partial_tmp_file_ignored(self):
        write_msg(self.queue_dir / ".tmp-x-1-abc.txt", "Subject: 半成品\n正文\n")
        with unittest.mock.patch("retry_queue.subprocess.run", side_effect=fake_success_run):
            _, out = self.run_consumer(["--dry-run"])
        self.assertNotIn(".tmp-x-1", out)
        self.assertEqual(fake_success_run.captured, [])

    def test_html_delivery_uses_html_file_and_text_fallback_via_stdin(self):
        html = "<h1>标题</h1><table><tr><td>数据</td></tr></table>"
        write_msg(self.queue_dir / "h7.txt", f"Subject: HTML 通知\nX-Notify-Format: html\n\n{html}\n")
        with unittest.mock.patch("retry_queue.subprocess.run", side_effect=fake_success_run):
            self.run_consumer()
        self.assertEqual(len(fake_success_run.captured), 1)
        cap = fake_success_run.captured[0]
        self.assertIn("--html-file", cap["args"])
        self.assertEqual(cap["html_content"], html + "\n")
        # HTML 原文不得出现在 argv，纯文本回退经 stdin 传递
        argv_blob = " ".join(str(a) for a in cap["args"])
        self.assertNotIn("<h1>", argv_blob)
        self.assertNotIn("<table>", cap["kwargs"]["input"])
        self.assertIn("标题", cap["kwargs"]["input"])
        self.assertIn("数据", cap["kwargs"]["input"])

    def test_dry_run_is_read_only_and_shows_formats(self):
        write_msg(self.queue_dir / "t1.txt", "Subject: 文本\n正文\n")
        write_msg(self.queue_dir / "h1.txt", "Subject: 超文\nX-Notify-Format: html\n\n<p>a</p>\n")
        write_msg(self.queue_dir / "bad.txt", "没有主题\n")
        with unittest.mock.patch("retry_queue.subprocess.run", side_effect=fake_success_run):
            _, out = self.run_consumer(["--dry-run"])
        self.assertIn("queue=q queued=3 sent_ids=0", out)
        self.assertIn("q/t1.txt [format=text]", out)
        self.assertIn("q/h1.txt [format=html]", out)
        self.assertIn("q/bad.txt [format=error:", out)
        self.assertEqual(fake_success_run.captured, [])
        self.assertTrue((self.queue_dir / "t1.txt").exists())  # 不删除、不修改

    def test_template_later_modified_does_not_change_enqueued_mail(self):
        template = (PROJECT_ROOT / "templates" / "minimal.html").read_text(encoding="utf-8")
        path = write_msg(self.queue_dir / "snap1.txt",
                         f"Subject: 快照\nX-Notify-Format: html\n\n{template}\n")
        # 模板文件被改写
        (PROJECT_ROOT / "templates" / "minimal.html").write_text(
            template + "<!-- changed -->", encoding="utf-8")
        try:
            _, body, fmt, err = retry_queue.parse_message(path)
            self.assertEqual(fmt, "html")
            self.assertEqual(body, template + "\n")
            self.assertNotIn("changed", body)
        finally:
            (PROJECT_ROOT / "templates" / "minimal.html").write_text(template, encoding="utf-8")


# ---------------------------------------------------------------------------
# MIME 构造（notify.build_message）
# ---------------------------------------------------------------------------

class TestMimeBuild(unittest.TestCase):
    def make_opts(self):
        env = {
            "NOTIFY_SENDER_EMAIL": "sender@test.example",
            "NOTIFY_SENDER_PASS": "secret",
            "NOTIFY_RECEIVER_EMAIL": "receiver@test.example",
            "NOTIFY_SMTP_SERVER": "smtp.test.example",
        }
        with unittest.mock.patch.dict(os.environ, env):
            opts, _, missing = notify.load_options(None)
        self.assertEqual(missing, [])
        return opts

    def test_text_plus_html_is_multipart_alternative(self):
        msg, recipients = notify.build_message(
            self.make_opts(), "测试主题", text="纯文本正文", html="<h1>中文标题</h1>")
        self.assertEqual(recipients, ["receiver@test.example"])
        self.assertTrue(msg.is_multipart())
        self.assertEqual(msg.get_content_type(), "multipart/alternative")
        parts = [p.get_content_type() for p in msg.walk() if not p.is_multipart()]
        self.assertEqual(parts, ["text/plain", "text/html"])
        html_part = next(p for p in msg.walk() if p.get_content_type() == "text/html")
        self.assertIn("<h1>中文标题</h1>", html_part.get_content())  # 中文无乱码

    def test_table_and_styles_preserved(self):
        html = ('<table style="border-collapse:collapse"><tr><td style="color:#f00">单元格</td>'
                '</tr></table>')
        msg, _ = notify.build_message(self.make_opts(), "s", text="t", html=html)
        html_part = next(p for p in msg.walk() if p.get_content_type() == "text/html")
        # set_content 会规范化补一个结尾换行，其余内容逐字保留
        self.assertEqual(html_part.get_content().rstrip("\n"), html)

    def test_html_only_gets_fallback_text(self):
        msg, _ = notify.build_message(self.make_opts(), "s", html="<p>仅 HTML</p>")
        text_part = next(p for p in msg.walk() if p.get_content_type() == "text/plain")
        self.assertIn("HTML", text_part.get_content())

    def test_text_only_single_part(self):
        msg, _ = notify.build_message(self.make_opts(), "s", text="普通文本")
        self.assertFalse(msg.is_multipart())
        self.assertEqual(msg.get_content_type(), "text/plain")


# ---------------------------------------------------------------------------
# PRD 第八节验收场景（Mock）：极简模板测试邮件入队
# ---------------------------------------------------------------------------

class TestAcceptanceScenario(QueueTestBase):
    def test_minimal_template_enqueue_flow(self):
        # 1-4. 基于 minimal.html 制作测试邮件
        template = (PROJECT_ROOT / "templates" / "minimal.html").read_text(encoding="utf-8")
        mail_html = template.replace("极简通知标题", "Berry 邮件队列测试")
        marker = "<p>这是一封 HTML 模板入队测试邮件。</p>"
        mail_html = mail_html.replace("</body>", marker + "</body>")
        self.assertIn("Berry 邮件队列测试", mail_html)
        src = self.root / "acceptance.html"
        src.write_text(mail_html, encoding="utf-8")

        # 5-7. 唯一 ID 入队 + 读回校验
        msg_id = f"test-{uuid.uuid4().hex[:8]}"
        out = io.StringIO()
        with redirect_stdout(out):
            rc = notify.main(["enqueue", "--queue", "task_reports", "--id", msg_id,
                              "-s", "Berry 邮件队列测试", "--html-file", str(src)])
        self.assertEqual(rc, 0)
        self.assertIn("已入队（未发送）", out.getvalue())  # 报告入队而非发送
        path = self.root / "pending" / "task_reports" / f"{msg_id}.txt"
        subject, body, fmt, err = retry_queue.parse_message(path)
        self.assertEqual((subject, fmt, err), ("Berry 邮件队列测试", "html", ""))
        self.assertIn("Berry 邮件队列测试", body)
        self.assertIn(marker, body)

        # 8-9. dry-run 确认可被发现；全程不触发 SMTP
        with unittest.mock.patch("retry_queue.subprocess.run", side_effect=fake_success_run):
            _, dry_out = None, io.StringIO()
            with redirect_stdout(dry_out):
                retry_queue.main(["--dry-run"])
        self.assertIn("queue=task_reports queued=1", dry_out.getvalue())
        self.assertIn(f"[format=html]", dry_out.getvalue())
        self.assertEqual(fake_success_run.captured, [])  # 未调用 SMTP


if __name__ == "__main__":
    unittest.main(verbosity=2)
