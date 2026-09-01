"""Shared helpers for dated Base64 subscription sources."""

import base64
import binascii
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from core.node_parser import decode_base64_text, extract_uris, parse_node_uri
from handler.configHandler import ConfigHandler


_BASE64_PATTERN = re.compile(r"^[A-Za-z0-9+/_-]*={0,2}$")


def current_date():
    """Capture the configured local date once for one source fetch."""
    timezone_name = ConfigHandler().timezone
    try:
        timezone = ZoneInfo(timezone_name)
    except (KeyError, ValueError) as exc:
        raise ValueError("invalid configured timezone: {}".format(timezone_name)) from exc
    return datetime.now(timezone).date()


def decode_subscription(value):
    """Strictly decode a Base64 subscription using the shared decoder."""
    raw = "".join((value or "").strip().lstrip("\ufeff").split())
    if not raw:
        return ""
    if len(raw) % 4 == 1 or not _BASE64_PATTERN.fullmatch(raw):
        raise ValueError("invalid base64 subscription")
    try:
        base64.b64decode(raw + "=" * ((4 - len(raw) % 4) % 4),
                         altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid base64 subscription") from exc
    return decode_base64_text(raw)


def iter_nodes(decoded, source, log):
    """Parse supported URIs and skip malformed individual entries."""
    for uri in extract_uris(decoded):
        try:
            yield parse_node_uri(uri, source)
        except (TypeError, ValueError, UnicodeError) as exc:
            log.warning("ProxyFetch - %s: skip node: %s" % (source, exc))
