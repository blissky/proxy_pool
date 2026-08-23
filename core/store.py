"""Redis storage for structured proxy nodes."""

import json
import threading
from urllib.parse import urlsplit

from redis import Redis
from redis.connection import BlockingConnectionPool

from handler.configHandler import ConfigHandler
from .node import ProxyNode


class NodeStore:
    """Small Redis hash repository; Redis remains the only node database."""

    def __init__(self, connection=None, table=None):
        self.conf = ConfigHandler()
        self.table = table or self.conf.tableName
        self.lock = threading.RLock()
        if connection is not None:
            self.connection = connection
        else:
            parsed = urlsplit(self.conf.dbConn)
            self.connection = Redis(
                connection_pool=BlockingConnectionPool(
                    host=parsed.hostname,
                    port=parsed.port or 6379,
                    username=parsed.username,
                    password=parsed.password,
                    db=int((parsed.path or "/0").lstrip("/") or 0),
                    decode_responses=True,
                    timeout=5,
                    socket_timeout=5,
                    protocol=2,
                )
            )

    def ping(self):
        return bool(self.connection.ping())

    def _key(self, node):
        return node.node_id

    def put(self, node):
        """Update an already admitted node without admitting new candidates."""
        if not isinstance(node, ProxyNode):
            node = ProxyNode.from_dict(node)
        if not self.connection.hexists(self.table, self._key(node)):
            return 0
        return self.connection.hset(self.table, self._key(node), node.to_json)

    def get(self, node_id):
        value = self.connection.hget(self.table, node_id)
        return ProxyNode.from_json(value) if value else None

    def all(self):
        nodes = []
        for value in self.connection.hvals(self.table):
            try:
                nodes.append(ProxyNode.from_json(value))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return nodes

    def active(self, tls_required=False):
        return [node for node in self.all() if node.synced and (not tls_required or node.tls)]

    def delete(self, node_id):
        return bool(self.connection.hdel(self.table, node_id))

    def replace_active(self, nodes, revision):
        """Commit only nodes that passed detection and entered the new config."""
        selected = list(nodes or [])
        selected_ids = {node.node_id for node in selected}
        existing_ids = set(self.connection.hkeys(self.table))
        pipe = self.connection.pipeline(transaction=True)
        for node in selected:
            node.synced = True
            node.config_revision = revision
            pipe.hset(self.table, node.node_id, node.to_json)
        stale_ids = existing_ids - selected_ids
        if stale_ids:
            pipe.hdel(self.table, *stale_ids)
        pipe.execute()
        return len(selected)

    def count(self):
        nodes = self.all()
        return {
            "total": len(nodes),
            "synced": sum(1 for node in nodes if node.synced),
            "tls": sum(1 for node in nodes if node.tls),
            "http": sum(1 for node in nodes if node.proxy_type == "http"),
            "socks": sum(1 for node in nodes if node.proxy_type == "socks"),
        }
