# -*- coding: utf-8 -*-
"""Socket helpers for the two-hop front-proxy -> pool-proxy chain."""

import base64
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit


def parse_proxy(value, default_scheme="http"):
    """Parse a proxy URL or an authenticated ``user:pass@host:port`` value."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("proxy is empty")
    if "://" not in raw:
        raw = default_scheme + "://" + raw
    parsed = urlsplit(raw)
    if not parsed.hostname or parsed.port is None:
        raise ValueError("invalid proxy: {}".format(value))
    return (
        (parsed.scheme or default_scheme).lower(),
        parsed.hostname,
        parsed.port,
        parsed.username,
        parsed.password,
    )


def _recv_exact(sock, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise OSError("proxy connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_headers(sock, limit=65536):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise OSError("proxy response headers too large")
    return data


def _connect_to_endpoint(host, port, timeout, front_proxy=""):
    """Connect to an endpoint directly or through the configured front proxy."""
    if not front_proxy:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        return sock

    scheme, front_host, front_port, username, password = parse_proxy(front_proxy)
    try:
        import socks
    except ImportError as exc:
        raise OSError("PySocks is required for a front proxy") from exc
    proxy_types = {
        "socks5": socks.PROXY_TYPE_SOCKS5,
        "socks5h": socks.PROXY_TYPE_SOCKS5,
        "socks4": socks.PROXY_TYPE_SOCKS4,
        "socks4a": socks.PROXY_TYPE_SOCKS4,
        "http": socks.PROXY_TYPE_HTTP,
        "https": socks.PROXY_TYPE_HTTP,
    }
    if scheme not in proxy_types:
        raise ValueError("unsupported front proxy type: {}".format(scheme))
    sock = socks.socksocket()
    sock.set_proxy(
        proxy_types[scheme],
        addr=front_host,
        port=front_port,
        rdns=scheme in ("socks5", "socks5h", "socks4a"),
        username=username,
        password=password,
    )
    sock.settimeout(timeout)
    sock.connect((host, port))
    return sock


def connect_to_proxy(proxy_host, proxy_port, timeout=10, front_proxy=""):
    """Open a socket to a pool proxy, optionally through the front proxy."""
    return _connect_to_endpoint(proxy_host, proxy_port, timeout, front_proxy)


def _socks5_connect(sock, host, port, username=None, password=None):
    if username is None:
        sock.sendall(b"\x05\x01\x00")
    else:
        sock.sendall(b"\x05\x02\x00\x02")
    response = _recv_exact(sock, 2)
    if response[0] != 5:
        raise OSError("invalid SOCKS5 upstream response")
    if response[1] == 2:
        user = (username or "").encode("utf-8")
        secret = (password or "").encode("utf-8")
        if len(user) > 255 or len(secret) > 255:
            raise ValueError("SOCKS5 credentials are too long")
        sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(secret)]) + secret)
        auth = _recv_exact(sock, 2)
        if auth != b"\x01\x00":
            raise OSError("SOCKS5 upstream authentication failed")
    elif response[1] != 0:
        raise OSError("SOCKS5 upstream refused authentication: {}".format(response[1]))

    try:
        packed = ipaddress.ip_address(host).packed
    except ValueError:
        encoded = host.encode("idna")
        if len(encoded) > 255:
            raise ValueError("destination hostname is too long")
        address = b"\x03" + bytes([len(encoded)]) + encoded
    else:
        address = (b"\x01" if len(packed) == 4 else b"\x04") + packed
    sock.sendall(b"\x05\x01\x00" + address + port.to_bytes(2, "big"))
    header = _recv_exact(sock, 4)
    if header[0] != 5 or header[1] != 0:
        raise OSError("SOCKS5 upstream refused connection: {}".format(header[1]))
    atyp = header[3]
    if atyp == 1:
        _recv_exact(sock, 4)
    elif atyp == 3:
        length = _recv_exact(sock, 1)[0]
        _recv_exact(sock, length)
    elif atyp == 4:
        _recv_exact(sock, 16)
    else:
        raise OSError("invalid SOCKS5 response address type")
    _recv_exact(sock, 2)


def _socks4_connect(sock, host, port):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        request = b"\x04\x01" + port.to_bytes(2, "big") + b"\x00\x00\x00\x01\x00" + host.encode("idna") + b"\x00"
    else:
        if address.version != 4:
            raise ValueError("SOCKS4 only supports IPv4 destinations")
        request = b"\x04\x01" + port.to_bytes(2, "big") + address.packed + b"\x00"
    sock.sendall(request)
    response = _recv_exact(sock, 8)
    if response[1] != 90:
        raise OSError("SOCKS4 upstream refused connection: {}".format(response[1]))


def _http_connect(sock, host, port, username=None, password=None):
    headers = [
        "CONNECT {}:{} HTTP/1.1".format(host, port),
        "Host: {}:{}".format(host, port),
        "Proxy-Connection: Keep-Alive",
    ]
    if username is not None:
        token = base64.b64encode(
            ("{}:{}".format(username, password or "")).encode("utf-8")
        ).decode("ascii")
        headers.append("Proxy-Authorization: Basic " + token)
    sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
    response = _recv_headers(sock)
    first_line = response.split(b"\r\n", 1)[0]
    try:
        status = int(first_line.split(None, 2)[1])
    except (IndexError, ValueError):
        status = 0
    if status != 200:
        raise OSError("HTTP upstream refused CONNECT: {}".format(status))


def connect_via_proxy(
    proxy_type,
    proxy_host,
    proxy_port,
    dest_host,
    dest_port,
    timeout=10,
    front_proxy="",
    proxy_username=None,
    proxy_password=None,
):
    """Connect to a destination through a pool proxy, optionally fronted."""
    sock = _connect_to_endpoint(proxy_host, proxy_port, timeout, front_proxy)
    try:
        if proxy_type == "socks5":
            _socks5_connect(sock, dest_host, dest_port, proxy_username, proxy_password)
        elif proxy_type == "socks4":
            _socks4_connect(sock, dest_host, dest_port)
        elif proxy_type == "http":
            _http_connect(sock, dest_host, dest_port,
                          proxy_username, proxy_password)
        else:
            raise ValueError("unsupported upstream type: {}".format(proxy_type))
        return sock
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise


def _http_request(sock, method, target, host, headers, body=b""):
    request_headers = {"Host": host, "Connection": "close"}
    request_headers.update(headers or {})
    lines = ["{} {} HTTP/1.1".format(method, target)]
    lines.extend("{}: {}".format(k, v) for k, v in request_headers.items())
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body)
    response = http.client.HTTPResponse(sock, method=method)
    response.begin()
    return response.status, response.read()


def request_via_proxy(
    proxy_type,
    proxy_host,
    proxy_port,
    url,
    timeout=10,
    front_proxy="",
    proxy_username=None,
    proxy_password=None,
    method="GET",
):
    """Issue one HTTP request through a pool proxy and optional front proxy."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("only HTTP(S) URLs are supported")
    dest_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    sock = _connect_to_endpoint(proxy_host, proxy_port, timeout, front_proxy)
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        host_header = parsed.hostname
        if parsed.port:
            host_header += ":{}".format(parsed.port)
        headers = {"User-Agent": "ProxyPool/1.0"}
        if proxy_type == "http":
            if parsed.scheme == "https":
                _http_connect(sock, parsed.hostname, dest_port,
                              proxy_username, proxy_password)
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=parsed.hostname
                )
                target = path
            else:
                target = url
                if proxy_username is not None:
                    token = base64.b64encode(
                        ("{}:{}".format(proxy_username, proxy_password or "")).encode("utf-8")
                    ).decode("ascii")
                    headers["Proxy-Authorization"] = "Basic " + token
        else:
            if proxy_type == "socks5":
                _socks5_connect(sock, parsed.hostname, dest_port,
                                 proxy_username, proxy_password)
            elif proxy_type == "socks4":
                _socks4_connect(sock, parsed.hostname, dest_port)
            else:
                raise ValueError("unsupported upstream type: {}".format(proxy_type))
            target = path
            if parsed.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=parsed.hostname
                )
        return _http_request(sock, method, target, host_header, headers)
    finally:
        try:
            sock.close()
        except Exception:
            pass
