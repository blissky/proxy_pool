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

# 以下取值对齐 Dockerfile 固定的 sing-box 1.13.2（源码 + 文档 + 二进制实测）。
# 升级 SING_BOX_VERSION 时需要重新核对。
SS_METHODS = {
    "none", "aes-128-gcm", "aes-192-gcm", "aes-256-gcm",
    "chacha20-ietf-poly1305", "xchacha20-ietf-poly1305",
    "aes-128-ctr", "aes-192-ctr", "aes-256-ctr",
    "aes-128-cfb", "aes-192-cfb", "aes-256-cfb",
    "rc4-md5", "chacha20-ietf", "xchacha20",
}
SS2022_KEY_SIZES = {
    "2022-blake3-aes-128-gcm": 16,
    "2022-blake3-aes-256-gcm": 32,
    "2022-blake3-chacha20-poly1305": 32,
}
# 文档列出 aes-128-ctr，但 1.13.2 二进制拒绝；aes-128-cfb 文档未列但可用。
VMESS_SECURITIES = {"", "auto", "none", "zero", "aes-128-gcm", "chacha20-poly1305", "aes-128-cfb"}
VLESS_FLOWS = {"", "xtls-rprx-vision"}
SINGBOX_UTLS_FINGERPRINTS = UTLS_FINGERPRINTS | {
    "", "chrome_psk", "chrome_psk_shuffle", "chrome_padding_psk_shuffle", "chrome_pq", "chrome_pq_psk",
}
_STD_BASE64 = re.compile(r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$")
# reality public_key 用 RawURLEncoding 解码且必须是 32 字节，即恰好 43 个字符。
_REALITY_PUBLIC_KEY = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SHORT_ID = re.compile(r"^(?:[0-9A-Fa-f]{2}){0,8}$")
_ATOI = re.compile(r"^[+-]?[0-9]+$")


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


def _strip_newlines(value):
    """Go's base64 decoders silently skip CR and LF."""
    return value.replace("\r", "").replace("\n", "")


def _plugin_options(value):
    """Port of sing-box ``sip003.ParsePluginOptions``; first value per key wins."""
    options, index = {}, 0

    def read(stop):
        nonlocal index
        chars = []
        while index < len(value) and value[index] not in stop:
            if value[index] == "\\":
                index += 1
                if index >= len(value):
                    raise ValueError("invalid ss plugin options")
            chars.append(value[index])
            index += 1
        return "".join(chars)

    while index < len(value):
        key = read("=;")
        if not key:
            raise ValueError("invalid ss plugin options")
        if index >= len(value) or value[index] != "=":
            options.setdefault(key, "1")
        else:
            index += 1
            options.setdefault(key, read(";"))
        index += 1
    return options


def _check_ss(config):
    method = config.get("method") or ""
    password = config.get("password") or ""
    if method in SS2022_KEY_SIZES:
        for key in password.split(":"):
            key = _strip_newlines(key)
            if not _STD_BASE64.fullmatch(key) or len(base64.b64decode(key)) != SS2022_KEY_SIZES[method]:
                raise ValueError("invalid ss 2022 key")
    elif method not in SS_METHODS:
        raise ValueError("unsupported ss method")
    if not password and method != "none":
        raise ValueError("missing ss password")
    plugin = config.get("plugin") or ""
    if not plugin:
        return
    options = _plugin_options(config.get("plugin_opts") or "")
    if plugin == "obfs-local":
        if options.get("obfs", "http") not in ("http", "tls"):
            raise ValueError("unsupported ss obfs mode")
    elif plugin == "v2ray-plugin":
        mode = options.get("mode", "websocket")
        if mode == "websocket":
            if "mux" in options and not _ATOI.fullmatch(options["mux"]):
                raise ValueError("invalid v2ray-plugin mux")
        elif mode != "quic" or "tls" not in options:
            raise ValueError("unsupported v2ray-plugin mode")
    else:
        raise ValueError("unsupported ss plugin")


def _check_tls(tls):
    utls = tls.get("utls") or {}
    if tls.get("enabled") and utls.get("enabled") and utls.get("fingerprint", "") not in SINGBOX_UTLS_FINGERPRINTS:
        raise ValueError("unsupported utls fingerprint")
    reality = tls.get("reality") or {}
    if not (tls.get("enabled") and reality.get("enabled")):
        return
    if not (tls.get("utls") or {}).get("enabled"):
        raise ValueError("reality requires utls fingerprint")
    if not _REALITY_PUBLIC_KEY.fullmatch(_strip_newlines(reality.get("public_key") or "")):
        raise ValueError("invalid reality public key")
    if not _SHORT_ID.fullmatch(reality.get("short_id") or ""):
        raise ValueError("invalid reality short id")


def check_outbound(config):
    """Reject configs the pinned sing-box would refuse at ``sing-box check``.

    Messages never echo field values: malformed URIs may carry credentials.
    """
    kind = config.get("type")
    if kind == "shadowsocks":
        _check_ss(config)
    elif kind == "vmess" and (config.get("security") or "") not in VMESS_SECURITIES:
        raise ValueError("unsupported vmess security")
    elif kind == "vless" and (config.get("flow") or "") not in VLESS_FLOWS:
        raise ValueError("unsupported vless flow")
    elif kind == "hysteria2":
        if not (config.get("tls") or {}).get("enabled"):
            raise ValueError("hysteria2 requires tls")
        obfs = config.get("obfs")
        if obfs and (obfs.get("type") != "salamander" or not obfs.get("password")):
            raise ValueError("unsupported hysteria2 obfs")
    if (config.get("transport") or {}).get("type") == "quic" and not (config.get("tls") or {}).get("enabled"):
        raise ValueError("quic transport requires tls")
    _check_tls(config.get("tls") or {})


_STRICT_BASE64 = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}$")
_ENDPOINT_SCHEMES = {
    "http": "http",
    "socks": "socks",
    "socks5": "socks",
    "socks5h": "socks",
}


def _decode_pasted(text):
    """Return decoded text when the paste is one Base64 blob, else ``None``."""
    raw = "".join(text.split())
    if "://" in text or len(raw) % 4 == 1 or not _STRICT_BASE64.fullmatch(raw):
        return None
    try:
        base64.b64decode(raw + "=" * ((4 - len(raw) % 4) % 4), altchars=b"-_", validate=True)
        return decode_base64_text(raw)
    except (ValueError, binascii.Error):
        return None


def _parse_pasted_line(line, source):
    scheme, separator, _ = line.partition("://")
    scheme = scheme.lower() if separator else ""
    if scheme in SUPPORTED_SCHEMES:
        return parse_node_uri(line, source)
    if not separator:
        return ProxyNode.from_endpoint(line, source, "http")
    if scheme in _ENDPOINT_SCHEMES:
        return ProxyNode.from_endpoint(line, source, _ENDPOINT_SCHEMES[scheme])
    raise ValueError("unsupported node scheme")


def parse_import_text(text, source="manual"):
    """Parse pasted nodes, either plain lines or one Base64 subscription.

    Accepts every type the fetchers produce: ss, trojan, vless, vmess and
    hysteria2 links, ``http://``/``socks5://`` endpoints and bare ``host:port``
    lines (treated as HTTP). Returns ``(nodes, errors)``; each error is
    ``(line_number, reason)`` and never repeats the line, which may hold secrets.
    """
    text = (text or "").lstrip("\ufeff")
    decoded = _decode_pasted(text)
    nodes = []
    errors = []
    for number, line in enumerate((text if decoded is None else decoded).splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            nodes.append(_parse_pasted_line(line, source))
        except Exception as exc:
            errors.append((number, str(exc).split(":", 1)[0] or type(exc).__name__))
    return nodes, errors


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
    check_outbound(node.outbound_config)
    node.source = source or node.source
    return node
