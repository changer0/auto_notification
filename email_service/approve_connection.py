"""Approve one existing browser OAuth session using the Berry SSH identity."""
import argparse
import json
import socket
from settings import MCP_OAUTH_STATE_FILE

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--client-id", required=True)
parser.add_argument("--state", required=True)
args = parser.parse_args()
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.settimeout(5)
    client.connect(str(MCP_OAUTH_STATE_FILE.parent / "approval.sock"))
    client.sendall(json.dumps({"client_id": args.client_id, "state": args.state}).encode() + b"\n")
    result = json.loads(client.recv(1024))
if not result.get("approved"):
    raise SystemExit("确认失败：没有唯一的有效浏览器请求。请重新发起 ChatGPT 连接。")
print("已确认此次浏览器会话，有效期 120 秒。请在同一浏览器访问 /oauth/ssh-consent 并确认权限。")
