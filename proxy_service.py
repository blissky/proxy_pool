#!/usr/bin/env python3
"""8082 proxy entry and 8083 Web UI backed by sing-box and Redis."""

import argparse
import base64
import hmac
import json
import os
import random
import secrets
import select
import signal
import socket
import threading
import time
import urllib.parse
from collections import deque
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer

from core.share_link import export_share_links
from core.store import NodeStore
from core.sync import SyncManager
from handler.configHandler import ConfigHandler
from proxy_chain import connect_to_proxy, connect_via_proxy


CONFIG_FILE = os.getenv("CONFIG_FILE", "config.json")
PROXY_TIMEOUT = max(1, int(os.getenv("PROXY_TIMEOUT", "5")))
FAIL_THRESHOLD = max(1, int(os.getenv("FAIL_THRESHOLD", "2")))
WEBUI_ACCESS_TOKEN = os.getenv("WEBUI_ACCESS_TOKEN", "sk-change-me")
WEBUI_SESSION_TIMEOUT_SECONDS = max(
    1, int(os.getenv("WEBUI_SESSION_TIMEOUT_SECONDS", "1800")),
)
CONFIG_DEFAULTS = {
    "listen": "0.0.0.0",
    "port": 8082,
    "stats_port": 8083,
    "max_clients": 100,
}

# /api/proxies 导出的代理链接主机部分，可填 IP、域名、127.0.0.1（默认）或容器名。
EXPORT_PROXY_HOST = os.getenv("EXPORT_PROXY_HOST", "127.0.0.1").strip() or "127.0.0.1"
EXPORT_PROXY_SCHEME = os.getenv("EXPORT_PROXY_SCHEME", "http").strip().lower()
if EXPORT_PROXY_SCHEME not in ("http", "https", "socks5", "socks5h"):
    EXPORT_PROXY_SCHEME = "http"
_FLAG_TRUE = {"1", "true", "yes", "on"}
_FLAG_FALSE = {"", "0", "false", "no", "off"}


class Config:
    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self.lock = threading.RLock()
        self.data = dict(CONFIG_DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, ValueError, TypeError):
            return
        if isinstance(value, dict):
            with self.lock:
                for key in CONFIG_DEFAULTS:
                    if key in value:
                        self.data[key] = value[key]

    def get(self, key, default=None):
        with self.lock:
            return self.data.get(key, default)

    def get_all(self):
        with self.lock:
            return dict(self.data)

    def update(self, values):
        changed = set()
        with self.lock:
            for key, value in values.items():
                if key in CONFIG_DEFAULTS and self.data.get(key) != value:
                    self.data[key] = value
                    changed.add(key)
        if changed:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as stream:
                json.dump(self.get_all(), stream, ensure_ascii=False, indent=2)
        return changed


class WebAuth:
    COOKIE_NAME = "proxypool_session"

    def __init__(self, access_token, session_timeout=1800):
        self.access_token = str(access_token)
        self.session_timeout = max(1, int(session_timeout))
        self.lock = threading.RLock()
        self.sessions = {}

    @classmethod
    def _cookie_session(cls, cookie_header):
        try:
            cookie = SimpleCookie()
            cookie.load(cookie_header or "")
        except CookieError:
            return ""
        item = cookie.get(cls.COOKIE_NAME)
        return item.value if item else ""

    def _prune(self, now):
        expired = [
            session for session, last_seen in self.sessions.items()
            if now - last_seen >= self.session_timeout
        ]
        for session in expired:
            self.sessions.pop(session, None)

    def login(self, access_token):
        candidate = str(access_token or "")
        if not hmac.compare_digest(candidate, self.access_token):
            return ""
        session = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self.lock:
            self._prune(now)
            self.sessions[session] = now
        return session

    def authenticate(self, cookie_header, touch=False):
        session = self._cookie_session(cookie_header)
        if not session:
            return ""
        now = time.monotonic()
        with self.lock:
            self._prune(now)
            if session not in self.sessions:
                return ""
            if touch:
                self.sessions[session] = now
        return session

    def check_token(self, candidate):
        """Constant-time check of an access token supplied as a query parameter.

        Used only by the read-only ``/api/proxies`` export; it does not create a
        session and never widens the cookie gate of other routes.
        """
        token = str(candidate or "")
        return bool(token) and hmac.compare_digest(token, self.access_token)

    def logout(self, cookie_header):
        session = self._cookie_session(cookie_header)
        if session:
            with self.lock:
                self.sessions.pop(session, None)

    @classmethod
    def session_cookie(cls, session):
        return "{}={}; Path=/; HttpOnly; SameSite=Strict".format(cls.COOKIE_NAME, session)

    @classmethod
    def expired_cookie(cls):
        return "{}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict".format(cls.COOKIE_NAME)


class Logger:
    def __init__(self, maxlen=1000):
        self.lock = threading.Lock()
        self.items = deque(maxlen=maxlen)
        self.seq = 0

    def log(self, message, level="INFO"):
        with self.lock:
            self.seq += 1
            self.items.append((self.seq, level, str(message)))
        print("[{}] {}".format(level, message), flush=True)

    def info(self, message):
        self.log(message, "INFO")

    def warn(self, message):
        self.log(message, "WARN")

    def error(self, message):
        self.log(message, "ERROR")

    def snapshot(self, since=0):
        with self.lock:
            return [
                {"seq": seq, "level": level, "msg": msg}
                for seq, level, msg in self.items if seq > since
            ]


class RuntimePool:
    def __init__(self, store, supervisor=None, threshold=3):
        self.store = store
        self.supervisor = supervisor
        self.threshold = max(1, int(threshold))
        self.lock = threading.RLock()
        self.failures = {}

    def pick(self, tls_required=False):
        lock = self.supervisor.lock if self.supervisor else threading.RLock()
        with lock:
            revision = None
            if self.supervisor:
                endpoint = self.supervisor.endpoint()
                if not endpoint:
                    return None
                revision = endpoint[2]
            nodes = [node for node in self.store.active(tls_required=tls_required)
                     if revision is None or node.config_revision == revision]
        return random.choice(nodes) if nodes else None

    def route(self, tls_required=False):
        if not self.supervisor:
            return None, None
        with self.supervisor.lock:
            endpoint = self.supervisor.endpoint()
            if not endpoint:
                return None, None
            revision = endpoint[2]
            nodes = [
                node for node in self.store.active(tls_required=tls_required)
                if node.config_revision == revision
            ]
            return endpoint, random.choice(nodes) if nodes else None

    def by_credentials(self, username, password):
        """Return the active node whose sing-box credential matches, else None.

        Only nodes of the revision currently served by the formal sing-box are
        considered, because any other credential would be blocked by the
        running ``final: block`` route table.
        """
        if not self.supervisor or not username:
            return None
        with self.supervisor.lock:
            endpoint = self.supervisor.endpoint()
            if not endpoint:
                return None
            revision = endpoint[2]
            for node in self.store.active():
                if revision is not None and node.config_revision != revision:
                    continue
                if node.inbound_username != username:
                    continue
                if hmac.compare_digest(str(node.inbound_password or ""), str(password or "")):
                    return node
        return None

    def success(self, node):
        with self.lock:
            self.failures.pop(node.node_id, None)

    def failure(self, node):
        with self.lock:
            count = self.failures.get(node.node_id, 0) + 1
            self.failures[node.node_id] = count
        if count >= self.threshold:
            return self.store.mark_unsynced(node.node_id)
        return False

    def stats(self):
        values = self.store.count()
        with self.lock:
            values["runtime_failures"] = sum(self.failures.values())
        return values


class RequestHeader:
    def __init__(self, data):
        self.data = data
        first, _, rest = data.partition(b"\r\n")
        pieces = first.decode("latin1", "replace").split()
        self.method = pieces[0].upper() if pieces else ""
        self.target = pieces[1] if len(pieces) > 1 else ""
        self.headers = {}
        for line in rest.split(b"\r\n"):
            if b":" not in line:
                continue
            key, value = line.split(b":", 1)
            self.headers[key.decode("latin1").lower()] = value.strip().decode("latin1")

    @property
    def is_connect(self):
        return self.method == "CONNECT"

    @property
    def host_port(self):
        target = self.target
        if self.is_connect and ":" in target:
            host, port = target.rsplit(":", 1)
            return host.strip("[]"), int(port)
        host = self.headers.get("host", "")
        if ":" in host:
            name, port = host.rsplit(":", 1)
            return name.strip("[]"), int(port)
        return host, 80

    def proxy_credentials(self):
        """Return ``(username, password)`` from Proxy-Authorization, else None."""
        raw = self.headers.get("proxy-authorization", "")
        if not raw:
            return None
        scheme, _, value = raw.partition(" ")
        if scheme.strip().lower() != "basic" or not value.strip():
            return None
        try:
            decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        if ":" not in decoded:
            return None
        username, _, password = decoded.partition(":")
        return (username, password) if username else None

    def to_upstream(self, username, password):
        header, separator, body = self.data.partition(b"\r\n\r\n")
        lines = []
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"proxy-authorization:"):
                continue
            lines.append(line)
        token = base64.b64encode((username + ":" + password).encode("utf-8")).decode("ascii")
        lines.append("Proxy-Authorization: Basic {}".format(token).encode("ascii"))
        return b"\r\n".join(lines) + separator + body


def _relay(left, right):
    sockets = [left, right]
    while sockets:
        readable, _, _ = select.select(sockets, [], [], 30)
        if not readable:
            continue
        for source in readable:
            destination = right if source is left else left
            try:
                data = source.recv(65536)
            except OSError:
                data = b""
            if not data:
                for sock in (left, right):
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                return
            try:
                destination.sendall(data)
            except OSError:
                return


def _recv_until(sock, marker=b"\r\n\r\n", limit=65536):
    data = b""
    while marker not in data and len(data) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _recv_exact(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def _socks5_choose_method(methods):
    """Pick a SOCKS5 method byte; None means "no acceptable methods".

    客户端只宣告账号密码（0x02）时用凭据认证；同时宣告匿名（0x00）时保持匿名轮询，
    与 8082 原有的“带凭据即固定出口、不带即随机出口”行为一致。
    """
    offers_user_pass = 2 in methods
    offers_anonymous = 0 in methods
    if offers_user_pass and not offers_anonymous:
        return 2
    if offers_anonymous:
        return 0
    return 2 if offers_user_pass else None


def _socks5_read_auth(client):
    """Read an RFC 1929 username/password request; returns (user, pass) or None."""
    header = _recv_exact(client, 2)
    if not header or header[0] != 1:
        return None
    username = _recv_exact(client, header[1])
    if username is None:
        return None
    length = _recv_exact(client, 1)
    if not length:
        return None
    password = _recv_exact(client, length[0])
    if password is None:
        return None
    return (username.decode("utf-8", "replace"), password.decode("utf-8", "replace"))


def _socks5_auth_reply(client, ok):
    try:
        client.sendall(b"\x01\x00" if ok else b"\x01\x01")
    except OSError:
        pass


def _socks5_handshake(client, first=b"\x05", validate=None):
    """Negotiate SOCKS5 and return ``(host, port, credentials)``.

    Credentials are ``(username, password)`` when RFC 1929 authentication was
    used, otherwise None. ``validate`` is an optional ``(user, password) -> bool``
    callable invoked before the authentication reply is written.
    """
    if first != b"\x05":
        return None
    count = _recv_exact(client, 1)
    if not count:
        return None
    methods = _recv_exact(client, count[0])
    if methods is None:
        return None
    choice = _socks5_choose_method(methods)
    if choice is None:
        try:
            client.sendall(b"\x05\xff")
        except OSError:
            pass
        return None
    client.sendall(bytes([5, choice]))
    credentials = None
    if choice == 2:
        credentials = _socks5_read_auth(client)
        if not credentials or (validate is not None and not validate(*credentials)):
            _socks5_auth_reply(client, False)
            return None
        _socks5_auth_reply(client, True)
    header = _recv_exact(client, 4)
    if not header or header[0] != 5 or header[1] != 1:
        return None
    atyp = header[3]
    if atyp == 1:
        raw = _recv_exact(client, 4)
        host = socket.inet_ntoa(raw) if raw else None
    elif atyp == 3:
        length = _recv_exact(client, 1)
        raw = _recv_exact(client, length[0]) if length else None
        host = raw.decode("idna") if raw else None
    elif atyp == 4:
        raw = _recv_exact(client, 16)
        host = socket.inet_ntop(socket.AF_INET6, raw) if raw else None
    else:
        return None
    port_raw = _recv_exact(client, 2)
    if not host or port_raw is None:
        return None
    return host, int.from_bytes(port_raw, "big"), credentials


def parse_bool_query_param(values):
    """Return True/False for an optional boolean query parameter, None if invalid.

    An absent or blank parameter is False, so callers that omit it keep the
    unfiltered behaviour. Unrecognized values return None so the caller can
    reject them instead of silently dropping the requested filter.
    """
    value = str((values or [""])[0]).strip().lower()
    if value in _FLAG_TRUE:
        return True
    if value in _FLAG_FALSE:
        return False
    return None


def export_nodes(sync, tls_only=False):
    """Active nodes usable for an export, filtered to the served revision."""
    store = getattr(sync, "store", None)
    if store is None:
        return []
    nodes = store.active(tls_required=tls_only)
    supervisor = getattr(sync, "supervisor", None)
    if not supervisor:
        return nodes
    endpoint = supervisor.endpoint()
    if not endpoint:
        return []
    revision = endpoint[2]
    served = [node for node in nodes if node.config_revision == revision]
    # 同步切换途中可能出现“节点已写新 revision、正式实例仍是旧 revision”，
    # 此时回落到全部可用节点，避免订阅方把整池误判为失效。
    return served or nodes


def export_proxy_port(config):
    """导出链接端口恒为监听端口（PROXY_PORT，默认 8082）。"""
    if config is not None:
        try:
            return int(config.get("port", 8082) or 8082)
        except (TypeError, ValueError):
            return 8082
    return 8082


def export_endpoint_links(nodes, scheme="http", host="127.0.0.1", port=8082):
    """One ``scheme://user:pass@host:port`` entry per node.

    The credentials are that node's sing-box inbound credentials, so a client
    dialling the 8082 entry with them is pinned to that single exit.
    """
    authority = "[{}]".format(host) if ":" in host and not host.startswith("[") else host
    links = []
    for node in nodes:
        username = str(node.inbound_username or "")
        password = str(node.inbound_password or "")
        if not username or not password:
            continue
        links.append("{}://{}:{}@{}:{}".format(
            scheme,
            urllib.parse.quote(username, safe=""),
            urllib.parse.quote(password, safe=""),
            authority, port,
        ))
    return links


def _socks_ok(client):
    client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")


def _socks_fail(client):
    try:
        client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
    except OSError:
        pass


class ProxyServer:
    def __init__(self, pool, supervisor, logger, config, timeout=PROXY_TIMEOUT):
        self.pool = pool
        self.supervisor = supervisor
        self.logger = logger
        self.config = config
        self.timeout = timeout
        self.semaphore = threading.BoundedSemaphore(config.get("max_clients"))

    def select_node(self, tls_required=False):
        return self.pool.route(tls_required=tls_required)

    def credential_node(self, username, password):
        """Resolve client-supplied credentials to one exit, else None."""
        lookup = getattr(self.pool, "by_credentials", None)
        if lookup is None:
            return None
        return lookup(username, password)

    def endpoint(self):
        return self.supervisor.endpoint() if self.supervisor else None

    def reject_http(self, client):
        client.sendall(
            b"HTTP/1.1 407 Proxy Authentication Required\r\n"
            b"Proxy-Authenticate: Basic realm=\"proxy_pool\"\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n\r\n"
        )

    def serve_one(self, client):
        client.settimeout(self.timeout)
        first = client.recv(1)
        if not first:
            return
        if not self.supervisor.endpoint():
            if first == b"\x05":
                _socks_fail(client)
            else:
                client.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n")
            return
        if first == b"\x05":
            destination = _socks5_handshake(
                client, first,
                validate=lambda user, secret: self.credential_node(user, secret) is not None,
            )
            if not destination:
                return
            dest_host, dest_port, credentials = destination
            node = self.credential_node(*credentials) if credentials else None
            if credentials and node is None:
                self.logger.warn("8082 SOCKS5 认证失败：凭据不匹配任何活动节点")
                _socks_fail(client)
                return
            if node is not None:
                endpoint = self.endpoint()
            else:
                endpoint, node = self.select_node(tls_required=dest_port == 443)
            if not endpoint or not node:
                _socks_fail(client)
                return
            host, port, _ = endpoint
            try:
                upstream = connect_via_proxy(
                    "socks5", host, port, dest_host, dest_port,
                    timeout=self.timeout,
                    proxy_username=node.inbound_username,
                    proxy_password=node.inbound_password,
                )
                self.pool.success(node)
                _socks_ok(client)
                _relay(client, upstream)
            except Exception as exc:
                self.pool.failure(node)
                self.logger.warn("节点 {} 连接失败：{}".format(node.node_id[:12], exc))
            return
        header = RequestHeader(first + _recv_until(client))
        if not header.target:
            return
        dest_host, dest_port = header.host_port
        credentials = header.proxy_credentials()
        node = self.credential_node(*credentials) if credentials else None
        if credentials and node is None:
            self.logger.warn("8082 HTTP 认证失败：凭据不匹配任何活动节点")
            self.reject_http(client)
            return
        if node is not None:
            endpoint = self.endpoint()
        else:
            endpoint, node = self.select_node(tls_required=header.is_connect)
        if not endpoint or not node:
            client.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n")
            return
        host, port, _ = endpoint
        try:
            if header.is_connect:
                upstream = connect_via_proxy(
                    "http", host, port, dest_host, dest_port,
                    timeout=self.timeout,
                    proxy_username=node.inbound_username,
                    proxy_password=node.inbound_password,
                )
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                upstream = connect_to_proxy(host, port, self.timeout)
                upstream.sendall(header.to_upstream(node.inbound_username, node.inbound_password))
            self.pool.success(node)
            _relay(client, upstream)
        except Exception as exc:
            self.pool.failure(node)
            self.logger.warn("节点 {} 连接失败：{}".format(node.node_id[:12], exc))
            if header.is_connect:
                try:
                    client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass

    def run(self, listen, port, stop_event):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((listen, port))
        listener.listen(self.config.get("max_clients"))
        listener.setblocking(False)
        self.logger.info("8082 代理入口监听 {}:{}".format(listen, port))
        try:
            while not stop_event.is_set():
                readable, _, _ = select.select([listener], [], [], 0.5)
                if listener not in readable:
                    continue
                client, _ = listener.accept()
                if not self.semaphore.acquire(blocking=False):
                    client.close()
                    continue

                def worker(connection):
                    try:
                        self.serve_one(connection)
                    except Exception as exc:
                        self.logger.warn("客户端连接失败：{}".format(exc))
                    finally:
                        try:
                            connection.close()
                        except OSError:
                            pass
                        self.semaphore.release()

                threading.Thread(target=worker, args=(client,), daemon=True).start()
        finally:
            listener.close()


def start_control_server(logger, pool, sync, config, stop_event, auth):
    class ControlHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def send_json(self, payload, code=200, headers=None):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def send_text(self, text, code=200):
            body = str(text or "").encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def redirect(self, location):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def require_auth(self, page=False, touch=False):
            session = auth.authenticate(self.headers.get("Cookie"), touch=touch)
            if session:
                return session
            if page:
                self.redirect("/login.html")
            else:
                self.send_json({"error": "unauthorized"}, 401)
            return ""

        def body(self):
            length = int(self.headers.get("Content-Length", "0") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self.send_json({"ok": True})
                return
            if path in ("/login", "/login.html"):
                if auth.authenticate(self.headers.get("Cookie")):
                    self.redirect("/")
                else:
                    self.send_file("login.html")
                return
            if path in ("/", "/index.html"):
                if not self.require_auth(page=True, touch=True):
                    return
                self.send_file("index.html")
                return
            if path == "/pool.html":
                if not self.require_auth(page=True, touch=True):
                    return
                self.send_file("pool.html")
                return
            if path == "/api/proxies":
                # 只读节点订阅导出：默认导出带账号密码的 8082 出口链接
                # （一组账号密码 = 一条出口），format=share 时导出分享链接。
                # 鉴权用查询参数 token，调用方无需自定义请求头。
                query = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                token = (query.get("token") or [""])[0]
                if not auth.check_token(token):
                    self.send_json({"error": "unauthorized"}, 401)
                    return
                tls_only = parse_bool_query_param(query.get("tls_only"))
                if tls_only is None:
                    self.send_json({"error": "invalid tls_only value"}, 400)
                    return
                export_format = str((query.get("format") or ["endpoint"])[0]).strip().lower() or "endpoint"
                if export_format not in ("endpoint", "share"):
                    self.send_json({"error": "invalid format value"}, 400)
                    return
                nodes = export_nodes(sync, tls_only)
                if export_format == "share":
                    lines, skipped = export_share_links(nodes)
                    for node_id, reason in skipped:
                        logger.warn("节点 {} 无法导出为分享链接：{}".format(node_id[:12], reason))
                else:
                    lines = export_endpoint_links(
                        nodes,
                        scheme=EXPORT_PROXY_SCHEME,
                        host=EXPORT_PROXY_HOST,
                        port=export_proxy_port(config),
                    )
                self.send_text("\n".join(lines) + "\n" if lines else "")
                return
            if not self.require_auth():
                return
            if path in ("/stats", "/config"):
                data = sync.snapshot()
                data.update(config.get_all())
                self.send_json(data)
                return
            if path == "/pool":
                nodes = []
                for node in sorted(sync.store.all(), key=lambda item: item.node_id):
                    nodes.append({
                        "node_id": node.node_id,
                        "proxy": node.proxy,
                        "protocol": node.protocol,
                        "tls": bool(node.tls),
                        "synced": bool(node.synced),
                        "source": node.source,
                        "check_count": node.check_count,
                        "last_status": bool(node.last_status),
                        "last_time": node.last_time,
                    })
                self.send_json({"total": len(nodes), "items": nodes})
                return
            if path == "/logs":
                try:
                    since = int(self.path.split("since=", 1)[1]) if "since=" in self.path else 0
                except ValueError:
                    since = 0
                self.send_json({"logs": logger.snapshot(since)})
                return
            if path == "/task":
                self.send_json(sync.snapshot())
                return
            self.send_json({"error": "not found"}, 404)

        def send_file(self, name):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", name)
            try:
                with open(path, "rb") as stream:
                    body = stream.read()
            except OSError:
                self.send_json({"error": "file not found"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/auth/login":
                session = auth.login(self.body().get("access_token"))
                if not session:
                    self.send_json({"error": "invalid access token"}, 401)
                    return
                self.send_json(
                    {"ok": True},
                    headers={"Set-Cookie": auth.session_cookie(session)},
                )
                return
            if not self.require_auth(touch=True):
                return
            if path == "/auth/logout":
                auth.logout(self.headers.get("Cookie"))
                self.send_json(
                    {"ok": True},
                    headers={"Set-Cookie": auth.expired_cookie()},
                )
                return
            if path == "/auth/touch":
                self.send_json({"ok": True})
                return
            if path == "/fetch":
                if not sync.start_fetch_async():
                    self.send_json({"error": "抓取任务正在运行"}, 409)
                else:
                    self.send_json(sync.snapshot(), 202)
                return
            if path == "/sync":
                if not sync.start_check_async():
                    self.send_json({"error": "同步任务正在运行或首次抓取尚未完成"}, 409)
                else:
                    self.send_json(sync.snapshot(), 202)
                return
            if path == "/pool/delete":
                node_id = str(self.body().get("node_id") or "")
                if not node_id:
                    self.send_json({"error": "缺少 node_id"}, 400)
                    return
                deleted = sync.store.delete(node_id)
                self.send_json({"deleted": deleted})
                return
            self.send_json({"error": "not found"}, 404)

        def do_PUT(self):
            if not self.require_auth(touch=True):
                return
            if self.path.split("?", 1)[0] != "/config":
                self.send_json({"error": "not found"}, 404)
                return
            integer_fields = {"port", "stats_port", "max_clients"}
            values = {}
            for key, value in self.body().items():
                if key in integer_fields:
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        self.send_json({"error": "{} 必须是整数".format(key)}, 400)
                        return
                    if value <= 0:
                        self.send_json({"error": "{} 必须大于 0".format(key)}, 400)
                        return
                values[key] = value
            config.update(values)
            self.send_json(sync.snapshot())

    server = HTTPServer((config.get("listen"), config.get("stats_port")), ControlHandler)
    thread = threading.Thread(target=server.serve_forever, name="web-control", daemon=True)
    thread.start()
    logger.info("Web UI 监听 {}:{}".format(config.get("listen"), config.get("stats_port")))
    return server


def main(args=None):
    parser = argparse.ArgumentParser(description="sing-box backed proxy pool")
    parser.add_argument("--listen", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--stats-port", type=int, default=None)
    parsed = parser.parse_args(args)
    config = Config()
    config.update({key: value for key, value in {
        "listen": parsed.listen, "port": parsed.port, "stats_port": parsed.stats_port,
    }.items() if value is not None})
    logger = Logger()
    store = NodeStore()
    sync = SyncManager(logger, store=store)
    pool = RuntimePool(store, sync.supervisor, threshold=FAIL_THRESHOLD)
    auth = WebAuth(WEBUI_ACCESS_TOKEN, WEBUI_SESSION_TIMEOUT_SECONDS)
    stop_event = threading.Event()
    control = start_control_server(logger, pool, sync, config, stop_event, auth)
    sync.start_scheduler()
    proxy_server = ProxyServer(pool, sync.supervisor, logger, config, timeout=PROXY_TIMEOUT)

    def stop(_signum=None, _frame=None):
        stop_event.set()
        sync.stop()
        control.shutdown()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        proxy_server.run(config.get("listen"), config.get("port"), stop_event)
    finally:
        stop()


if __name__ == "__main__":
    main()
