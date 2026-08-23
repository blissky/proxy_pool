# -*- coding: utf-8 -*-
"""
-------------------------------------------------
   File Name：     geonode.py
   Description :   Geonode代理源
   Author :        JHao
   date：          2026/5/31
-------------------------------------------------
   Change Activity:
                   2026/05/31:
-------------------------------------------------
"""
__author__ = 'JHao'

from fetcher.baseFetcher import BaseFetcher
from util.webRequest import WebRequest


class GeonodeFetcher(BaseFetcher):
    """Geonode Free Proxy https://geonode.com/"""

    name = "geonode"
    url = "https://geonode.com/"

    def fetch(self):
        url = ("https://proxylist.geonode.com/api/proxy-list?"
               "page=1&limit=100&sort_by=lastChecked&sort_type=desc")
        r = WebRequest().get(url, timeout=5, retry_time=1, verify=False)
        items = r.json.get("data")
        if not isinstance(items, list):
            raise RuntimeError("geonode returned an invalid response")
        proxies = []
        for item in items:
            ip = item.get("ip", "")
            port = item.get("port", "")
            if ip and port:
                proxies.append("%s:%s" % (ip, port))
        if not proxies:
            proxies = self.parseProxiesFromText(r.text)
        for proxy in self.yieldUniqueProxies(proxies):
            yield proxy


if __name__ == '__main__':
    for proxy in GeonodeFetcher().fetch():
        print(proxy)
