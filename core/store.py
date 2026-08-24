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

    def get(self, node_id):
        with self.lock:
            value = self.connection.hget(self.table, node_id)
        return ProxyNode.from_json(value) if value else None

    def all(self):
        nodes = []
        with self.lock:
            values = self.connection.hvals(self.table)
        for value in values:
            try:
                nodes.append(ProxyNode.from_json(value))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return nodes

    def active(self, tls_required=False):
        return [node for node in self.all() if node.synced and (not tls_required or node.tls)]

    def delete(self, node_id):
        with self.lock:
            return bool(self.connection.hdel(self.table, node_id))

    @staticmethod
    def _merge_sources(*values):
        sources = set()
        for value in values:
            sources.update(item for item in (value or "").split("/") if item)
        return "/".join(sorted(sources))

    def upsert_candidates(self, nodes, skip_ids=None):
        """Insert fetched candidates without overwriting detector-owned fields."""
        candidates = list(nodes or [])
        skipped_ids = set(skip_ids or ())
        result = {"inserted": 0, "updated": 0, "skipped": 0}
        with self.lock:
            pipe = self.connection.pipeline(transaction=True)
            for candidate in candidates:
                if candidate.node_id in skipped_ids:
                    result["skipped"] += 1
                    continue
                value = self.connection.hget(self.table, candidate.node_id)
                if value:
                    current = ProxyNode.from_json(value)
                    candidate.inbound_username = current.inbound_username
                    candidate.inbound_password = current.inbound_password
                    candidate.tls = current.tls
                    candidate.synced = current.synced
                    candidate.config_revision = current.config_revision
                    candidate.fail_count = current.fail_count
                    candidate.check_count = current.check_count
                    candidate.last_status = current.last_status
                    candidate.last_time = current.last_time
                    candidate.source = self._merge_sources(current.source, candidate.source)
                    result["updated"] += 1
                else:
                    candidate.tls = False
                    candidate.synced = False
                    candidate.config_revision = ""
                    result["inserted"] += 1
                pipe.hset(self.table, candidate.node_id, candidate.to_json)
            pipe.execute()
        return result

    def commit_detection(self, alive, failed_ids, snapshot_ids, revision):
        """Atomically publish one successful detection snapshot."""
        selected = list(alive or [])
        failed = set(failed_ids or ()) & set(snapshot_ids or ())
        selected_ids = {node.node_id for node in selected}
        failed -= selected_ids
        with self.lock:
            pipe = self.connection.pipeline(transaction=True)
            committed = 0
            for checked in selected:
                value = self.connection.hget(self.table, checked.node_id)
                if not value:
                    continue
                current = ProxyNode.from_json(value)
                current.tls = bool(checked.tls)
                current.synced = True
                current.config_revision = revision
                current.fail_count = checked.fail_count
                current.check_count = checked.check_count
                current.last_status = bool(checked.last_status)
                current.last_time = checked.last_time
                current.source = self._merge_sources(current.source, checked.source)
                pipe.hset(self.table, current.node_id, current.to_json)
                committed += 1
            if failed:
                pipe.hdel(self.table, *sorted(failed))
            pipe.execute()
        return {"active": committed, "deleted": len(failed)}

    def mark_unsynced(self, node_id):
        """Remove one runtime-failed node from selection without deleting it."""
        with self.lock:
            value = self.connection.hget(self.table, node_id)
            if not value:
                return False
            node = ProxyNode.from_json(value)
            node.synced = False
            node.config_revision = ""
            self.connection.hset(self.table, node_id, node.to_json)
            return True

    def count(self):
        nodes = self.all()
        return {
            "total": len(nodes),
            "synced": sum(1 for node in nodes if node.synced),
            "tls": sum(1 for node in nodes if node.tls),
            "http": sum(1 for node in nodes if node.proxy_type == "http"),
            "socks": sum(1 for node in nodes if node.proxy_type == "socks"),
        }
