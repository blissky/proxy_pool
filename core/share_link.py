"""Serialize stored proxy nodes back into standard subscription share links.

The output dialect is the common v2rayN / Clash share-link format so that any
standard subscription client can consume it. Only remote node credentials from
``remote_username``/``remote_password`` and ``outbound_config`` are used; the
local sing-box mixed-inbound credentials are never part of a share link.
"""

import base64
import json
import urllib.parse

# Shadowsocks AEAD methods that portable share links can carry.
SS_AEAD_METHODS = {
    "aes-128-gcm",
    "aes-192-gcm",
    "aes-256-gcm",
    "chacha20-ietf-poly1305",
    "2022-blake3-aes-128-gcm",
    "2022-blake3-aes-256-gcm",
}

_SS_METHOD_ALIASES = {
    "chacha20-poly1305": "chacha20-ietf-poly1305",
    "xchacha20-poly1305": "xchacha20-ietf-poly1305",
}

_QUOTE_SAFE = "-._~"


class ShareLinkUnsupported(ValueError):
    """Raised when a node cannot be represented as a portable share link."""


def export_share_links(nodes):
    """Serialize ``nodes`` into ``(share_links, [(node_id, reason), ...])``."""
    links = []
    skipped = []
    for node in nodes or []:
        try:
            links.append(node_to_share_link(node))
        except ShareLinkUnsupported as exc:
            skipped.append((str(getattr(node, "node_id", "") or ""), str(exc)))
    return links, skipped


def node_to_share_link(node):
    """Return the share link for one node or raise :class:`ShareLinkUnsupported`."""
    protocol = str(getattr(node, "protocol", "") or "").strip().lower()
    emitter = _EMITTERS.get(protocol)
    if emitter is None:
        raise ShareLinkUnsupported("unsupported protocol: {}".format(protocol or "unknown"))
    return emitter(node)


def _config(node):
    config = getattr(node, "outbound_config", None)
    return dict(config) if isinstance(config, dict) else {}


def _endpoint(config):
    host = str(config.get("server") or "").strip()
    if not host:
        raise ShareLinkUnsupported("missing server host")
    if ":" in host and not host.startswith("["):
        host = "[{}]".format(host)
    try:
        port = int(config.get("server_port"))
    except (TypeError, ValueError):
        raise ShareLinkUnsupported("missing server port")
    if not 1 <= port <= 65535:
        raise ShareLinkUnsupported("server port out of range")
    return host, port


def _quote(value):
    return urllib.parse.quote(str(value or ""), safe=_QUOTE_SAFE)


def _quote_all(value):
    return urllib.parse.quote(str(value or ""), safe="")


def _fragment(node, config):
    label = str(getattr(node, "source", "") or "").strip()
    if not label:
        label = "{}:{}".format(config.get("server") or "", config.get("server_port") or "")
    return "#{}".format(_quote_all(label))


def _suffix(pairs, node, config):
    query = "&".join("{}={}".format(key, _quote_all(value)) for key, value in pairs if value != "")
    suffix = "?{}".format(query) if query else ""
    return "{}{}".format(suffix, _fragment(node, config))


def _tls_pairs(tls, default_enabled):
    """Build ordered ``security``/TLS query pairs for one TLS block."""
    tls = tls if isinstance(tls, dict) else {}
    enabled = bool(tls.get("enabled", default_enabled))
    reality = tls.get("reality") if isinstance(tls.get("reality"), dict) else {}
    if reality.get("enabled") and not str(reality.get("public_key") or ""):
        raise ShareLinkUnsupported("Reality node is missing the public key")
    if reality.get("enabled"):
        security = "reality"
    elif enabled:
        security = "tls"
    else:
        security = "none"
    pairs = [("security", security)]
    if security == "none":
        return pairs
    server_name = str(tls.get("server_name") or "").strip()
    if server_name:
        pairs.append(("sni", server_name))
    alpn = tls.get("alpn")
    if isinstance(alpn, (list, tuple)):
        values = [str(item) for item in alpn if str(item)]
        if values:
            pairs.append(("alpn", ",".join(values)))
    if tls.get("insecure"):
        pairs.append(("allowInsecure", "1"))
    utls = tls.get("utls") if isinstance(tls.get("utls"), dict) else {}
    fingerprint = str(utls.get("fingerprint") or "").strip()
    if fingerprint:
        pairs.append(("fp", fingerprint))
    if security == "reality":
        pairs.append(("pbk", str(reality.get("public_key"))))
        short_id = str(reality.get("short_id") or "")
        if short_id:
            pairs.append(("sid", short_id))
    return pairs


def _transport_pairs(transport):
    """Build ordered transport query pairs, or ``[]`` for plain TCP."""
    if not isinstance(transport, dict) or not transport:
        return []
    kind = str(transport.get("type") or "").strip().lower()
    if kind in ("", "tcp", "raw", "none"):
        return []
    if transport.get("max_early_data") or transport.get("early_data_header_name"):
        raise ShareLinkUnsupported("WebSocket early data is not portable")
    if kind == "ws":
        pairs = [("type", "ws")]
        headers = transport.get("headers") if isinstance(transport.get("headers"), dict) else {}
        host = str(headers.get("Host") or headers.get("host") or "")
        path = str(transport.get("path") or "")
        if host:
            pairs.append(("host", host))
        if path:
            pairs.append(("path", path))
        return pairs
    if kind == "grpc":
        pairs = [("type", "grpc")]
        service = str(transport.get("service_name") or "")
        if service:
            pairs.append(("serviceName", service))
        return pairs
    if kind == "quic":
        return [("type", "quic")]
    if kind == "httpupgrade":
        pairs = [("type", "httpupgrade")]
        host = str(transport.get("host") or "")
        path = str(transport.get("path") or "")
        if host:
            pairs.append(("host", host))
        if path:
            pairs.append(("path", path))
        return pairs
    if kind == "http":
        pairs = [("type", "http")]
        hosts = transport.get("host")
        if isinstance(hosts, (list, tuple)):
            host = str(hosts[0]) if hosts else ""
        else:
            host = str(hosts or "")
        path = str(transport.get("path") or "")
        if host:
            pairs.append(("host", host))
        if path:
            pairs.append(("path", path))
        return pairs
    raise ShareLinkUnsupported("unsupported transport: {}".format(kind))


def _emit_http_socks(node):
    config = _config(node)
    host, port = _endpoint(config)
    scheme = "socks5" if str(getattr(node, "protocol", "")).lower() == "socks" else "http"
    username = str(getattr(node, "remote_username", "") or "")
    password = str(getattr(node, "remote_password", "") or "")
    if not username and not password:
        userinfo = ""
    elif password:
        userinfo = "{}:{}@".format(_quote_all(username), _quote_all(password))
    else:
        userinfo = "{}@".format(_quote_all(username))
    return "{}://{}{}:{}{}".format(scheme, userinfo, host, port, _fragment(node, config))


def _emit_ss(node):
    config = _config(node)
    host, port = _endpoint(config)
    if config.get("plugin") or config.get("plugin_opts"):
        raise ShareLinkUnsupported("Shadowsocks plugins are not portable")
    method = str(config.get("method") or "").strip().lower()
    method = _SS_METHOD_ALIASES.get(method, method)
    if method not in SS_AEAD_METHODS:
        raise ShareLinkUnsupported("unsupported Shadowsocks method: {}".format(method or "unknown"))
    password = str(config.get("password") or "")
    if not password:
        raise ShareLinkUnsupported("missing Shadowsocks password")
    credentials = "{}:{}".format(method, password).encode("utf-8")
    userinfo = base64.urlsafe_b64encode(credentials).decode("ascii").rstrip("=")
    return "ss://{}@{}:{}{}".format(userinfo, host, port, _fragment(node, config))


def _emit_trojan(node):
    config = _config(node)
    host, port = _endpoint(config)
    password = str(config.get("password") or "")
    if not password:
        raise ShareLinkUnsupported("missing Trojan password")
    pairs = _transport_pairs(config.get("transport"))
    pairs.extend(_tls_pairs(config.get("tls"), default_enabled=True))
    return "trojan://{}@{}:{}{}".format(
        _quote_all(password), host, port, _suffix(pairs, node, config),
    )


def _emit_vless(node):
    config = _config(node)
    host, port = _endpoint(config)
    uuid = str(config.get("uuid") or "").strip()
    if not uuid:
        raise ShareLinkUnsupported("missing VLESS UUID")
    pairs = [("encryption", "none")]
    flow = str(config.get("flow") or "").strip()
    if flow:
        pairs.append(("flow", flow))
    pairs.extend(_transport_pairs(config.get("transport")))
    pairs.extend(_tls_pairs(config.get("tls"), default_enabled=False))
    return "vless://{}@{}:{}{}".format(
        _quote_all(uuid), host, port, _suffix(pairs, node, config),
    )


def _emit_vmess(node):
    config = _config(node)
    host, port = _endpoint(config)
    uuid = str(config.get("uuid") or "").strip()
    if not uuid:
        raise ShareLinkUnsupported("missing VMess UUID")
    transport = config.get("transport") if isinstance(config.get("transport"), dict) else {}
    kind = str(transport.get("type") or "").strip().lower()
    if kind in ("", "tcp", "raw", "none"):
        network = "tcp"
    elif kind == "http":
        network = "http"
    elif kind in ("ws", "grpc", "quic", "httpupgrade"):
        network = kind
    else:
        raise ShareLinkUnsupported("unsupported transport: {}".format(kind))
    if transport.get("max_early_data") or transport.get("early_data_header_name"):
        raise ShareLinkUnsupported("WebSocket early data is not portable")
    tls = config.get("tls") if isinstance(config.get("tls"), dict) else {}
    reality = tls.get("reality") if isinstance(tls.get("reality"), dict) else {}
    if reality.get("enabled"):
        raise ShareLinkUnsupported("VMess Reality nodes are not portable")
    tls_enabled = bool(tls.get("enabled", False))
    hosts = transport.get("host")
    if isinstance(hosts, (list, tuple)):
        transport_host = str(hosts[0]) if hosts else ""
    else:
        transport_host = str(hosts or "")
    headers = transport.get("headers") if isinstance(transport.get("headers"), dict) else {}
    ws_host = str(headers.get("Host") or headers.get("host") or "")
    utls = tls.get("utls") if isinstance(tls.get("utls"), dict) else {}
    alpn = tls.get("alpn")
    if isinstance(alpn, (list, tuple)):
        alpn_value = ",".join(str(item) for item in alpn if str(item))
    else:
        alpn_value = ""
    payload = {
        "add": config.get("server"),
        "port": str(port),
        "id": uuid,
        "aid": str(int(config.get("alter_id") or 0)),
        "scy": str(config.get("security") or "auto"),
        "net": network,
        "tls": "tls" if tls_enabled else "",
        "sni": str(tls.get("server_name") or ""),
        "alpn": alpn_value,
        "fp": str(utls.get("fingerprint") or ""),
        "allowInsecure": "1" if tls.get("insecure") else "",
        "host": transport_host or ws_host,
        "path": str(transport.get("path") or ""),
        "serviceName": str(transport.get("service_name") or ""),
    }
    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return "vmess://{}{}".format(encoded, _fragment(node, config))


def _emit_hysteria2(node):
    config = _config(node)
    host, port = _endpoint(config)
    password = str(config.get("password") or "")
    if not password:
        raise ShareLinkUnsupported("missing Hysteria2 password")
    pairs = _tls_pairs(config.get("tls"), default_enabled=True)
    if ("security", "none") in pairs:
        raise ShareLinkUnsupported("Hysteria2 requires TLS")
    obfs = config.get("obfs") if isinstance(config.get("obfs"), dict) else {}
    obfs_type = str(obfs.get("type") or "").strip()
    if obfs_type and obfs_type.lower() != "none":
        pairs.append(("obfs", obfs_type))
        obfs_password = str(obfs.get("password") or "")
        if obfs_password:
            pairs.append(("obfs-password", obfs_password))
    try:
        up = int(config.get("up_mbps") or 0)
    except (TypeError, ValueError):
        up = 0
    try:
        down = int(config.get("down_mbps") or 0)
    except (TypeError, ValueError):
        down = 0
    if up:
        pairs.append(("upmbps", str(up)))
    if down:
        pairs.append(("downmbps", str(down)))
    return "hysteria2://{}@{}:{}{}".format(
        _quote_all(password), host, port, _suffix(pairs, node, config),
    )


_EMITTERS = {
    "http": _emit_http_socks,
    "socks": _emit_http_socks,
    "ss": _emit_ss,
    "trojan": _emit_trojan,
    "vless": _emit_vless,
    "vmess": _emit_vmess,
    "hysteria2": _emit_hysteria2,
}
