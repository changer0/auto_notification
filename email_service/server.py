"""Single-user authenticated, stateless Streamable HTTP mail MCP service."""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import socketserver
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import notify
from oauth_server import create_oauth_server
from settings import MCP_OAUTH_STATE_FILE, MCP_PUBLIC_ORIGIN

from email_service import service
GUIDE = service.GUIDE
TOOLS = service.TOOLS
TEMPLATE_NAMES = service.TEMPLATE_NAMES
render_template = service.render_template
minimal = service.minimal
validate = service.validate

def execute(name,args):
    return service.execute(name,args,context="mcp")


def rpc(payload, authorization, oauth):
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method, request_id = payload.get("method"), payload.get("id")
    params = payload.get("params", {})
    if not isinstance(params, dict):
        params = {}
    def result(value):
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": value}
    if request_id is None:
        return 202, None
    if method == "initialize":
        version = params.get("protocolVersion")
        if version not in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            version = "2025-06-18"
        return result({"protocolVersion": version, "capabilities": {"tools": {}},
                       "serverInfo": {"name": "berry-email", "version": "1.2.0"}, "instructions": service.instructions('mcp')['instructions']})
    if method == "ping":
        return result({})
    if method == "tools/list":
        return result({"tools": TOOLS})
    if method == "tools/call":
        if not oauth.validate_access_token(authorization):
            return result({"content": [{"type": "text", "text": "请连接 Berry 邮件。"}], "isError": True,
                           "_meta": {"mcp/www_authenticate": [oauth.auth_challenge()]}})
        try:
            value = execute(params.get("name"), params.get("arguments", {}))
            return result({"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": value})
        except (Exception, SystemExit):
            # Never expose configuration, raw provider errors or mail contents in logs.
            return result({"content": [{"type": "text", "text": "操作失败，请检查参数、唯一 ID 或 Berry 邮件服务配置；未确认发送成功。"}], "isError": True})
    return 200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *_):
        pass  # OAuth query strings contain authorization details.

    def respond(self, status, headers, body):
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/.well-known/oauth-protected-resource/mcp":
            path = "/.well-known/oauth-protected-resource"
        response = self.server.oauth.handle_get(path, parts.query, dict(self.headers.items()))
        if response:
            self.respond(*response)
        elif path == "/":
            self.respond(200, {"Content-Type": "text/html; charset=utf-8"},
                         '<!doctype html><meta charset="utf-8"><title>Berry 邮件</title><h1>Berry 邮件</h1><p>ChatGPT MCP 邮件服务 · OAuth 授权</p><p>接入地址：/mcp</p>'.encode())
        elif path == "/health":
            self.respond(200, {"Content-Type": "application/json"}, b'{"status":"ok","service":"berry-email"}')
        else:
            self.respond(405 if path == "/mcp" else 404, {"Allow": "POST"}, b"")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= service.REQUEST_LIMITS['mcp']:
                self.respond(413, {}, b"")
                return
            body = self.rfile.read(length)
            path = urlsplit(self.path).path
            if path.startswith("/oauth/"):
                response = self.server.oauth.handle_post(path, body, self.headers.get("Content-Type", ""), dict(self.headers.items()))
                self.respond(*(response or (404, {}, b"")))
            elif path == "/mcp":
                origin = self.headers.get("Origin")
                if origin and origin != MCP_PUBLIC_ORIGIN:
                    self.respond(403, {}, b"")
                    return
                status, value = rpc(json.loads(body), self.headers.get("Authorization", ""), self.server.oauth)
                self.respond(status, {"Content-Type": "application/json"}, json.dumps(value, ensure_ascii=False).encode() if value is not None else b"")
            else:
                self.respond(404, {}, b"")
        except (ValueError, UnicodeDecodeError):
            self.respond(400, {}, b"")


def main():
    username = os.environ.get("UPLOAD_USER")
    password = os.environ.get("UPLOAD_PASSWORD")
    if not username or not password:
        raise SystemExit("需要 Berry 现有用户登录配置")
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("EMAIL_PORT", "8771"))), Handler)
    server.oauth = create_oauth_server(username, password)
    class ApprovalHandler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            try:
                data = json.loads(self.rfile.readline(4097))
                approved = server.oauth.approve_ssh_session(data.get("client_id", ""), data.get("state", ""))
            except (ValueError, AttributeError, OSError):
                approved = False
            self.wfile.write(json.dumps({"approved": approved}).encode() + b"\n")
    socket_path = MCP_OAUTH_STATE_FILE.parent / "approval.sock"
    if socket_path.exists():
        socket_path.unlink()
    local = socketserver.UnixStreamServer(str(socket_path), ApprovalHandler)
    os.chmod(socket_path, 0o600)
    threading.Thread(target=local.serve_forever, daemon=True).start()
    from email_service.local_tools import start_executor_server
    executor = start_executor_server(MCP_OAUTH_STATE_FILE.parent / 'tools.sock')
    try:
        server.serve_forever()
    finally:
        executor.shutdown();executor.server_close()
        (MCP_OAUTH_STATE_FILE.parent / 'tools.sock').unlink(missing_ok=True)
        local.shutdown();local.server_close();server.server_close()


if __name__ == "__main__":
    main()
