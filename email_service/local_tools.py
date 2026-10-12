"""Owner-only executor adapter; never accepts a client-selected context."""
import ctypes
import json
import os
import socket
import socketserver
import struct
import threading
from . import service

MAX_REQUEST = service.REQUEST_LIMITS['executor']


def peer_uid(connection):
    if hasattr(socket, 'SO_PEERCRED'):
        return struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    function = getattr(ctypes.CDLL(None), 'getpeereid', None)
    if function is None or function(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise PermissionError('无法核验本地调用方')
    return uid.value


def dispatch(payload):
    if not isinstance(payload, dict) or set(payload) - {'action', 'tool', 'arguments'}:
        raise ValueError('请求格式无效')
    if payload.get('action') == 'describe':
        return service.instructions('executor')
    if payload.get('action', 'call') != 'call':
        raise ValueError('操作无效')
    return service.execute(payload.get('tool'), payload.get('arguments', {}), context='executor')


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(90)
        try:
            if peer_uid(self.request) != os.getuid():
                raise PermissionError('本地调用方未授权')
            raw = self.rfile.readline(MAX_REQUEST + 1)
            if len(raw) > MAX_REQUEST or not raw.endswith(b'\n'):
                raise ValueError('请求过大或不完整')
            value = dispatch(json.loads(raw))
        except (Exception, SystemExit):
            value = {'status': 'failed', 'error': '邮件工具未确认成功，请核对参数或服务；勿换ID重发'}
        self.wfile.write(json.dumps(value, ensure_ascii=False).encode() + b'\n')


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def start_executor_server(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.unlink(missing_ok=True)
    server = Server(str(path), Handler)
    os.chmod(path, 0o600)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
