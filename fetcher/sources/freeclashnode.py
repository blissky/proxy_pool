"""Dated multi-file Base64 subscription source from freeclashnode.com."""

from fetcher.baseFetcher import BaseFetcher
from handler.logHandler import LogHandler
from util.webRequest import WebRequest

from fetcher.sources._subscription import current_date, decode_subscription, iter_nodes


class FreeClashNodeFetcher(BaseFetcher):
    name = "freeclashnode"
    url = "https://node.freeclashnode.com"
    enabled = True

    def fetch(self):
        log = LogHandler("fetcher")
        day = current_date()
        date_path = "{}/{}".format(day.strftime("%Y"), day.strftime("%m"))
        date_name = day.strftime("%Y%m%d")
        succeeded = 0
        failures = []
        for index in range(5):
            url = "https://node.freeclashnode.com/uploads/{}/{}-{}.txt".format(
                date_path, index, date_name,
            )
            try:
                response = WebRequest().get(url, retry_time=2, timeout=15)
                decoded = decode_subscription(response.text)
            except Exception as exc:
                failures.append("{}: {}".format(url, exc))
                log.error("ProxyFetch - {} file failed: {}: {}".format(
                    self.name, url, exc,
                ))
                continue
            succeeded += 1
            found = 0
            for node in iter_nodes(decoded, self.name, log):
                found += 1
                yield node
            if not found:
                log.info("ProxyFetch - {} file succeeded with 0 nodes: {}".format(
                    self.name, url,
                ))
        if not succeeded:
            detail = "; ".join(failures) or "no subscription files"
            raise RuntimeError("{}: all subscription files failed ({})".format(
                self.name, detail,
            ))
