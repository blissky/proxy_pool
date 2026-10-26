"""Shared helpers for public aggregator subscriptions refreshed by upstream CI.

* 单个 URL 下载失败不拖垮整个来源，全部失败才 raise；
* 解析复用 ``_subscription.iter_nodes``，坏条目记录日志后跳过；
* 按 ``node_id`` 去重，再按协议权重截断，避免大源把检测队列撑爆。
"""

from core.node_parser import decode_base64_text
from fetcher.sources._subscription import iter_nodes
from handler.logHandler import LogHandler
from util.webRequest import WebRequest

_PROTOCOL_RANK = {"ss": 0, "vless": 0, "trojan": 1, "vmess": 2, "hysteria2": 3}
# 截断时保持协议多样性：单纯按优先级截断会让大源退化成「全 ss」。
_WEIGHT = {"ss": 3, "vless": 3, "trojan": 2, "vmess": 2, "hysteria2": 1}


def decode_body(text):
    """Return plain URI text, decoding a Base64 subscription when needed."""
    raw = (text or "").strip()
    if not raw or "://" in raw:
        return raw
    try:
        return decode_base64_text(raw)
    except ValueError:
        return raw


def truncate(nodes, limit):
    """Trim to ``limit`` while keeping a weighted mix of protocols."""
    if not limit or len(nodes) <= limit:
        return nodes
    groups = {}
    for node in nodes:
        groups.setdefault(node.protocol, []).append(node)
    queues = [
        (iter(groups[name]), _WEIGHT.get(name, 1))
        for name in sorted(groups, key=lambda name: _PROTOCOL_RANK.get(name, 7))
    ]
    picked = []
    while True:
        for queue, weight in queues:
            for node in queue:
                picked.append(node)
                if len(picked) >= limit:
                    return picked
                weight -= 1
                if not weight:
                    break


def fetch_urls(urls, source, limit=0, per_url=0, timeout=60, retry=2):
    """Download several subscription URLs and return deduplicated nodes.

    ``per_url`` 给每个 URL 单独配额：分协议分片源不能先到先得，
    否则第一个分片会吃光全部名额。
    """
    log = LogHandler("fetcher")
    nodes, seen, errors = [], set(), []
    for url in urls:
        try:
            text = WebRequest().get(url, retry_time=retry, timeout=timeout).text
        except Exception as exc:
            errors.append("{}: {}".format(url, exc))
            log.error("ProxyFetch - {} download failed: {}: {}".format(source, url, exc))
            continue
        part = []
        for node in iter_nodes(decode_body(text), source, log):
            if node.node_id not in seen:
                seen.add(node.node_id)
                part.append(node)
        nodes.extend(truncate(part, per_url))
    if not nodes and errors:
        raise RuntimeError("{}: all {} url(s) failed ({})".format(
            source, len(urls), "; ".join(errors),
        ))
    nodes = truncate(nodes, limit)
    log.info("ProxyFetch - {}: {} node(s) from {} url(s)".format(source, len(nodes), len(urls)))
    return nodes
