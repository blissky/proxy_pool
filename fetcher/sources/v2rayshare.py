"""Dated Base64 subscription source from static.v2rayshare.net."""

from fetcher.baseFetcher import BaseFetcher
from handler.logHandler import LogHandler
from util.webRequest import WebRequest

from fetcher.sources._subscription import current_date, decode_subscription, iter_nodes


class V2rayShareFetcher(BaseFetcher):
    name = "v2rayshare"
    url = "https://static.v2rayshare.net"
    enabled = True

    def fetch(self):
        log = LogHandler("fetcher")
        day = current_date()
        url = "https://static.v2rayshare.net/{}/{}/{}.txt".format(
            day.strftime("%Y"), day.strftime("%m"), day.strftime("%Y%m%d"),
        )
        response = WebRequest().get(url, retry_time=2, timeout=15)
        decoded = decode_subscription(response.text)
        found = 0
        for node in iter_nodes(decoded, self.name, log):
            found += 1
            yield node
        if not found:
            log.info("ProxyFetch - {} succeeded with 0 nodes: {}".format(
                self.name, url,
            ))
