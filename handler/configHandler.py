"""Environment-backed configuration for the integrated proxy service."""

import os
import re

import setting
from front_proxy import resolve_front_proxy

STATUS_ITEM = re.compile(r"([0-9]{3})(?:\s*-\s*([0-9]{3}))?", re.ASCII)


def parse_status_codes(raw, name):
    codes = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        match = STATUS_ITEM.fullmatch(item)
        if not match:
            raise ValueError("{} 格式错误：{}".format(name, item))
        low = int(match.group(1))
        high = int(match.group(2) or low)
        if not 100 <= low <= high <= 999:
            raise ValueError("{} 状态码须在 100-999 且区间不能倒序：{}".format(name, item))
        codes.update(range(low, high + 1))
    return frozenset(codes)


class ConfigHandler:
    @property
    def dbConn(self):
        return os.getenv("DB_CONN", setting.DB_CONN)

    @property
    def tableName(self):
        return os.getenv("TABLE_NAME", setting.TABLE_NAME)

    @property
    def frontProxy(self):
        return resolve_front_proxy(getattr(setting, "FRONT_PROXY", ""))

    @property
    def fetcherExclude(self):
        raw = os.getenv("PROXY_FETCHER_EXCLUDE", "")
        if raw.strip():
            return [item.strip() for item in raw.split(",") if item.strip()]
        return list(getattr(setting, "PROXY_FETCHER_EXCLUDE", []))

    @property
    def httpUrl(self):
        return os.getenv("HTTP_URL", setting.HTTP_URL)

    @property
    def httpsUrl(self):
        return os.getenv("HTTPS_URL", setting.HTTPS_URL)

    @property
    def httpStatusBlacklist(self):
        return parse_status_codes(
            os.getenv("HTTP_STATUS_BLACKLIST", setting.HTTP_STATUS_BLACKLIST),
            "HTTP_STATUS_BLACKLIST",
        )

    @property
    def httpsStatusBlacklist(self):
        return parse_status_codes(
            os.getenv("HTTPS_STATUS_BLACKLIST", setting.HTTPS_STATUS_BLACKLIST),
            "HTTPS_STATUS_BLACKLIST",
        )

    @property
    def verifyTimeout(self):
        return max(1, int(os.getenv("VERIFY_TIMEOUT", setting.VERIFY_TIMEOUT)))

    @property
    def timezone(self):
        return os.getenv("TIMEZONE", setting.TIMEZONE)

    @property
    def fetchIntervalSeconds(self):
        return max(1, int(os.getenv("FETCH_INTERVAL_SECONDS", setting.FETCH_INTERVAL_SECONDS)))

    @property
    def checkIntervalSeconds(self):
        return max(1, int(os.getenv("CHECK_INTERVAL_SECONDS", setting.CHECK_INTERVAL_SECONDS)))

    @property
    def singBoxBinary(self):
        return os.getenv("SING_BOX_BINARY", setting.SING_BOX_BINARY)

    @property
    def singBoxRuntimeDir(self):
        return os.getenv("SING_BOX_RUNTIME_DIR", setting.SING_BOX_RUNTIME_DIR)

    @property
    def singBoxCheckConcurrency(self):
        return max(1, int(os.getenv(
            "SING_BOX_CHECK_CONCURRENCY", setting.SING_BOX_CHECK_CONCURRENCY,
        )))
