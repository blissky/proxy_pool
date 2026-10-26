"""体量较小、每日或每小时刷新的订阅源。

Pawdroid/Free-servers 已由 free_servers.py 提供，这里不重复接入。
"""

from fetcher.baseFetcher import BaseFetcher
from fetcher.sources._premium import fetch_urls


class FreshNoMoreWallsFetcher(BaseFetcher):
    name = "fresh_nomorewalls"
    url = "https://github.com/peasoft/NoMoreWalls"
    enabled = True

    def fetch(self):
        yield from fetch_urls(
            ["https://raw.githubusercontent.com/peasoft/NoMoreWalls/master/list.txt"],
            self.name, limit=400,
        )


class FreshErmaoziFetcher(BaseFetcher):
    name = "fresh_ermaozi"
    url = "https://github.com/ermaozi/get_subscribe"
    enabled = True

    def fetch(self):
        yield from fetch_urls(
            ["https://raw.githubusercontent.com/ermaozi/get_subscribe/main/subscribe/v2ray.txt"],
            self.name, limit=400,
        )


class FreshOpenProxyListFetcher(BaseFetcher):
    name = "fresh_openproxylist"
    url = "https://github.com/roosterkid/openproxylist"
    enabled = True

    def fetch(self):
        yield from fetch_urls(
            ["https://raw.githubusercontent.com/roosterkid/openproxylist/main/V2RAY_RAW.txt"],
            self.name, limit=300,
        )


class FreshMhdiTaheriFetcher(BaseFetcher):
    name = "fresh_mhditaheri"
    url = "https://github.com/MhdiTaheri/V2rayCollector"
    enabled = True

    def fetch(self):
        yield from fetch_urls(
            ["https://raw.githubusercontent.com/MhdiTaheri/V2rayCollector/main/sub/mix"],
            self.name, limit=400,
        )
