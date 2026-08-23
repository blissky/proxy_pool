"""TheSpeedX SOCKS5 proxy source."""

import re

from fetcher.baseFetcher import BaseFetcher
from handler.logHandler import LogHandler
from util.webRequest import WebRequest


logger = LogHandler("fetcher")

PROXY_RE = re.compile(
    r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3}):(\d{1,5})$"
)


def _valid_proxy(value):
    match = PROXY_RE.match(value.strip())
    if not match:
        return None
    octets = [int(item) for item in match.groups()[:4]]
    port = int(match.group(5))
    if any(octet > 255 for octet in octets) or not 1 <= port <= 65535:
        return None
    return "{}.{}.{}.{}:{}".format(*octets, port)


class TheSpeedXFetcher(BaseFetcher):
    """Fetch SOCKS5 endpoints from TheSpeedX."""

    name = "TheSpeedX"
    url = "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt"
    proxy_type = "socks"

    def fetch(self):
        try:
            response = WebRequest().get(self.url, timeout=20, retry_time=1)
            for candidate in self.parseProxiesFromText(response.text):
                proxy = _valid_proxy(candidate)
                if proxy:
                    yield proxy
        except Exception as exc:
            logger.error("ProxyFetch - %s: %s" % (self.name, exc))


if __name__ == "__main__":
    for proxy in TheSpeedXFetcher().fetch():
        print(proxy)
