"""Epodonios/v2ray-configs，上游 GitHub Actions 每 30 分钟刷新。"""

from fetcher.baseFetcher import BaseFetcher
from fetcher.sources._premium import fetch_urls

_BASE = "https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/"


class FreshEpodoniosAllFetcher(BaseFetcher):
    """全量明文订阅（vless/vmess/trojan/ss 混合）。"""

    name = "fresh_epodonios"
    url = "https://github.com/Epodonios/v2ray-configs"
    enabled = True

    def fetch(self):
        yield from fetch_urls([_BASE + "All_Configs_Sub.txt"], self.name, limit=900)


class FreshEpodoniosSplitFetcher(BaseFetcher):
    """分协议文件，每个分片单独配额，避免 vless.txt 吃光名额。"""

    name = "fresh_epodonios_split"
    url = "https://github.com/Epodonios/v2ray-configs"
    enabled = True

    def fetch(self):
        urls = [_BASE + "Splitted-By-Protocol/{}.txt".format(name)
                for name in ("vless", "trojan", "vmess", "ss")]
        yield from fetch_urls(urls, self.name, limit=700, per_url=200)
