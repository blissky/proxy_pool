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
from collections import deque
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer

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

    def to_upstream(self, username, password):
        lines = []
        for line in self.data.split(b"\r\n"):
            if line.lower().startswith(b"proxy-authorization:"):
                continue
            lines.append(line)
        token = base64.b64encode((username + ":" + password).encode("utf-8")).decode("ascii")
        lines.insert(-1, "Proxy-Authorization: Basic {}".format(token).encode("ascii"))
        return b"\r\n".join(lines)


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


def _socks5_handshake(client, first=b"\x05"):
    if first != b"\x05":
        return None
    count = client.recv(1)
    if not count:
        return None
    client.recv(count[0])
    client.sendall(b"\x05\x00")
    header = client.recv(4)
    if len(header) != 4 or header[0] != 5 or header[1] != 1:
        return None
    atyp = header[3]
    if atyp == 1:
        host = socket.inet_ntoa(client.recv(4))
    elif atyp == 3:
        length = client.recv(1)[0]
        host = client.recv(length).decode("idna")
    elif atyp == 4:
        host = socket.inet_ntop(socket.AF_INET6, client.recv(16))
    else:
        return None
    port = int.from_bytes(client.recv(2), "big")
    return host, port


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
            destination = _socks5_handshake(client, first)
            if not destination:
                return
            dest_host, dest_port = destination
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
