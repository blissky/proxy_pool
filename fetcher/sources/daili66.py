# -*- coding: utf-8 -*-
"""
-------------------------------------------------
   File Name：     daili66.py
   Description :   66代理
   Author :        JHao
   date：          2026/06/08
-------------------------------------------------
   Change Activity:
                   2026/06/08:
-------------------------------------------------
"""
__author__ = 'JHao'

from fetcher.baseFetcher import BaseFetcher
from util.webRequest import WebRequest


class DaiLi66Fetcher(BaseFetcher):
    """66代理 https://www.66daili.com"""

    name = "daili66"
    url = "https://www.66daili.com"

    enabled = True

    def fetch(self):
        url = "http://api.66daili.com/?format=json"
        r = WebRequest().get(url, timeout=10)
        payload = r.json
        proxies = payload.get("data")
        if not isinstance(proxies, list):
            raise RuntimeError("daili66 API error {}: {}".format(
                payload.get("code", "unknown"), payload.get("message", "invalid response")
            ))
        for each in proxies:
            yield "%s:%s" % (each["ip"], each["port"])



if __name__ == '__main__':
    for proxy in DaiLi66Fetcher().fetch():
        print(proxy)
