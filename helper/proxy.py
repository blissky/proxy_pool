"""Compatibility import for the structured proxy node model."""

from core.node import LEGACY_TYPES, PROTOCOLS, ProxyNode


PROXY_TYPES = LEGACY_TYPES
Proxy = ProxyNode

__all__ = ["Proxy", "ProxyNode", "PROXY_TYPES", "PROTOCOLS"]
