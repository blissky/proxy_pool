"""高频刷新的大型聚合源。"""

from fetcher.baseFetcher import BaseFetcher
from fetcher.sources._premium import fetch_urls


class FreshBarryFarFetcher(BaseFetcher):
    name = "fresh_barry_far"
    url = "https://github.com/barry-far/V2ray-Config"
    enabled = True

    def fetch(self):
        yield from fetch_urls(
            ["https://raw.githubusercontent.com/barry-far/V2ray-Config/main/All_Configs_Sub.txt"],
            self.name, limit=900,
        )


class FreshMahdiBlandFetcher(BaseFetcher):
    name = "fresh_mahdibland"
    url = "https://github.com/mahdibland/V2RayAggregator"
    enabled = True

    def fetch(self):
        yield from fetch_urls(
            ["https://raw.githubusercontent.com/mahdibland/V2RayAggregator/master/sub/sub_merge.txt"],
            self.name, limit=900,
        )


class FreshEternityFetcher(BaseFetcher):
    name = "fresh_eternity"
    url = "https://github.com/mahdibland/ShadowsocksAggregator"
    enabled = True

    def fetch(self):
        yield from fetch_urls(
            ["https://raw.githubusercontent.com/mahdibland/ShadowsocksAggregator/master/Eternity.txt"],
            self.name, limit=400,
        )


class FreshTgParseFetcher(BaseFetcher):
    name = "fresh_tgparse"
    url = "https://github.com/Surfboardv2ray/TGParse"
    enabled = True

    def fetch(self):
        yield from fetch_urls(
            ["https://raw.githubusercontent.com/Surfboardv2ray/TGParse/main/splitted/mixed"],
            self.name, limit=800,
        )
