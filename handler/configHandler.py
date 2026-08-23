"""Environment-backed configuration for the integrated proxy service."""

import os

import setting
from front_proxy import resolve_front_proxy


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
    def verifyTimeout(self):
        return int(os.getenv("VERIFY_TIMEOUT", setting.VERIFY_TIMEOUT))

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
