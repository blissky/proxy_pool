"""Dynamic proxy-source discovery and structured node collection."""

import importlib
import os
import sys
from threading import Lock, Thread

from core.node import ProxyNode
from fetcher.baseFetcher import BaseFetcher
from handler.configHandler import ConfigHandler
from handler.logHandler import LogHandler


_logger = LogHandler("fetch")
_module_cache = {}


def _get_sources_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fetcher", "sources")


def _load_module(module_name, filepath):
    global _module_cache
    mtime = os.path.getmtime(filepath)
    cached = _module_cache.get(module_name)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        module = importlib.reload(sys.modules[module_name]) if module_name in sys.modules else importlib.import_module(module_name)
        _module_cache[module_name] = (mtime, module)
        return module
    except Exception as exc:
        _logger.warning("ProxyFetch: load %s error - %s" % (module_name, exc))
        return None


def _discover_fetchers(exclude_list):
    sources_dir = _get_sources_dir()
    fetcher_classes = []
    seen_modules = set()
    for filename in sorted(os.listdir(sources_dir)):
        if not filename.endswith(".py") or filename.startswith("_"):
            continue
        module_name = "fetcher.sources.%s" % filename[:-3]
        seen_modules.add(module_name)
        module = _load_module(module_name, os.path.join(sources_dir, filename))
        if module is None:
            continue
        for attr in vars(module).values():
            if (isinstance(attr, type) and issubclass(attr, BaseFetcher)
                    and attr is not BaseFetcher and attr.name and attr.enabled
                    and attr.__name__ not in exclude_list):
                fetcher_classes.append(attr)
    for name in list(_module_cache):
        if name not in seen_modules:
            del _module_cache[name]
    return sorted(fetcher_classes, key=lambda cls: cls.name)


def get_fetcher_source_count(exclude_list=None):
    if exclude_list is None:
        exclude_list = ConfigHandler().fetcherExclude
    return len(_discover_fetchers(exclude_list))


class _ThreadFetcher(Thread):
    def __init__(self, fetcher_class, node_dict, source_callback=None, lock=None):
        super().__init__(name="fetch-{}".format(fetcher_class.name))
        self.fetcher_class = fetcher_class
        self.node_dict = node_dict
        self.source_callback = source_callback
        self.lock = lock or Lock()
        self.log = LogHandler("fetcher")

    def _report(self, count, error=None, parsed=0, skipped=0):
        if self.source_callback:
            try:
                self.source_callback(self.fetcher_class.name, count, error, parsed, skipped)
            except TypeError:
                self.source_callback(self.fetcher_class.name, count, error)

    def run(self):
        count = parsed = skipped = 0
        try:
            for value in self.fetcher_class().fetch():
                count += 1
                try:
                    if isinstance(value, ProxyNode):
                        node = value
                        node.add_source(self.fetcher_class.name)
                    else:
                        protocol = getattr(self.fetcher_class, "proxy_type", "http")
                        node = ProxyNode.from_endpoint(value, self.fetcher_class.name, protocol)
                    key = node.node_id
                    with self.lock:
                        if key in self.node_dict:
                            self.node_dict[key].add_source(self.fetcher_class.name)
                        else:
                            self.node_dict[key] = node
                    parsed += 1
                except (TypeError, ValueError) as exc:
                    skipped += 1
                    self.log.warning("ProxyFetch - %s: skip node: %s" % (self.fetcher_class.name, exc))
        except Exception as exc:
            self.log.error("ProxyFetch - %s: error: %s" % (self.fetcher_class.name, exc))
            self._report(count, exc, parsed, skipped)
            return
        self._report(count, None, parsed, skipped)


class Fetcher:
    name = "fetcher"

    def __init__(self):
        self.conf = ConfigHandler()

    def run(self, source_callback=None):
        node_dict = {}
        node_lock = Lock()
        threads = []
        classes = _discover_fetchers(self.conf.fetcherExclude)
        _logger.info("ProxyFetch: active fetchers [%s]" % ", ".join(cls.name for cls in classes))
        for fetcher_class in classes:
            thread = _ThreadFetcher(fetcher_class, node_dict, source_callback, node_lock)
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        return list(node_dict.values())
