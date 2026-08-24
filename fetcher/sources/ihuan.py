# -*- coding: utf-8 -*-
"""
-------------------------------------------------
   File Name：     ihuan.py
   Description :   小幻代理代理源
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


class IhuanFetcher(BaseFetcher):
    """小幻代理 https://ip.ihuan.me/"""

    name = "ihuan"
    url = "https://ip.ihuan.me/"
    enabled = True

    def fetch(self):
        request = WebRequest()
        request.get(self.url, verify=False)
        tree = request.get(self.url, verify=False).tree
        for item in tree.xpath("//table[@class='table table-hover table-bordered']//tr"):
            ip = "".join(item.xpath("./td[1]//text()")).strip()
            port = "".join(item.xpath("./td[2]//text()")).strip()
            if ip and port:
                yield "%s:%s" % (ip, port)


if __name__ == '__main__':
    for proxy in IhuanFetcher().fetch():
        print(proxy)
