"""Parse proxy URIs and Base64 subscriptions into :class:`ProxyNode`."""

import base64
import binascii
import json
import re
from urllib.parse import parse_qs, unquote, unquote_plus, urlsplit

from .node import ProxyNode


SUPPORTED_SCHEMES = ("ss", "shadowsocks", "trojan", "vless", "vmess", "hysteria2")
URI_PATTERN = re.compile(r"(?:ss|shadowsocks|trojan|vless|vmess|hysteria2)://[^\s<>'\"]+")
UTLS_FINGERPRINTS = {
    "chrome", "firefox", "edge", "safari", "360", "qq", "ios", "android",
    "random", "randomized",
}


def decode_base64_text(value):
    """Decode standard or URL-safe Base64 text with optional missing padding."""
    raw = "".join((value or "").strip().split())
    if not raw:
        return ""
    padding = "=" * ((4 - len(raw) % 4) % 4)
    try:
        decoded = base64.b64decode(raw + padding, altchars=b"-_")
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid base64 subscription") from exc
    try:
        return decoded.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("base64 subscription is not UTF-8") from exc


def extract_uris(text):
    """Extract supported proxy links from a decoded subscription."""
    return [match.rstrip(".,;)") for match in URI_PATTERN.findall(text or "")]


def _port(value):
    try:
        number = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid node port") from exc
    if not 1 <= number <= 65535:
        raise ValueError("node port out of range")
    return number


def _query(uri):
    return {key: values[-1] for key, values in parse_qs(uri.query, keep_blank_values=True).items()}


def _tls_config(params, default=True):
    security = str(params.get("security") or "").lower()
    enabled = default if security in ("", "tls", "reality") else security != "none"
    config = {"enabled": enabled}
    server_name = params.get("sni") or params.get("serverName")
    if server_name:
        config["server_name"] = server_name
    fingerprint = str(params.get("fp") or params.get("fingerprint") or "").lower()
    if fingerprint in UTLS_FINGERPRINTS:
        config["utls"] = {"enabled": True, "fingerprint": fingerprint}
    public_key = params.get("pbk")
    if security == "reality" and public_key:
        config["reality"] = {
            "enabled": True,
            "public_key": public_key,
            "short_id": params.get("sid", ""),
        }
    if params.get("insecure") in ("1", "true", "yes"):
        config["insecure"] = True
    if params.get("allowInsecure") in ("1", "true", "yes"):
        config["insecure"] = True
    if params.get("alpn"):
        config["alpn"] = [item for item in params["alpn"].split(",") if item]
    return config


def _transport(params, network="tcp"):
    network = (network or "tcp").lower()
    if network in ("tcp", "raw", "none"):
        return {}
    if network == "quic":
        return {"transport": {"type": "quic"}}
    if network in ("ws", "websocket"):
        transport = {"type": "ws"}
        if params.get("path"):
            transport["path"] = params["path"]
        if params.get("host"):
            transport["headers"] = {"Host": params["host"]}
        if str(params.get("ed", "")).isdigit():
            transport["max_early_data"] = int(params["ed"])
            transport["early_data_header_name"] = params.get("eh", "Sec-WebSocket-Protocol")
        return {"transport": transport}
    if network in ("grpc", "gun"):
        transport = {"type": "grpc"}
        if params.get("serviceName"):
            transport["service_name"] = params["serviceName"]
        return {"transport": transport}
    if network == "httpupgrade":
        transport = {"type": "httpupgrade"}
        if params.get("path"):
            transport["path"] = params["path"]
        if params.get("host"):
            transport["host"] = params["host"]
        return {"transport": transport}
    if network in ("http", "h2"):
        transport = {"type": "http"}
        if params.get("path"):
            transport["path"] = params["path"]
        if params.get("host"):
            transport["host"] = [params["host"]]
        return {"transport": transport}
    raise ValueError("unsupported transport: {}".format(network))


def _endpoint(config):
    return "{}:{}".format(config["server"], config["server_port"])


def _parse_ss(uri):
    parsed = urlsplit(uri)
    fragment = unquote(parsed.fragment or "")
    raw_user = parsed.username or ""
    raw_password = parsed.password or ""
    if not parsed.hostname:
        encoded = parsed.netloc.split("@", 1)[0]
        decoded = decode_base64_text(encoded)
        if "@" not in decoded:
            raise ValueError("invalid ss node")
        credentials, address = decoded.rsplit("@", 1)
        method, separator, password = credentials.partition(":")
        if not separator:
            raise ValueError("invalid ss credentials")
        host, separator, port = address.rpartition(":")
        if not separator:
            raise ValueError("invalid ss endpoint")
    else:
        host = parsed.hostname
        port = parsed.port
        credentials = unquote(raw_user)
        if credentials and ":" not in credentials:
            try:
                credentials = decode_base64_text(credentials)
            except ValueError:
                pass
        method, separator, password = credentials.partition(":")
        if not separator:
            method, password = raw_user, raw_password
    config = {
        "type": "shadowsocks",
        "server": host,
        "server_port": _port(port),
        "method": {
            "chacha20-poly1305": "chacha20-ietf-poly1305",
            "xchacha20-poly1305": "xchacha20-ietf-poly1305",
        }.get(unquote(method), unquote(method)),
        "password": unquote(password),
    }
    params = _query(parsed)
    plugin = params.get("plugin")
    if plugin:
        parts = plugin.split(";", 1)
        config["plugin"] = parts[0]
        if len(parts) == 2:
            config["plugin_opts"] = parts[1]
    return ProxyNode(proxy=_endpoint(config), protocol="ss", source=fragment,
                     outbound_config=config)


def _parse_trojan(uri):
    parsed = urlsplit(uri)
    if not parsed.hostname or not parsed.username:
        raise ValueError("invalid trojan node")
    params = _query(parsed)
    config = {
        "type": "trojan",
        "server": parsed.hostname,
        "server_port": _port(parsed.port),
        "password": unquote(parsed.username),
        "tls": _tls_config(params),
    }
    config.update(_transport(params, params.get("type", params.get("network", "tcp"))))
    return ProxyNode(proxy=_endpoint(config), protocol="trojan", source=unquote(parsed.fragment),
                     outbound_config=config)


def _parse_vless(uri):
    parsed = urlsplit(uri)
    if not parsed.hostname or not parsed.username:
        raise ValueError("invalid vless node")
    params = _query(parsed)
    network = params.get("type", params.get("network", "tcp"))
    flow = params.get("flow", "")
    if flow == "xtls-rprx-vision-udp443":
        flow = "xtls-rprx-vision"
    config = {
        "type": "vless",
        "server": parsed.hostname,
        "server_port": _port(parsed.port),
        "uuid": unquote(parsed.username),
        "flow": flow,
        "tls": _tls_config(params, default=params.get("security", "none") != "none"),
    }
    config.update(_transport(params, network))
    return ProxyNode(proxy=_endpoint(config), protocol="vless", source=unquote(parsed.fragment),
                     outbound_config=config)


def _parse_vmess(uri):
    encoded = uri.split("://", 1)[1].split("#", 1)[0]
    data = json.loads(decode_base64_text(encoded))
    host = data.get("add") or data.get("server")
    port = _port(data.get("port"))
    uuid = data.get("id") or data.get("uuid")
    if not host or not uuid:
        raise ValueError("invalid vmess node")
    params = {
        "security": data.get("tls", "") or data.get("security", "none"),
        "sni": data.get("sni", "") or data.get("host", ""),
        "path": data.get("path", ""),
        "host": data.get("host", ""),
        "serviceName": data.get("serviceName", ""),
        "fp": data.get("fp", ""),
        "alpn": data.get("alpn", ""),
        "allowInsecure": str(data.get("allowInsecure", "")),
        "ed": str(data.get("ed", "")),
        "eh": data.get("eh", ""),
    }
    network = data.get("net", "tcp")
    config = {
        "type": "vmess",
        "server": host,
        "server_port": port,
        "uuid": uuid,
        "security": data.get("scy", data.get("cipher", "auto")),
        "alter_id": int(data.get("aid", 0) or 0),
        "tls": _tls_config(params, default=bool(data.get("tls"))),
    }
    config.update(_transport(params, network))
    return ProxyNode(proxy=_endpoint(config), protocol="vmess", source=unquote(uri.split("#", 1)[1]) if "#" in uri else "",
                     outbound_config=config)


def _parse_hysteria2(uri):
    parsed = urlsplit(uri)
    if not parsed.hostname or not parsed.username:
        raise ValueError("invalid hysteria2 node")
    params = _query(parsed)
    config = {
        "type": "hysteria2",
        "server": parsed.hostname,
        "server_port": _port(parsed.port),
        "password": unquote(parsed.username),
        "tls": _tls_config(params),
    }
    if params.get("obfs"):
        config["obfs"] = {"type": params["obfs"], "password": params.get("obfs-password", "")}
    if params.get("upmbps"):
        config["up_mbps"] = int(params["upmbps"])
    if params.get("downmbps"):
        config["down_mbps"] = int(params["downmbps"])
    return ProxyNode(proxy=_endpoint(config), protocol="hysteria2", source=unquote(parsed.fragment),
                     outbound_config=config)


def parse_node_uri(uri, source=""):
    """Parse one supported URI and attach the fetcher source name."""
    uri = (uri or "").strip()
    scheme = uri.split(":", 1)[0].lower()
    if scheme not in SUPPORTED_SCHEMES:
        raise ValueError("unsupported node scheme: {}".format(scheme))
    parser = {
        "ss": _parse_ss,
        "shadowsocks": _parse_ss,
        "trojan": _parse_trojan,
        "vless": _parse_vless,
        "vmess": _parse_vmess,
        "hysteria2": _parse_hysteria2,
    }[scheme]
    node = parser(uri)
    node.source = source or node.source
    return node
