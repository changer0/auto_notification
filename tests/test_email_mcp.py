import base64
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "email_service"))
import server
from oauth_server import OAuthServer, MCP_RESOURCE


class MailMCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "oauth.json"
        self.oauth = OAuthServer("test", "test-password", self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def test_anonymous_calls_never_execute(self):
        with patch.object(server, "execute") as execute:
            _, response = server.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "send_email"}}, "", self.oauth)
        execute.assert_not_called()
        self.assertTrue(response["result"]["isError"])
        self.assertIn("mcp/www_authenticate", response["result"]["_meta"])

    def test_mailbox_read_is_peek_and_readonly(self):
        with patch.object(server.notify, "load_options", return_value=([], None, [])), patch.object(server.notify, "imap_connect") as connect, patch.object(server.notify, "imap_fetch_message", return_value=None) as fetch:
            connect.return_value.select.return_value = ("OK", [])
            server.execute("receive_email", {"uid": "123"})
            connect.return_value.select.assert_called_once_with(server.notify.encode_imap_mailbox("INBOX"), readonly=True)
            fetch.assert_called_once_with(connect.return_value, "123", peek=True)

    def test_delivery_id_prevents_resend_and_rejects_changed_content(self):
        args = {"subject": "Test", "body": "Body", "request_id": "test-1"}
        with patch.object(server, "MCP_OAUTH_STATE_FILE", self.state), patch.object(server.notify, "load_options", return_value=([], None, [])), patch.object(server.notify, "build_message", return_value=(object(), ["test@example.com"])), patch.object(server.notify, "send_message") as send:
            self.assertEqual(server.execute("send_email", args)["status"], "submitted")
            self.assertEqual(server.execute("send_email", args)["status"], "submitted")
            send.assert_called_once()
            with self.assertRaises(ValueError):
                server.execute("send_email", {**args, "body": "changed"})

    def test_unknown_delivery_is_not_retried(self):
        args = {"subject": "Test", "body": "Body", "request_id": "test-2"}
        with patch.object(server, "MCP_OAUTH_STATE_FILE", self.state), patch.object(server.notify, "load_options", return_value=([], None, [])), patch.object(server.notify, "build_message", return_value=(object(), [])), patch.object(server.notify, "send_message", side_effect=TimeoutError) as send:
            self.assertEqual(server.execute("send_email", args)["status"], "unknown")
            self.assertEqual(server.execute("send_email", args)["status"], "unknown")
            send.assert_called_once()

    def test_validation_and_template_escape(self):
        for args in ({"limit": True}, {"uid": "1 ALL"}, {"limit": 100}):
            with self.assertRaises(ValueError):
                server.validate("receive_email", args)
        rendered = server.minimal("Test", "<script>bad</script>")
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("正文示例", rendered)

    def test_all_templates_replace_examples_and_keep_original_styles(self):
        from html.parser import HTMLParser
        class TextCollector(HTMLParser):
            def __init__(self):
                super().__init__()
                self.values = []
            def handle_data(self, value):
                self.values.append(value)
        for name in server.TEMPLATE_NAMES:
            with self.subTest(template=name):
                source = (server.ROOT / "templates" / (name + ".html")).read_text()
                rendered = server.render_template(name, "真实主题 <标题>", "真实正文\n<script>内容</script>")
                import re
                self.assertEqual(re.search(r"<style>.*?</style>", source, re.S)[0], re.search(r"<style>.*?</style>", rendered, re.S)[0])
                self.assertNotIn("<script>", rendered)
                self.assertIn("&lt;script&gt;", rendered)
                collector = TextCollector(); collector.feed(rendered)
                text = "".join(collector.values)
                self.assertIn("真实主题 <标题>", text)
                self.assertIn("真实正文", text)
                for sample in ("示例", "二级标题", "example-service", "99.98%", "ONLINE", "2026-10-06", "https://example.com"):
                    self.assertNotIn(sample, rendered)
                self.assertEqual(rendered.count("<td"), rendered.count("</td>"))

    def test_each_template_send_uses_rendered_html(self):
        for name in server.TEMPLATE_NAMES:
            with self.subTest(template=name), patch.object(server, "MCP_OAUTH_STATE_FILE", self.state), patch.object(server.notify, "load_options", return_value=([], None, [])), patch.object(server.notify, "build_message", return_value=(object(), [])) as build, patch.object(server.notify, "send_message"):
                server.execute("send_email", {"subject": "主题", "body": "正文", "format": name, "request_id": "send-" + name})
                self.assertEqual(build.call_args.kwargs["html"], server.render_template(name, "主题", "正文"))
                self.assertEqual(build.call_args.kwargs["text"], "正文")

    def test_each_template_queue_uses_rendered_html_file(self):
        import subprocess
        def enqueue(command, **kwargs):
            self.assertIn("--html-file", command)
            document = Path(command[-1]).read_text()
            self.assertEqual(document, server.render_template(current, "主题", "正文"))
            return subprocess.CompletedProcess(command, 0, "", "")
        for current in server.TEMPLATE_NAMES:
            with self.subTest(template=current), patch("subprocess.run", side_effect=enqueue):
                result = server.execute("enqueue_email", {"subject": "主题", "body": "正文", "format": current, "queue": "test", "id": "queue-" + current})
                self.assertEqual(result["status"], "queued")

    def test_template_enum_matches_send_queue_and_rejects_file_paths(self):
        for tool in server.TOOLS:
            if tool["name"] in ("send_email", "enqueue_email"):
                formats = tool["inputSchema"]["properties"]["format"]
                self.assertEqual(set(formats["enum"]), {*server.TEMPLATE_NAMES, "html", "text"})
                self.assertEqual(formats["default"], "minimal")
        with self.assertRaises(ValueError):
            server.render_template("../config.ini", "主题", "正文")

    def test_oauth_pkce_resource_binding_and_code_consumption(self):
        metadata = {"redirect_uris": ["https://chatgpt.com/connector/oauth/test"], "grant_types": ["authorization_code", "refresh_token"]}
        _, _, body = self.oauth._register_client(json.dumps(metadata).encode(), "application/json")
        client = json.loads(body)["client_id"]
        verifier = "a" * 43
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"client_id": client, "redirect_uri": metadata["redirect_uris"][0], "response_type": "code", "resource": MCP_RESOURCE, "state": "state", "code_challenge_method": "S256", "code_challenge": challenge}
        grant = self.oauth._validate_authorization_request({k: [v] for k, v in params.items()})
        from oauth_server import _token_hash
        import time
        self.oauth.authorization_codes[_token_hash("code")] = {**grant, "expires_at": time.time() + 60}
        request = urlencode({"client_id": client, "redirect_uri": params["redirect_uri"], "grant_type": "authorization_code", "resource": MCP_RESOURCE, "code": "code", "code_verifier": verifier}).encode()
        status, _, body = self.oauth._token(request, "application/x-www-form-urlencoded")
        self.assertEqual(status, 200)
        token = json.loads(body)["access_token"]
        self.assertTrue(self.oauth.validate_access_token("Bearer " + token))
        self.assertEqual(self.oauth._token(request, "application/x-www-form-urlencoded")[0], 400)

    def test_ssh_approval_requires_exact_active_session_and_browser_cookie(self):
        import time
        from oauth_server import _token_hash
        self.oauth.login_sessions[_token_hash("sid")] = {
            "request": {"client_id": "client", "state": "state", "redirect_uri": "https://chatgpt.com/connector/oauth/test"},
            "csrf": _token_hash("csrf"), "expires_at": time.time() + 60, "attempts": 0,
        }
        self.assertFalse(self.oauth.approve_ssh_session("other-client", "state"))
        self.assertEqual(self.oauth.handle_get("/oauth/ssh-consent", "", {"Cookie": "berry_oauth_session=sid"})[0], 403)
        self.assertTrue(self.oauth.approve_ssh_session("client", "state"))
        self.assertEqual(self.oauth.handle_get("/oauth/ssh-consent", "", {})[0], 403)
        response = self.oauth.handle_get("/oauth/ssh-consent", "", {"Cookie": "berry_oauth_session=sid"})
        self.assertEqual(response[0], 200)
        import re
        csrf = re.search(r'name="csrf" value="([^"]+)"', response[2].decode()).group(1)
        self.oauth.login_sessions[_token_hash("sid")]["ssh_approved_until"] = time.time() - 1
        response = self.oauth._authorize_user(urlencode({"csrf": csrf, "consent": "yes"}).encode(), "application/x-www-form-urlencoded", {"Cookie": "berry_oauth_session=sid"})
        self.assertNotEqual(response[0], 303)

    def test_callback_csp_and_ssh_session_one_time_consumption(self):
        import time
        from oauth_server import _token_hash
        self.assertIn("https://chatgpt.com", self.oauth._html_headers()["Content-Security-Policy"])
        self.oauth.login_sessions[_token_hash("sid")] = {
            "request": {"client_id": "client", "state": "state", "redirect_uri": "https://chatgpt.com/connector/oauth/test"},
            "csrf": _token_hash("csrf"), "expires_at": time.time() + 60, "attempts": 0,
        }
        self.assertTrue(self.oauth.approve_ssh_session("client", "state"))
        request = urlencode({"csrf": "csrf", "consent": "yes"}).encode()
        headers = {"Cookie": "berry_oauth_session=sid"}
        self.assertEqual(self.oauth._authorize_user(request, "application/x-www-form-urlencoded", headers)[0], 303)
        self.assertNotEqual(self.oauth._authorize_user(request, "application/x-www-form-urlencoded", headers)[0], 303)


if __name__ == "__main__":
    unittest.main()
