"""Fetch, detect and activate proxy nodes as one serialized task."""

import threading
import time

from core.singbox import NodeDetector, SingBoxSupervisor
from core.store import NodeStore
from handler.configHandler import ConfigHandler
from helper.fetch import Fetcher


class SyncManager:
    def __init__(self, logger, store=None, config=None):
        self.logger = logger
        self.conf = config or ConfigHandler()
        self.store = store or NodeStore()
        self.supervisor = SingBoxSupervisor(
            binary=self.conf.singBoxBinary,
            runtime_dir=self.conf.singBoxRuntimeDir,
            front_proxy=self.conf.frontProxy,
        )
        self.detector = NodeDetector(
            binary=self.conf.singBoxBinary,
            runtime_dir=self.conf.singBoxRuntimeDir,
            concurrency=self.conf.singBoxCheckConcurrency,
            http_url=self.conf.httpUrl,
            https_url=self.conf.httpsUrl,
            timeout=self.conf.verifyTimeout,
            front_proxy=self.conf.frontProxy,
        )
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.state = self._blank_state()
        self.launching = False
        self.next_sync = 0
        self.last_fetch = 0
        self.scheduler_thread = None

    @staticmethod
    def _blank_state():
        return {
            "running": False, "status": "idle", "phase": "idle",
            "start": 0, "end": 0, "current": 0, "total": 0,
            "fetched": 0, "checked": 0, "alive": 0, "failed": 0,
            "result": None, "error": "",
        }

    def snapshot(self):
        with self.lock:
            value = dict(self.state)
            value["next_sync"] = self.next_sync
            value["singbox"] = self.supervisor.status()
        try:
            value["redis"] = {"ok": self.store.ping(), "count": self.store.count()}
        except Exception as exc:
            value["redis"] = {"ok": False, "error": str(exc)}
        return value

    def _set(self, **changes):
        with self.lock:
            self.state.update(changes)

    def _source_report(self, source, count, error=None, parsed=0, skipped=0):
        if error:
            self.logger.error("[来源] {} 抓取失败，原始 {} 条，解析 {} 条，跳过 {} 条：{}".format(
                source, count, parsed, skipped, error
            ))
        else:
            self.logger.info("[来源] {} 抓取完成，原始 {} 条，解析 {} 条，跳过 {} 条".format(
                source, count, parsed, skipped
            ))

    def _merge_fetched(self, fetched):
        existing = {node.node_id: node for node in self.store.all()}
        merged = []
        for node in fetched:
            old = existing.get(node.node_id)
            if old:
                node.inbound_username = old.inbound_username
                node.inbound_password = old.inbound_password
                node.tls = old.tls
                node.synced = old.synced
                node.config_revision = old.config_revision
                node.check_count = old.check_count
                node.fail_count = old.fail_count
                node.last_status = old.last_status
                node.last_time = old.last_time
                node.region = old.region
            merged.append(node)
        fetched_ids = {node.node_id for node in merged}
        # Keep nodes from earlier fetches until the availability detector
        # proves them dead. This prevents one source outage from deleting the
        # entire pool.
        merged.extend(node for node in existing.values() if node.node_id not in fetched_ids)
        return merged

    def _commit(self, node_ids, revision):
        self.store.update_sync(node_ids, revision)

    def run_sync(self, force_fetch=False):
        with self.lock:
            if self.state["running"]:
                return False
            self.state = self._blank_state()
            self.state.update({"running": True, "status": "running", "phase": "fetching", "start": time.time()})
        try:
            now = time.time()
            do_fetch = force_fetch or not self.last_fetch or now - self.last_fetch >= self.conf.fetchIntervalSeconds
            nodes = self.store.all()
            if do_fetch:
                fetched = Fetcher().run(source_callback=self._source_report)
                nodes = self._merge_fetched(fetched)
                self.store.put_many(nodes)
                self.last_fetch = now
            self._set(fetched=len(nodes), phase="checking", total=len(nodes), current=0)
            checked = []

            def progress(node, error, current, total):
                checked.append(node)
                self._set(current=current, total=total, checked=current)
                if error:
                    self.logger.warn("[检测] {} 失败：{}".format(node.proxy, error))
                else:
                    self.logger.info("[检测] {} 成功，TLS={}".format(node.proxy, node.tls))

            checked = self.detector.detect(nodes, callback=progress)
            # Keep the active revision visible while the replacement instance
            # is still being built. The detector mutates its in-memory nodes
            # to synced=False, but that must not interrupt the old instance.
            previous = {node.node_id: node for node in self.store.all()}
            for node in checked:
                old = previous.get(node.node_id)
                if old:
                    node.synced = old.synced
                    node.config_revision = old.config_revision
            self.store.put_many(checked)
            alive = [node for node in checked if node.last_status]
            self._set(alive=len(alive), failed=len(checked) - len(alive), phase="activating")
            if not alive and self.supervisor.endpoint():
                raise RuntimeError("本轮没有可用节点，保留旧正式 sing-box 配置")
            self.supervisor.activate(alive, self._commit)
            result = {
                "fetched": len(nodes), "checked": len(checked),
                "alive": len(alive), "failed": len(checked) - len(alive),
                "revision": self.supervisor.endpoint()[2] if self.supervisor.endpoint() else "",
            }
            self._set(status="done", phase="completed", result=result, end=time.time(), running=False)
            self.logger.info("同步完成：检测 {} 条，可用 {} 条".format(len(checked), len(alive)))
            return result
        except Exception as exc:
            self.logger.error("同步失败：{}".format(exc))
            self._set(status="error", phase="failed", error=str(exc), result=str(exc), end=time.time(), running=False)
            return None
        finally:
            self.next_sync = time.time() + self.conf.checkIntervalSeconds

    def start_async(self, force_fetch=True):
        with self.lock:
            if self.state["running"] or self.launching:
                return False
            self.launching = True

        def runner():
            try:
                self.run_sync(force_fetch=force_fetch)
            finally:
                with self.lock:
                    self.launching = False

        threading.Thread(target=runner, name="sync-task", daemon=True).start()
        return True

    def start_scheduler(self):
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            return
        self.stop_event.clear()

        def worker():
            self.run_sync(force_fetch=True)
            while not self.stop_event.wait(max(1, self.conf.checkIntervalSeconds)):
                self.run_sync(force_fetch=False)

        self.scheduler_thread = threading.Thread(target=worker, name="sync-scheduler", daemon=True)
        self.scheduler_thread.start()

    def stop(self):
        self.stop_event.set()
        self.supervisor.stop()
