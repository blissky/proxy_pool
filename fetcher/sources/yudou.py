"""Dated Base64 subscription source published daily by yudou789.top.

The subscription file name carries a random suffix
(``https://www.yudou89.shop/YYYYMM/YYYYMMDD<suffix>.txt``), so it cannot be
built from the date. The source reads the yudou789.top index, picks the post for
today or, before today's post is published, the latest earlier post, and takes
the subscription link from that post's HTML.
"""

import re
from datetime import date

from fetcher.baseFetcher import BaseFetcher
from handler.logHandler import LogHandler
from util.webRequest import WebRequest

from fetcher.sources._subscription import current_date, decode_subscription, iter_nodes


POST_PATTERN = re.compile(
    r'<a[^>]+href="(https://www\.yudou789\.top/\d+\.html)"[^>]*>\s*'
    r"(\d{4})年(\d{1,2})月(\d{1,2})日免费精选节点"
)
SUBSCRIPTION_PATTERN = re.compile(r"https://www\.yudou89\.shop/(\d{6})/(\d{8})\w+\.txt")


def latest_post(index_html, today):
    """Return ``(post_url, post_date)`` for today or the latest earlier day."""
    posts = {}
    for url, year, month, day in POST_PATTERN.findall(index_html):
        try:
            posted = date(int(year), int(month), int(day))
        except ValueError:
            continue
        if posted <= today:
            posts.setdefault(posted, url)
    if not posts:
        raise ValueError("no dated node post found on index page")
    posted = max(posts)
    return posts[posted], posted


def subscription_url(post_html, posted):
    """Return the post's Base64 subscription link for ``posted``."""
    stamp = posted.strftime("%Y%m%d")
    for match in SUBSCRIPTION_PATTERN.finditer(post_html):
        if match.group(1) == stamp[:6] and match.group(2) == stamp:
            return match.group(0)
    raise ValueError("subscription link for {} not found".format(stamp))


class YudouFetcher(BaseFetcher):
    name = "yudou"
    url = "https://www.yudou789.top/"
    enabled = True

    def fetch(self):
        log = LogHandler("fetcher")
        today = current_date()
        index = WebRequest().get(self.url, retry_time=2, timeout=15)
        post_url, posted = latest_post(index.response.content.decode("utf-8", "replace"), today)
        if posted != today:
            log.info("ProxyFetch - {} has no post for {} yet, using {}".format(
                self.name, today.isoformat(), posted.isoformat(),
            ))
        post = WebRequest().get(post_url, retry_time=2, timeout=15)
        url = subscription_url(post.response.content.decode("utf-8", "replace"), posted)
        response = WebRequest().get(url, retry_time=2, timeout=15)
        decoded = decode_subscription(response.text)
        found = 0
        for node in iter_nodes(decoded, self.name, log):
            found += 1
            yield node
        if not found:
            log.info("ProxyFetch - {} succeeded with 0 nodes: {}".format(self.name, url))
