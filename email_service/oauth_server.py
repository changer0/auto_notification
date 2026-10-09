"""Single-user OAuth 2.1 authorization for ChatGPT's Berry email tools.

The existing uploader credentials authenticate the user at Berry. ChatGPT is
registered as a public OAuth client and receives opaque, short-lived bearer
tokens bound to the email MCP resource and the email:access scope.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from settings import MCP_OAUTH_STATE_FILE, MCP_PUBLIC_ORIGIN

PUBLIC_ORIGIN = MCP_PUBLIC_ORIGIN
MCP_RESOURCE = PUBLIC_ORIGIN + "/mcp"
RESOURCE_METADATA_URL = PUBLIC_ORIGIN + "/.well-known/oauth-protected-resource"
CLIENT_ID_PREFIX = "berry-chatgpt-"
MAX_REGISTERED_CLIENTS = 1000
AUTHORIZATION_CODE_TTL = 300
LOGIN_SESSION_TTL = 600
ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 90 * 24 * 3600
SUPPORTED_SCOPE = "email:access"


class OAuthError(ValueError):
    def __init__(self, status: int, code: str, description: str):
        super().__init__(description)
        self.status = status
        self.code = code
        self.description = description


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _one(params: dict[str, list[str]], key: str, *, required: bool = True) -> str:
    values = params.get(key, [])
    if len(values) > 1:
        raise OAuthError(400, "invalid_request", f"重复的 {key} 参数。")
    if not values:
        if required:
            raise OAuthError(400, "invalid_request", f"缺少 {key} 参数。")
        return ""
    return values[0]


def _valid_redirect_uri(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.hostname not in {"chatgpt.com", "chat.openai.com"}:
            return False
        if parts.username or parts.password or parts.port not in (None, 443) or parts.query or parts.fragment:
            return False
    except ValueError:
        return False
    return parts.path == "/connector_platform_oauth_redirect" or bool(
        re.fullmatch(r"/connector/oauth/[A-Za-z0-9_-]{1,200}", parts.path)
    )


class OAuthServer:
    def __init__(self, username: str, password: str, state_file: Path):
        self.username = username
        self.password = password
        self.state_file = state_file
        self.lock = threading.RLock()
        self.authorization_codes: dict[str, dict] = {}
        self.login_sessions: dict[str, dict] = {}
        self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.state_file.parent, 0o700)
        except OSError:
            pass
        self.state = self._load_state()

    def _load_state(self) -> dict:
        try:
            with self.state_file.open("r", encoding="utf-8") as source:
                value = json.load(source)
            if isinstance(value, dict):
                return {
                    "clients": value.get("clients", {}) if isinstance(value.get("clients"), dict) else {},
                    "access_tokens": value.get("access_tokens", {}) if isinstance(value.get("access_tokens"), dict) else {},
                    "refresh_tokens": value.get("refresh_tokens", {}) if isinstance(value.get("refresh_tokens"), dict) else {},
                }
        except (OSError, json.JSONDecodeError):
            pass
        return {"clients": {}, "access_tokens": {}, "refresh_tokens": {}}

    def _save_state(self) -> None:
        temporary = self.state_file.with_name(self.state_file.name + "." + uuid.uuid4().hex + ".tmp")
        encoded = json.dumps(self.state, separators=(",", ":")).encode("utf-8")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.state_file)
            os.chmod(self.state_file, 0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def metadata(self, path: str) -> tuple[int, dict[str, str], bytes] | None:
        if path == "/.well-known/oauth-protected-resource":
            value = {
                "resource": MCP_RESOURCE,
                "authorization_servers": [PUBLIC_ORIGIN],
                "scopes_supported": [SUPPORTED_SCOPE],
                "resource_documentation": PUBLIC_ORIGIN,
            }
            return 200, self._json_headers(), json.dumps(value, separators=(",", ":")).encode()
        if path == "/.well-known/oauth-authorization-server":
            value = {
                "issuer": PUBLIC_ORIGIN,
                # Error responses render a local page, so not every response
                # includes an issuer parameter.
                "authorization_response_iss_parameter_supported": False,
                "client_id_metadata_document_supported": False,
                "authorization_endpoint": PUBLIC_ORIGIN + "/oauth/authorize",
                "token_endpoint": PUBLIC_ORIGIN + "/oauth/token",
                "registration_endpoint": PUBLIC_ORIGIN + "/oauth/register",
                "token_endpoint_auth_methods_supported": ["none"],
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "scopes_supported": [SUPPORTED_SCOPE],
            }
            return 200, self._json_headers(), json.dumps(value, separators=(",", ":")).encode()
        return None

    @staticmethod
    def _json_headers() -> dict[str, str]:
        return {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}

    @staticmethod
    def _html_headers() -> dict[str, str]:
        return {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://chatgpt.com https://chat.openai.com; base-uri 'none'; frame-ancestors 'none'",
        }

    @staticmethod
    def _cookie_value(cookie_header: str, name: str) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
            return cookie[name].value if name in cookie else ""
        except Exception:
            return ""

    def _html_page(self, title: str, content: str, status: int = 200, extra_headers: dict | None = None):
        page = (
            "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(title)}</title><body style=\"font:16px system-ui;max-width:34rem;margin:4rem auto;padding:0 1rem;color:#17211d\">"
            f"{content}</body></html>"
        ).encode("utf-8")
        headers = self._html_headers()
        if extra_headers:
            headers.update(extra_headers)
        return status, headers, page

    def _error_page(self, description: str, status: int = 400):
        return self._html_page("Berry 邮件", f"<h1>无法连接</h1><p>{html.escape(description)}</p>", status)

    def handle_get(self, path: str, query: str, headers: dict[str, str]):
        metadata = self.metadata(path)
        if metadata is not None:
            return metadata
        if path == "/oauth/ssh-consent":
            cookie = next((v for k, v in headers.items() if k.lower() == "cookie"), "")
            sid = self._cookie_value(cookie, "berry_oauth_session")
            with self.lock:
                session = self.login_sessions.get(_token_hash(sid)) if sid else None
                now = time.time()
                if not session or session["expires_at"] <= now or session.get("ssh_approved_until", 0) <= now:
                    return self._error_page("本次浏览器会话尚未通过 Berry SSH 确认或确认已过期。", 403)
                csrf = secrets.token_urlsafe(32)
                session["csrf"] = _token_hash(csrf)
            form = (
                "<h1>连接 Berry 邮件</h1><p>本次浏览器会话已由 Berry 的本机管理员通过 SSH 确认。</p>"
                "<p>ChatGPT 将可读取邮箱、发送邮件和提交通知队列。</p>"
                '<form method="post" action="/oauth/authorize">'
                f'<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">'
                '<label><input type="checkbox" name="consent" value="yes" required> 允许 ChatGPT 读取邮箱、发送邮件和提交通知队列</label>'
                '<p><button>授权并连接</button></p></form>'
            )
            return self._html_page("连接 Berry 邮件", form)
        if path != "/oauth/authorize":
            return None
        try:
            request = self._validate_authorization_request(parse_qs(query, keep_blank_values=True))
        except OAuthError as error:
            return self._error_page(error.description, error.status)

        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        with self.lock:
            self._prune_locked()
            self.login_sessions[_token_hash(sid)] = {
                "request": request,
                "csrf": _token_hash(csrf),
                "expires_at": time.time() + LOGIN_SESSION_TTL,
                "attempts": 0,
            }
        form = (
            "<h1>连接 Berry 邮件</h1>"
            "<p>连接后，ChatGPT 可以在你明确要求时读取邮箱、发送邮件和提交通知队列。</p>"
            "<form method=\"post\" action=\"/oauth/authorize\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{html.escape(csrf, quote=True)}\">"
            "<label>Berry 用户名<br><input name=\"username\" autocomplete=\"username\" required style=\"box-sizing:border-box;width:100%;padding:.7rem;margin:.35rem 0 1rem\"></label>"
            "<label>Berry 密码<br><input type=\"password\" name=\"password\" autocomplete=\"current-password\" required style=\"box-sizing:border-box;width:100%;padding:.7rem;margin:.35rem 0 1rem\"></label>"
            "<label style=\"display:block;margin:0 0 1.2rem\"><input type=\"checkbox\" name=\"consent\" value=\"yes\" required> 允许 ChatGPT 读取邮箱、发送邮件和提交通知队列</label>"
            "<button style=\"padding:.7rem 1rem\">登录并连接</button></form>"
        )
        cookie = f"berry_oauth_session={sid}; Path=/oauth; HttpOnly; Secure; SameSite=Lax; Max-Age={LOGIN_SESSION_TTL}"
        return self._html_page("连接 Berry 邮件", form, extra_headers={"Set-Cookie": cookie})

    def approve_ssh_session(self, client_id: str, state: str) -> bool:
        """Called only through a user-owned Unix socket, never through HTTP."""
        if not client_id or not state:
            return False
        with self.lock:
            self._prune_locked()
            sessions = [s for s in self.login_sessions.values()
                        if s["request"]["client_id"] == client_id and s["request"]["state"] == state]
            if len(sessions) != 1:
                return False
            sessions[0]["ssh_approved_until"] = time.time() + 120
            return True

    def _validate_authorization_request(self, params: dict[str, list[str]]) -> dict:
        client_id = _one(params, "client_id")
        redirect_uri = _one(params, "redirect_uri")
        if _one(params, "response_type") != "code":
            raise OAuthError(400, "unsupported_response_type", "只支持 OAuth authorization code flow。")
        if not _valid_redirect_uri(redirect_uri):
            raise OAuthError(400, "invalid_request", "ChatGPT 回调地址无效。")
        with self.lock:
            client = self.state["clients"].get(client_id)
        if client_id.startswith(CLIENT_ID_PREFIX):
            if not client or redirect_uri not in client.get("redirect_uris", []):
                raise OAuthError(400, "invalid_client", "OAuth 客户端未注册或回调地址不匹配。")
        else:
            raise OAuthError(400, "invalid_client", "仅允许 ChatGPT OAuth 客户端。")

        state = _one(params, "state")
        if len(state) > 1024:
            raise OAuthError(400, "invalid_request", "state 参数过长。")
        resource = _one(params, "resource")
        if resource != MCP_RESOURCE:
            raise OAuthError(400, "invalid_target", "OAuth resource 与 Berry MCP 服务不匹配。")
        scope = _one(params, "scope", required=False) or SUPPORTED_SCOPE
        requested_scopes = set(scope.split())
        if not requested_scopes.issubset({SUPPORTED_SCOPE}) or SUPPORTED_SCOPE not in requested_scopes:
            raise OAuthError(400, "invalid_scope", "只支持 email:access 权限。")
        if _one(params, "code_challenge_method") != "S256":
            raise OAuthError(400, "invalid_request", "必须使用 PKCE S256。")
        challenge = _one(params, "code_challenge")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
            raise OAuthError(400, "invalid_request", "PKCE challenge 格式无效。")
        return {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "resource": resource,
            "scope": SUPPORTED_SCOPE,
            "code_challenge": challenge,
            "refresh_grant": "refresh_token" in client.get("grant_types", []),
        }

    def handle_post(self, path: str, body: bytes, content_type: str, headers: dict[str, str]):
        if path == "/oauth/register":
            return self._register_client(body, content_type)
        if path == "/oauth/token":
            return self._token(body, content_type)
        if path == "/oauth/authorize":
            return self._authorize_user(body, content_type, headers)
        return None

    def _register_client(self, body: bytes, content_type: str):
        if not content_type.lower().startswith("application/json"):
            return self._json_error(415, "invalid_client_metadata", "DCR requests must use JSON.")
        try:
            metadata = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json_error(400, "invalid_client_metadata", "DCR JSON is invalid.")
        if not isinstance(metadata, dict):
            return self._json_error(400, "invalid_client_metadata", "DCR metadata must be an object.")
        redirect_uris = metadata.get("redirect_uris")
        if (
            not isinstance(redirect_uris, list)
            or not redirect_uris
            or len(redirect_uris) > 10
            or not all(_valid_redirect_uri(uri) for uri in redirect_uris)
        ):
            return self._json_error(400, "invalid_redirect_uri", "仅允许 ChatGPT 的 HTTPS OAuth 回调地址。")
        grant_types = metadata.get("grant_types", ["authorization_code"])
        response_types = metadata.get("response_types", ["code"])
        auth_method = metadata.get("token_endpoint_auth_method", "none")
        if grant_types not in (["authorization_code"], ["authorization_code", "refresh_token"], ["refresh_token", "authorization_code"]):
            return self._json_error(400, "invalid_client_metadata", "仅支持 authorization_code 和 refresh_token。")
        if response_types != ["code"] or auth_method != "none":
            return self._json_error(400, "invalid_client_metadata", "客户端必须使用 public client、authorization code 和 PKCE。")
        client_id = CLIENT_ID_PREFIX + secrets.token_urlsafe(24)
        client_name = metadata.get("client_name", "ChatGPT")
        if not isinstance(client_name, str):
            client_name = "ChatGPT"
        client_name = client_name[:120]
        registered = {
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "grant_types": grant_types,
            "created_at": int(time.time()),
        }
        with self.lock:
            if len(self.state["clients"]) >= MAX_REGISTERED_CLIENTS:
                return self._json_error(429, "registration_not_supported", "Berry OAuth 客户端注册数量已达上限。")
            self.state["clients"][client_id] = registered
            self._save_state()
        response = {
            "client_id": client_id,
            "client_id_issued_at": registered["created_at"],
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "grant_types": grant_types,
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SUPPORTED_SCOPE,
        }
        return 201, self._json_headers(), json.dumps(response, separators=(",", ":")).encode()

    @staticmethod
    def _form_params(body: bytes, content_type: str):
        if not content_type.lower().startswith("application/x-www-form-urlencoded"):
            raise OAuthError(415, "invalid_request", "OAuth 请求必须使用 application/x-www-form-urlencoded。")
        try:
            return parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
        except (UnicodeDecodeError, ValueError):
            raise OAuthError(400, "invalid_request", "OAuth 表单格式无效。")

    def _authorize_user(self, body: bytes, content_type: str, headers: dict[str, str]):
        # HTTP field names are case-insensitive; proxies may normalize them
        # differently before forwarding the request to this service.
        cookie_header = next(
            (value for name, value in headers.items() if name.lower() == "cookie"),
            "",
        )
        sid = self._cookie_value(cookie_header, "berry_oauth_session")
        if not sid:
            logging.warning("OAuth authorization rejected: session cookie missing")
            return self._error_page("登录会话已过期，请返回 ChatGPT 重试。", 400)
        try:
            params = self._form_params(body, content_type)
            csrf = _one(params, "csrf")
            username = _one(params, "username", required=False)
            password = _one(params, "password", required=False)
            consent = _one(params, "consent", required=False)
        except OAuthError as error:
            return self._error_page(error.description, error.status)
        key = _token_hash(sid)
        with self.lock:
            session = self.login_sessions.get(key)
            if not session or session["expires_at"] < time.time():
                self.login_sessions.pop(key, None)
                logging.warning("OAuth authorization rejected: server-side login session missing or expired")
                return self._error_page("登录会话已过期，请返回 ChatGPT 重试。", 400)
            if not hmac.compare_digest(session["csrf"], _token_hash(csrf)):
                logging.warning("OAuth authorization rejected: CSRF token mismatch")
                return self._error_page("登录请求校验失败，请重新发起授权。", 400)
            if consent != "yes":
                return self._error_page("需要明确同意连接 Berry 邮件。", 400)
            credentials_match = (session.get("ssh_approved_until", 0) > time.time() or
                                 (hmac.compare_digest(username, self.username) and hmac.compare_digest(password, self.password)))
            if not credentials_match:
                logging.warning("OAuth authorization rejected: credentials mismatch")
                session["attempts"] += 1
                if session["attempts"] >= 5:
                    self.login_sessions.pop(key, None)
                    return self._error_page("登录失败次数过多，请返回 ChatGPT 重新发起授权。", 429)
                return self._login_error_page(sid, session, "用户名或密码不正确。")

            self.login_sessions.pop(key, None)
            code = secrets.token_urlsafe(32)
            request = session["request"]
            self.authorization_codes[_token_hash(code)] = {
                **request,
                "expires_at": time.time() + AUTHORIZATION_CODE_TTL,
            }
            logging.info("OAuth authorization approved; redirecting to registered callback")

        query = {"code": code, "state": request["state"]}
        separator = "&" if "?" in request["redirect_uri"] else "?"
        location = request["redirect_uri"] + separator + urlencode(query)
        expired_cookie = "berry_oauth_session=; Path=/oauth; HttpOnly; Secure; SameSite=Lax; Max-Age=0"
        return 303, {"Location": location, "Cache-Control": "no-store", "Set-Cookie": expired_cookie}, b""

    def _login_error_page(self, sid: str, session: dict, message: str):
        csrf = secrets.token_urlsafe(32)
        with self.lock:
            session["csrf"] = _token_hash(csrf)
        form = (
            "<h1>连接 Berry 邮件</h1>"
            f"<p style=\"color:#a22\">{html.escape(message)}</p>"
            "<p>连接后，ChatGPT 可以在你明确要求时读取邮箱、发送邮件和提交通知队列。</p>"
            "<form method=\"post\" action=\"/oauth/authorize\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{html.escape(csrf, quote=True)}\">"
            "<label>Berry 用户名<br><input name=\"username\" autocomplete=\"username\" required style=\"box-sizing:border-box;width:100%;padding:.7rem;margin:.35rem 0 1rem\"></label>"
            "<label>Berry 密码<br><input type=\"password\" name=\"password\" autocomplete=\"current-password\" required style=\"box-sizing:border-box;width:100%;padding:.7rem;margin:.35rem 0 1rem\"></label>"
            "<label style=\"display:block;margin:0 0 1.2rem\"><input type=\"checkbox\" name=\"consent\" value=\"yes\" required> 允许 ChatGPT 读取邮箱、发送邮件和提交通知队列</label>"
            "<button style=\"padding:.7rem 1rem\">登录并连接</button></form>"
        )
        cookie = f"berry_oauth_session={sid}; Path=/oauth; HttpOnly; Secure; SameSite=Lax; Max-Age={LOGIN_SESSION_TTL}"
        return self._html_page("连接 Berry 邮件", form, extra_headers={"Set-Cookie": cookie})

    def _token(self, body: bytes, content_type: str):
        try:
            params = self._form_params(body, content_type)
            grant_type = _one(params, "grant_type")
            client_id = _one(params, "client_id")
            with self.lock:
                client = self.state["clients"].get(client_id)
            if not client:
                raise OAuthError(400, "invalid_client", "仅允许 ChatGPT OAuth 客户端。")
            resource = _one(params, "resource")
            if resource != MCP_RESOURCE:
                raise OAuthError(400, "invalid_target", "OAuth resource 与 Berry MCP 服务不匹配。")

            if grant_type == "authorization_code":
                code = _one(params, "code")
                redirect_uri = _one(params, "redirect_uri")
                verifier = _one(params, "code_verifier")
                if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
                    raise OAuthError(400, "invalid_grant", "PKCE verifier 格式无效。")
                with self.lock:
                    grant = self.authorization_codes.pop(_token_hash(code), None)
                if not grant or grant["expires_at"] < time.time():
                    raise OAuthError(400, "invalid_grant", "授权码无效或已过期。")
                if grant["client_id"] != client_id or grant["redirect_uri"] != redirect_uri or grant["resource"] != resource:
                    raise OAuthError(400, "invalid_grant", "授权码绑定信息不匹配。")
                derived = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if not hmac.compare_digest(derived, grant["code_challenge"]):
                    raise OAuthError(400, "invalid_grant", "PKCE 校验失败。")
                token_pair = self._issue_tokens(
                    client_id, resource, grant["scope"],
                    allow_refresh=bool(grant.get("refresh_grant")),
                )
                return 200, self._json_headers(), json.dumps(token_pair, separators=(",", ":")).encode()

            if grant_type == "refresh_token":
                refresh = _one(params, "refresh_token")
                token_key = _token_hash(refresh)
                with self.lock:
                    grant = self.state["refresh_tokens"].pop(token_key, None)
                    if grant:
                        self._prune_locked()
                        self._save_state()
                if not grant or grant["expires_at"] < time.time():
                    raise OAuthError(400, "invalid_grant", "Refresh token 无效或已过期，请重新连接。")
                if grant["client_id"] != client_id or grant["resource"] != resource:
                    raise OAuthError(400, "invalid_grant", "Refresh token 绑定信息不匹配。")
                token_pair = self._issue_tokens(client_id, resource, grant["scope"], allow_refresh=True)
                return 200, self._json_headers(), json.dumps(token_pair, separators=(",", ":")).encode()
            raise OAuthError(400, "unsupported_grant_type", "只支持 authorization_code 和 refresh_token。")
        except OAuthError as error:
            return self._json_error(error.status, error.code, error.description)

    def _issue_tokens(self, client_id: str, resource: str, scope: str, *, allow_refresh: bool) -> dict:
        access = secrets.token_urlsafe(32)
        now = int(time.time())
        token_pair = {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "scope": scope,
        }
        with self.lock:
            self._prune_locked()
            self.state["access_tokens"][_token_hash(access)] = {
                "client_id": client_id,
                "resource": resource,
                "scope": scope,
                "expires_at": now + ACCESS_TOKEN_TTL,
            }
            if allow_refresh:
                refresh = secrets.token_urlsafe(48)
                self.state["refresh_tokens"][_token_hash(refresh)] = {
                    "client_id": client_id,
                    "resource": resource,
                    "scope": scope,
                    "expires_at": now + REFRESH_TOKEN_TTL,
                }
                token_pair["refresh_token"] = refresh
            self._save_state()
        return token_pair

    def _prune_locked(self) -> None:
        now = time.time()
        for key in ("access_tokens", "refresh_tokens"):
            self.state[key] = {
                token: value for token, value in self.state[key].items()
                if isinstance(value, dict) and float(value.get("expires_at", 0)) > now
            }
        self.authorization_codes = {
            token: value for token, value in self.authorization_codes.items()
            if float(value.get("expires_at", 0)) > now
        }
        self.login_sessions = {
            token: value for token, value in self.login_sessions.items()
            if float(value.get("expires_at", 0)) > now
        }

    def validate_access_token(self, authorization: str) -> bool:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or len(token) > 512:
            return False
        token_key = _token_hash(token)
        with self.lock:
            self._prune_locked()
            grant = self.state["access_tokens"].get(token_key)
            if not grant:
                return False
            return (
                grant.get("resource") == MCP_RESOURCE
                and grant.get("scope") == SUPPORTED_SCOPE
                and float(grant.get("expires_at", 0)) > time.time()
            )

    def auth_challenge(self, error: str = "invalid_token") -> str:
        description = "请连接 Berry 邮件以使用邮箱能力。"
        return (
            f'Bearer resource_metadata="{RESOURCE_METADATA_URL}", '
            f'error="{error}", error_description="{description}"'
        )

    def _json_error(self, status: int, code: str, description: str):
        value = {"error": code, "error_description": description}
        return status, self._json_headers(), json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def create_oauth_server(username: str, password: str) -> OAuthServer:
    return OAuthServer(username, password, MCP_OAUTH_STATE_FILE)
