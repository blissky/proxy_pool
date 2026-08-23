"""sing-box configuration, temporary checks and blue-green supervision."""

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from urllib.parse import urlsplit

from front_proxy import resolve_front_proxy
from proxy_chain import parse_proxy, request_via_proxy


ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def find_free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _front_outbound(value):
    if not value:
        return None
    scheme, host, port, username, password = parse_proxy(value)
    if scheme in ("http", "https"):
        result = {
            "type": "http", "tag": "front-proxy", "server": host,
            "server_port": port,
        }
        if username is not None:
            result["username"] = username
            result["password"] = password or ""
        if scheme == "https":
            result["tls"] = {"enabled": True, "server_name": host}
        return result
    if scheme in ("socks4", "socks4a", "socks5", "socks5h"):
        result = {
            "type": "socks", "tag": "front-proxy", "server": host,
            "server_port": port, "version": "4" if scheme.startswith("socks4") else "5",
        }
        if username is not None:
            result["username"] = username
            result["password"] = password or ""
        return result
    raise ValueError("unsupported front proxy type: {}".format(scheme))


def _node_outbound(node):
    config = deepcopy(node.outbound_config)
    protocol = config.get("type", node.protocol)
    if protocol == "ss":
        protocol = "shadowsocks"
    config["type"] = protocol
    config["tag"] = "node-{}".format(node.node_id[:20])
    if node.protocol in ("http", "socks"):
        config["server"] = node.outbound_config.get("server") or node.proxy.rsplit(":", 1)[0]
        config["server_port"] = int(node.outbound_config.get("server_port") or node.proxy.rsplit(":", 1)[1])
        if node.remote_username:
            config["username"] = node.remote_username
            config["password"] = node.remote_password
    return config


def build_config(nodes, listen_port, front_proxy="", log_level="error"):
    """Build one mixed-inbound config for a set of already parsed nodes."""
    front = _front_outbound(resolve_front_proxy(front_proxy)) if resolve_front_proxy(front_proxy) else None
    users = []
    outbounds = []
    rules = []
    if front:
        outbounds.append(front)
    for node in nodes:
        tag = "node-{}".format(node.node_id[:20])
        users.append({"username": node.inbound_username, "password": node.inbound_password})
        outbound = _node_outbound(node)
        if front:
            outbound["detour"] = "front-proxy"
        outbounds.append(outbound)
        rules.append({
            "inbound": ["mixed-in"],
            "auth_user": [node.inbound_username],
            "action": "route",
            "outbound": tag,
        })
    outbounds.append({"type": "block", "tag": "block"})
    return {
        "$schema": "https://sing-box.sagernet.org/schema.json",
        "log": {"level": log_level},
        "inbounds": [{
            "type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1",
            "listen_port": int(listen_port), "users": users,
        }],
        "outbounds": outbounds,
        "route": {"rules": rules, "final": "block"},
    }


class RunningInstance:
    def __init__(self, process, port, config_path, revision, temporary=False):
        self.process = process
        self.port = port
        self.config_path = config_path
        self.revision = revision
        self.temporary = temporary
        self.started_at = time.time()

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None


class SingBoxRunner:
    def __init__(self, binary="sing-box", runtime_dir="/tmp/proxy-pool-singbox"):
        self.binary = binary
        self.runtime_dir = runtime_dir
        os.makedirs(self.runtime_dir, exist_ok=True)

    def write_config(self, config, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        directory = os.path.dirname(path)
        fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(config, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def check(self, path):
        try:
            result = subprocess.run(
                [self.binary, "check", "-c", path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=20, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("sing-box check failed: {}".format(exc)) from exc
        if result.returncode:
            output = ANSI_ESCAPE.sub("", result.stdout or "").strip()
            raise RuntimeError("sing-box check failed: {}".format(output))

    def start(self, config_path, port, revision, temporary=False):
        try:
            process = subprocess.Popen(
                [self.binary, "run", "-c", config_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise RuntimeError("sing-box start failed: {}".format(exc)) from exc
        instance = RunningInstance(process, port, config_path, revision, temporary)
        try:
            self.wait_ready(instance, timeout=20)
        except Exception:
            self.stop(instance)
            raise
        return instance

    @staticmethod
    def wait_ready(instance, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not instance.alive:
                raise RuntimeError("sing-box exited before mixed port became ready")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.3)
            try:
                if sock.connect_ex(("127.0.0.1", instance.port)) == 0:
                    return
            finally:
                sock.close()
            time.sleep(0.1)
        raise RuntimeError("sing-box mixed port is not ready")

    @staticmethod
    def stop(instance):
        if not instance or not instance.process:
            return
        process = instance.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


class NodeDetector:
    """Run one temporary sing-box process per node with bounded concurrency."""

    def __init__(self, binary="sing-box", runtime_dir="/tmp/proxy-pool-singbox",
                 concurrency=4, http_url="http://httpbin.org", https_url="https://www.qq.com",
                 timeout=10, front_proxy=""):
        self.runner = SingBoxRunner(binary, os.path.join(runtime_dir, "checks"))
        self.concurrency = max(1, int(concurrency))
        self.http_url = http_url
        self.https_url = https_url
        self.timeout = timeout
        self.front_proxy = front_proxy

    def detect(self, nodes, callback=None):
        # _port is set immediately before each probe; each detector owns one
        # process, so this is thread-local through the worker call.
        results = []
        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            futures = {}
            for node in nodes:
                futures[executor.submit(self._detect_one_with_port, node)] = node
            for future in as_completed(futures):
                node, error = future.result()
                results.append(node)
                if callback:
                    callback(node, error, len(results), len(nodes))
        return results

    def _detect_one_with_port(self, node):
        return self._detect_one_at_port(node, find_free_port())

    def _detect_one_at_port(self, node, port):
        path = os.path.join(self.runner.runtime_dir, "{}.json".format(uuid.uuid4().hex))
        revision = "check-{}".format(uuid.uuid4().hex[:12])
        instance = None
        try:
            self.runner.write_config(build_config([node], port, self.front_proxy), path)
            self.runner.check(path)
            instance = self.runner.start(path, port, revision, temporary=True)
            # HTTP controls admission; HTTPS only records TLS capability.
            self._probe_at_port(node, self.http_url, port)
            try:
                self._probe_at_port(node, self.https_url, port)
                tls_ok = True
            except Exception:
                tls_ok = False
            node.check_count += 1
            node.last_status = True
            node.tls = tls_ok
            node.synced = False
            node.config_revision = ""
            node.last_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            node.fail_count = max(0, node.fail_count - 1)
            return node, None
        except Exception as exc:
            node.check_count += 1
            node.last_status = False
            node.tls = False
            node.synced = False
            node.config_revision = ""
            node.fail_count += 1
            node.last_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            return node, str(exc)
        finally:
            self.runner.stop(instance)
            try:
                os.unlink(path)
            except OSError:
                pass

    def _probe_at_port(self, node, url, port):
        status, _ = request_via_proxy(
            "http", "127.0.0.1", port, url,
            timeout=self.timeout, proxy_username=node.inbound_username,
            proxy_password=node.inbound_password, method="HEAD",
        )
        if not (200 <= status < 400 or status == 403):
            raise RuntimeError("{} returned HTTP status {}".format(url, status))
        return status


class SingBoxSupervisor:
    """Own the active formal instance and perform blue-green activation."""

    def __init__(self, binary="sing-box", runtime_dir="/tmp/proxy-pool-singbox",
                 front_proxy=""):
        self.runner = SingBoxRunner(binary, os.path.join(runtime_dir, "formal"))
        self.runtime_dir = runtime_dir
        self.front_proxy = front_proxy
        self.lock = threading.RLock()
        self.active = None
        self.last_error = ""
        self.last_started = 0

    def activate(self, nodes, commit):
        revision = "formal-{}".format(uuid.uuid4().hex[:16])
        port = find_free_port()
        path = os.path.join(self.runner.runtime_dir, "{}.json".format(revision))
        new_instance = None
        with self.lock:
            try:
                self.runner.write_config(build_config(nodes, port, self.front_proxy), path)
                self.runner.check(path)
                new_instance = self.runner.start(path, port, revision)
                old_instance = self.active
                self.active = new_instance
                try:
                    commit(nodes, revision)
                except Exception:
                    self.active = old_instance
                    raise
                self.last_started = time.time()
                self.last_error = ""
                if old_instance:
                    self.runner.stop(old_instance)
                    try:
                        os.unlink(old_instance.config_path)
                    except OSError:
                        pass
                return new_instance
            except Exception as exc:
                self.last_error = str(exc)
                self.runner.stop(new_instance)
                try:
                    os.unlink(path)
                except OSError:
                    pass
                raise

    def stop(self):
        with self.lock:
            active = self.active
            self.active = None
        self.runner.stop(active)

    def endpoint(self):
        with self.lock:
            if not self.active or not self.active.alive:
                return None
            return "127.0.0.1", self.active.port, self.active.revision

    def status(self):
        with self.lock:
            active = self.active
            return {
                "running": bool(active and active.alive),
                "port": active.port if active else 0,
                "revision": active.revision if active else "",
                "started": self.last_started,
                "error": self.last_error,
            }
