"""mheidari98/.proxy：单文件约 8MB，放宽超时并截断到 1200 条。"""

from fetcher.baseFetcher import BaseFetcher
from fetcher.sources._premium import fetch_urls


class FreshMhdiProxyFetcher(BaseFetcher):
    name = "fresh_mhdi_proxy"
    url = "https://github.com/mheidari98/.proxy"
    enabled = True

    def fetch(self):
        yield from fetch_urls(
            ["https://raw.githubusercontent.com/mheidari98/.proxy/main/all"],
            self.name, limit=1200, timeout=120,
        )
