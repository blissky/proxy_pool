"""Independent source fetching, node detection and sing-box activation."""

import threading
import time
import uuid
from copy import deepcopy

from core.singbox import NodeDetector, SingBoxSupervisor
from core.store import NodeStore
from front_proxy import require_front_proxy
from handler.configHandler import ConfigHandler
from helper.fetch import Fetcher, get_fetcher_source_count


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
        self.coordination_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.initial_fetch_complete = threading.Event()
        self.fetch_state = self._blank_fetch_state()
        self.check_state = self._blank_check_state()
        self.next_fetch = 0
        self.next_check = 0
        self.sync_epoch = 0
        self.failed_ids_by_epoch = {}
        self.active_fetch_epochs = {}
        self.scheduler_thread = None

    @staticmethod
    def _blank_fetch_state():
        return {
            "running": False, "status": "idle", "phase": "idle",
            "start": 0, "end": 0, "current": 0, "total": 0,
            "raw": 0, "parsed": 0, "duplicates": 0, "skipped": 0,
            "fetched": 0, "inserted": 0, "updated": 0,
            "write_skipped": 0, "result": None, "error": "",
        }

    @staticmethod
    def _blank_check_state():
        return {
            "running": False, "status": "idle", "phase": "idle",
            "start": 0, "end": 0, "current": 0, "total": 0,
            "checked": 0, "alive": 0, "failed": 0,
            "result": None, "error": "",
        }

    def snapshot(self):
        with self.lock:
            fetch = deepcopy(self.fetch_state)
            check = deepcopy(self.check_state)
            fetch["next_run"] = self.next_fetch
            check["next_run"] = self.next_check
            value = {
                "fetch": fetch,
                "check": check,
                "singbox": self.supervisor.status(),
            }
        try:
            value["redis"] = {"ok": self.store.ping(), "count": self.store.count()}
        except Exception as exc:
            value["redis"] = {"ok": False, "error": str(exc)}
        try:
            value["source_count"] = get_fetcher_source_count(self.conf.fetcherExclude)
        except Exception:
            value["source_count"] = 0
        value["front_proxy_configured"] = bool(self.conf.frontProxy)
        return value

    def _set_fetch(self, **changes):
        with self.lock:
            self.fetch_state.update(changes)

    def _set_check(self, **changes):
        with self.lock:
            self.check_state.update(changes)

    def _safe_error(self, error):
        message = str(error)
        proxy = self.conf.frontProxy
        return message.replace(proxy, "<front-proxy>") if proxy else message

    def _source_report(self, source, count, error=None, parsed=0, skipped=0, duplicates=0):
        with self.lock:
            self.fetch_state["current"] += 1
            self.fetch_state["raw"] += count
            self.fetch_state["parsed"] += parsed
            self.fetch_state["skipped"] += skipped
            self.fetch_state["duplicates"] += duplicates
        detail = "原始 {} 条，解析 {} 条，重复 {} 条，跳过 {} 条".format(
            count, parsed, duplicates, skipped,
        )
        if error:
            self.logger.error("[来源] {} 抓取失败，{}：{}".format(
                source, detail, self._safe_error(error),
            ))
        else:
            self.logger.info("[来源] {} 抓取完成，{}".format(source, detail))

    def _begin_fetch(self):
        with self.lock:
            if self.fetch_state["running"]:
                return False
            total = get_fetcher_source_count(self.conf.fetcherExclude)
            self.fetch_state = self._blank_fetch_state()
            self.fetch_state.update({
                "running": True, "status": "running", "phase": "fetching",
                "start": time.time(), "total": total,
            })
            self.next_fetch = 0
            return True

    def _begin_check(self):
        with self.lock:
            if self.check_state["running"]:
                return False
            self.check_state = self._blank_check_state()
            self.check_state.update({
                "running": True, "status": "running", "phase": "loading",
                "start": time.time(),
            })
            self.next_check = 0
            return True

    def _start_fetch_batch(self):
        batch_id = uuid.uuid4().hex
        with self.coordination_lock:
            start_epoch = self.sync_epoch
            self.active_fetch_epochs[batch_id] = start_epoch
        return batch_id, start_epoch

    def _finish_fetch_batch(self, batch_id):
        with self.coordination_lock:
            self.active_fetch_epochs.pop(batch_id, None)
            if not self.active_fetch_epochs:
                self.failed_ids_by_epoch.clear()
                return
            oldest = min(self.active_fetch_epochs.values())
            for epoch in list(self.failed_ids_by_epoch):
                if epoch <= oldest:
                    self.failed_ids_by_epoch.pop(epoch, None)

    def _execute_fetch(self):
        batch_id, start_epoch = self._start_fetch_batch()
        try:
            require_front_proxy(self.conf.frontProxy)
            nodes = Fetcher().run(source_callback=self._source_report)
            if self.stop_event.is_set():
                raise RuntimeError("服务正在停止，取消抓取提交")
            with self.lock:
                self.fetch_state["duplicates"] = max(
                    self.fetch_state["duplicates"],
                    self.fetch_state["parsed"] - len(nodes),
                )
            self._set_fetch(phase="committing", fetched=len(nodes))
            with self.store.lock:
                with self.coordination_lock:
                    relevant = [
                        failed for epoch, failed in self.failed_ids_by_epoch.items()
                        if epoch > start_epoch
                    ]
                    blocked_ids = set().union(*relevant) if relevant else set()
                write_result = self.store.upsert_candidates(nodes, skip_ids=blocked_ids)
            result = {
                "fetched": len(nodes),
                "inserted": write_result["inserted"],
                "updated": write_result["updated"],
                "skipped": write_result["skipped"],
            }
            self._set_fetch(
                status="done", phase="completed", result=result,
                fetched=len(nodes), inserted=write_result["inserted"],
                updated=write_result["updated"],
                write_skipped=write_result["skipped"],
                end=time.time(), running=False,
            )
            self.logger.info(
                "抓取完成：唯一节点 {} 条，新增 {} 条，更新 {} 条，跳过过期批次 {} 条".format(
                    len(nodes), write_result["inserted"], write_result["updated"],
                    write_result["skipped"],
                )
            )
            return result
        except Exception as exc:
            message = self._safe_error(exc)
            self.logger.error("抓取失败：{}".format(message))
            self._set_fetch(
                status="error", phase="failed", error=message,
                result=None, end=time.time(), running=False,
            )
            return None
        finally:
            self._finish_fetch_batch(batch_id)
            with self.lock:
                self.next_fetch = time.time() + self.conf.fetchIntervalSeconds

    def run_fetch(self):
        if not self._begin_fetch():
            return False
        return self._execute_fetch()

    def start_fetch_async(self):
        if not self._begin_fetch():
            return False
        threading.Thread(
            target=self._execute_fetch, name="fetch-task", daemon=True,
        ).start()
        return True

    def _publish_detection(self, alive, failed_ids, snapshot_ids, revision):
        with self.store.lock:
            result = self.store.commit_detection(
                alive, failed_ids, snapshot_ids, revision,
            )
            with self.coordination_lock:
                self.sync_epoch += 1
                self.failed_ids_by_epoch[self.sync_epoch] = set(failed_ids)
        return result

    def _execute_check(self):
        try:
            require_front_proxy(self.conf.frontProxy)
            snapshot = deepcopy(self.store.all())
            snapshot_ids = {node.node_id for node in snapshot}
            self._set_check(phase="checking", total=len(snapshot), current=0)

            def progress(node, error, current, total):
                self._set_check(current=current, total=total, checked=current)
                if error:
                    self.logger.warn("[检测] {} 不可用：{}".format(
                        node.proxy, self._safe_error(error),
                    ))
                else:
                    self.logger.info("[检测] {} 可用，TLS={}".format(node.proxy, node.tls))

            checked = self.detector.detect(snapshot, callback=progress)
            alive = sorted(
                (node for node in checked if node.last_status),
                key=lambda node: node.node_id,
            )
            failed_ids = {node.node_id for node in checked if not node.last_status}
            self._set_check(
                alive=len(alive), failed=len(failed_ids), phase="activating",
            )
            if self.stop_event.is_set():
                raise RuntimeError("服务正在停止，取消正式实例激活")
            if not alive and self.supervisor.endpoint():
                raise RuntimeError("本轮没有可用节点，保留旧正式 sing-box 配置")
            commit_result = {}

            def commit(nodes, revision):
                commit_result.update(self._publish_detection(
                    nodes, failed_ids, snapshot_ids, revision,
                ))

            self.supervisor.activate(alive, commit)
            endpoint = self.supervisor.endpoint()
            result = {
                "checked": len(checked), "alive": len(alive),
                "failed": len(failed_ids),
                "activated": commit_result.get("active", 0),
                "deleted": commit_result.get("deleted", 0),
                "revision": endpoint[2] if endpoint else "",
            }
            self._set_check(
                status="done", phase="completed", result=result,
                checked=len(checked), alive=len(alive), failed=len(failed_ids),
                end=time.time(), running=False,
            )
            self.logger.info(
                "同步完成：检测 {} 条，可用 {} 条，删除 {} 条".format(
                    len(checked), len(alive), commit_result.get("deleted", 0),
                )
            )
            return result
        except Exception as exc:
            message = self._safe_error(exc)
            self.logger.error("同步失败：{}".format(message))
            self._set_check(
                status="error", phase="failed", error=message,
                result=None, end=time.time(), running=False,
            )
            return None
        finally:
            with self.lock:
                self.next_check = time.time() + self.conf.checkIntervalSeconds

    def run_check(self):
        if not self._begin_check():
            return False
        return self._execute_check()

    def start_check_async(self):
        if (self.scheduler_thread and self.scheduler_thread.is_alive()
                and not self.initial_fetch_complete.is_set()):
            return False
        if not self._begin_check():
            return False
        threading.Thread(
            target=self._execute_check, name="check-task", daemon=True,
        ).start()
        return True

    def start_scheduler(self):
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            return
        self.stop_event.clear()
        self.initial_fetch_complete.clear()

        def worker():
            self.run_fetch()
            self.initial_fetch_complete.set()
            if self.stop_event.is_set():
                return
            self.start_check_async()
            while not self.stop_event.wait(0.5):
                now = time.time()
                with self.lock:
                    fetch_due = bool(self.next_fetch and now >= self.next_fetch)
                    check_due = bool(self.next_check and now >= self.next_check)
                if fetch_due:
                    self.start_fetch_async()
                if check_due:
                    self.start_check_async()

        self.scheduler_thread = threading.Thread(
            target=worker, name="task-scheduler", daemon=True,
        )
        self.scheduler_thread.start()

    def stop(self):
        self.stop_event.set()
        self.detector.stop()
        self.supervisor.stop()
