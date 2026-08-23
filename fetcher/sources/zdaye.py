# -*- coding: utf-8 -*-
"""
-------------------------------------------------
   File Name：     zdaye.py
   Description :   站大爷代理源
   Author :        JHao
   date：          2026/5/31
-------------------------------------------------
   Change Activity:
                   2026/05/31:
-------------------------------------------------
"""
__author__ = 'JHao'

from time import sleep
from urllib.parse import urljoin

from fetcher.baseFetcher import BaseFetcher
from util.webRequest import WebRequest


class ZdayeFetcher(BaseFetcher):
    """站大爷 https://www.zdaye.com/dayProxy.html"""

    name = "zdaye"
    url = "https://www.zdaye.com/dayProxy.html"

    def fetch(self):
        start_url = "https://www.zdaye.com/free/"
        html_tree = WebRequest().get(start_url, verify=False).tree
        links = html_tree.xpath("//h3[@class='thread_title']/a/@href") if html_tree is not None else []
        if not links:
            raise RuntimeError("zdaye proxy-list link was not found")
        target_url = urljoin(start_url, links[0].strip())
        while target_url:
            tree = WebRequest().get(target_url, verify=False).tree
            for tr in tree.xpath("//table//tr"):
                ip = "".join(tr.xpath("./td[1]/text()")).strip()
                port = "".join(tr.xpath("./td[2]/text()")).strip()
                if ip and port:
                    yield "%s:%s" % (ip, port)
            next_page = tree.xpath("//div[@class='page']/a[@title='下一页']/@href")
            target_url = urljoin(target_url, next_page[0].strip()) if next_page else ""
            if target_url:
                sleep(5)


if __name__ == '__main__':
    for proxy in ZdayeFetcher().fetch():
        print(proxy)
