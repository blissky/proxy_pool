# -*- coding: utf-8 -*-
"""Resolve the optional proxy used by outbound pool-management requests."""

import json
import os


FRONT_PROXY_ENV_NAMES = ("FRONT_PROXY", "PROXY_POOL_FRONT_PROXY")
DEFAULT_CONFIG_FILE = "config.json"


def get_environment_front_proxy():
    """Return ``(value, variable_name)`` for the first non-empty env value."""
    for name in FRONT_PROXY_ENV_NAMES:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip(), name
    return "", None


def get_persisted_front_proxy(path=None):
    """Read the Web-configured value shared by all service processes."""
    config_path = path or os.getenv("CONFIG_FILE", DEFAULT_CONFIG_FILE)
    try:
        with open(config_path, "r", encoding="utf-8") as config_file:
            data = json.load(config_file)
    except (OSError, ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return (data.get("front_proxy") or data.get("fetch_proxy") or "").strip()


def resolve_front_proxy(configured=""):
    """Resolve the effective proxy, with environment configuration first."""
    environment_value, _ = get_environment_front_proxy()
    if environment_value:
        return environment_value
    return (configured or "").strip() or get_persisted_front_proxy()


def front_proxy_is_locked():
    """Whether a non-empty environment value prevents UI configuration."""
    value, _ = get_environment_front_proxy()
    return bool(value)


def front_proxy_requests(configured=""):
    """Return a requests-compatible proxy mapping, or an empty mapping."""
    proxy = resolve_front_proxy(configured)
    if not proxy:
        return {}
    return {"http": proxy, "https": proxy}


def require_front_proxy(configured=""):
    """Return the effective front proxy or reject a direct outbound path."""
    proxy = resolve_front_proxy(configured)
    if not proxy:
        raise RuntimeError("FRONT_PROXY is required; direct outbound access is disabled")
    return proxy


def front_proxy_metadata(configured=""):
    """Return safe metadata for the control panel configuration response."""
    environment_value, variable_name = get_environment_front_proxy()
    effective = environment_value or (configured or "").strip() or get_persisted_front_proxy()
    return {
        "front_proxy": effective,
        "front_proxy_locked": bool(environment_value),
        "front_proxy_source": "environment" if environment_value else (
            "web" if effective else "direct"
        ),
        "front_proxy_env": variable_name,
    }
