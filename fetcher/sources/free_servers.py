"""Base64 subscription source from Pawdroid/Free-servers."""

from core.node_parser import decode_base64_text, extract_uris, parse_node_uri
from fetcher.baseFetcher import BaseFetcher
from handler.logHandler import LogHandler
from util.webRequest import WebRequest


class FreeServersFetcher(BaseFetcher):
    name = "free-servers"
    url = "https://raw.githubusercontent.com/Pawdroid/Free-servers/main/sub"
    proxy_type = "http"

    def fetch(self):
        log = LogHandler("fetcher")
        response = WebRequest().get(self.url, retry_time=2, timeout=15)
        raw = response.text.strip()
        try:
            decoded = decode_base64_text(raw)
        except ValueError:
            decoded = raw
        for uri in extract_uris(decoded):
            try:
                yield parse_node_uri(uri, self.name)
            except (TypeError, ValueError, UnicodeError) as exc:
                log.warning("ProxyFetch - %s: skip node: %s" % (self.name, exc))

