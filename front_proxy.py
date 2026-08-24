# -*- coding: utf-8 -*-
"""Resolve the optional proxy used by outbound pool-management requests."""

import os


FRONT_PROXY_ENV_NAMES = ("FRONT_PROXY", "PROXY_POOL_FRONT_PROXY")
def get_environment_front_proxy():
    """Return ``(value, variable_name)`` for the first defined env value."""
    for name in FRONT_PROXY_ENV_NAMES:
        value = os.getenv(name)
        if value is not None:
            return value.strip(), name
    return "", None


def resolve_front_proxy(configured=""):
    """Resolve the optional proxy, including an explicitly empty env value."""
    environment_value, variable_name = get_environment_front_proxy()
    if variable_name:
        return environment_value
    return (configured or "").strip()


def front_proxy_is_locked():
    """Whether an environment value controls the effective configuration."""
    _, variable_name = get_environment_front_proxy()
    return bool(variable_name)


def front_proxy_requests(configured=""):
    """Return a requests-compatible proxy mapping, or an empty mapping."""
    proxy = resolve_front_proxy(configured)
    if not proxy:
        return {}
    return {"http": proxy, "https": proxy}


def front_proxy_metadata(configured=""):
    """Return safe metadata for the control panel configuration response."""
    environment_value, variable_name = get_environment_front_proxy()
    effective = environment_value if variable_name else (configured or "").strip()
    return {
        "front_proxy": effective,
        "front_proxy_locked": bool(variable_name),
        "front_proxy_source": "environment" if variable_name else (
            "web" if effective else "direct"
        ),
        "front_proxy_env": variable_name,
    }
