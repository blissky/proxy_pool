"""Structured proxy node model used by Redis and sing-box integration."""

import hashlib
import json
import secrets
from copy import deepcopy
from urllib.parse import urlsplit


PROTOCOLS = ("http", "socks", "ss", "trojan", "vless", "vmess", "hysteria2")
LEGACY_TYPES = ("http", "socks")


def _bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _normal(value):
    if isinstance(value, dict):
        return {str(key): _normal(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normal(item) for item in value]
    return value


def node_fingerprint(protocol, outbound_config):
    """Return a stable identifier for all protocol-specific node parameters."""
    payload = json.dumps(
        {"protocol": protocol, "outbound": _normal(outbound_config)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ProxyNode:
    """A Redis-persisted node plus its local sing-box route credentials."""

    def __init__(self, proxy="", protocol="http", source="", outbound_config=None,
                 remote_username="", remote_password="", inbound_username="",
                 inbound_password="", tls=False, synced=False, config_revision="",
                 fail_count=0, check_count=0, last_status=False, last_time="",
                 region="", node_id="", anonymous=""):
        protocol = (protocol or "http").lower()
        if protocol not in PROTOCOLS:
            raise ValueError("unsupported node protocol: {}".format(protocol))
        self.proxy = proxy or self._proxy_from_config(outbound_config or {})
        self.protocol = protocol
        self.source = source or ""
        self.outbound_config = deepcopy(outbound_config or {})
        self.remote_username = remote_username or ""
        self.remote_password = remote_password or ""
        self.inbound_username = inbound_username or self._new_credential("u")
        self.inbound_password = inbound_password or self._new_credential("p")
        self.tls = _bool(tls)
        self.synced = _bool(synced)
        self.config_revision = config_revision or ""
        self.fail_count = int(fail_count or 0)
        self.check_count = int(check_count or 0)
        self.last_status = _bool(last_status)
        self.last_time = last_time or ""
        self.region = region or ""
        self.anonymous = anonymous or ""
        self.node_id = node_id or node_fingerprint(self.protocol, self.outbound_config)

    @staticmethod
    def _new_credential(prefix):
        return "{}-{}".format(prefix, secrets.token_urlsafe(18))

    @staticmethod
    def _proxy_from_config(config):
        server = config.get("server", "")
        port = config.get("server_port", "")
        return "{}:{}".format(server, port) if server and port else ""

    @classmethod
    def from_dict(cls, value):
        value = dict(value or {})
        protocol = value.get("protocol") or value.get("type") or "http"
        outbound = value.get("outbound_config") or {}
        if isinstance(outbound, str):
            try:
                outbound = json.loads(outbound)
            except (TypeError, ValueError):
                outbound = {}
        # Existing records store the endpoint as proxy and do not have a
        # sing-box config yet. Keep them usable as legacy HTTP/SOCKS nodes.
        if not outbound and value.get("proxy"):
            host, separator, port = value["proxy"].rpartition(":")
            if separator and port.isdigit():
                outbound = {
                    "type": protocol,
                    "server": host,
                    "server_port": int(port),
                }
        return cls(
            proxy=value.get("proxy", ""), protocol=protocol,
            source=value.get("source", ""), outbound_config=outbound,
            remote_username=value.get("remote_username", value.get("username", "")),
            remote_password=value.get("remote_password", value.get("password", "")),
            inbound_username=value.get("inbound_username", ""),
            inbound_password=value.get("inbound_password", ""),
            tls=value.get("tls", value.get("https", False)),
            synced=value.get("synced", False), config_revision=value.get("config_revision", ""),
            fail_count=value.get("fail_count", 0), check_count=value.get("check_count", 0),
            last_status=value.get("last_status", False), last_time=value.get("last_time", ""),
            region=value.get("region", ""), node_id=value.get("node_id", ""),
            anonymous=value.get("anonymous", ""),
        )

    @classmethod
    def from_endpoint(cls, value, source="", protocol="http"):
        raw = (value or "").strip()
        if not raw:
            raise ValueError("empty proxy endpoint")
        parse_value = raw if "://" in raw else "//" + raw
        parsed = urlsplit(parse_value)
        if not parsed.hostname or parsed.port is None:
            raise ValueError("invalid proxy endpoint: {}".format(value))
        protocol = (protocol or parsed.scheme or "http").lower()
        if protocol not in LEGACY_TYPES:
            raise ValueError("legacy endpoint only supports http or socks")
        config = {
            "type": protocol,
            "server": parsed.hostname,
            "server_port": parsed.port,
        }
        return cls(
            proxy="{}:{}".format(parsed.hostname, parsed.port),
            protocol=protocol,
            source=source,
            outbound_config=config,
            remote_username=parsed.username or "",
            remote_password=parsed.password or "",
        )

    @classmethod
    def from_json(cls, value):
        return cls.from_dict(json.loads(value))

    createFromJson = from_json

    @property
    def proxy_type(self):
        """Compatibility view for the public HTTP/SOCKS distinction."""
        return "socks" if self.protocol == "socks" else "http"

    @proxy_type.setter
    def proxy_type(self, value):
        value = (value or "http").lower()
        if value not in LEGACY_TYPES:
            raise ValueError("proxy_type only accepts http or socks")
        self.protocol = value

    @property
    def https(self):
        """Compatibility alias for the TLS capability flag."""
        return self.tls

    @https.setter
    def https(self, value):
        self.tls = _bool(value)

    def add_source(self, source):
        values = [item for item in self.source.split("/") if item]
        if source and source not in values:
            values.append(source)
        self.source = "/".join(sorted(set(values)))

    @property
    def to_dict(self):
        return {
            "node_id": self.node_id,
            "proxy": self.proxy,
            "protocol": self.protocol,
            "type": self.proxy_type,
            "source": self.source,
            "outbound_config": deepcopy(self.outbound_config),
            "remote_username": self.remote_username,
            "remote_password": self.remote_password,
            "inbound_username": self.inbound_username,
            "inbound_password": self.inbound_password,
            "tls": bool(self.tls),
            "synced": bool(self.synced),
            "config_revision": self.config_revision,
            "fail_count": self.fail_count,
            "check_count": self.check_count,
            "last_status": bool(self.last_status),
            "last_time": self.last_time,
            "region": self.region,
            "anonymous": self.anonymous,
        }

    @property
    def to_json(self):
        return json.dumps(self.to_dict, ensure_ascii=False, separators=(",", ":"))
