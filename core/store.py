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
        if not isinstance(node, ProxyNode):
            node = ProxyNode.from_dict(node)
        return self.connection.hset(self.table, self._key(node), node.to_json)

    def put_many(self, nodes):
        if not nodes:
            return 0
        pipe = self.connection.pipeline()
        for node in nodes:
            if not isinstance(node, ProxyNode):
                node = ProxyNode.from_dict(node)
            pipe.hset(self.table, self._key(node), node.to_json)
        pipe.execute()
        return len(nodes)

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

    def update_sync(self, node_ids, revision):
        selected = set(node_ids)
        nodes = self.all()
        pipe = self.connection.pipeline()
        for node in nodes:
            node.synced = node.node_id in selected
            node.config_revision = revision if node.synced else ""
            pipe.hset(self.table, node.node_id, node.to_json)
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
