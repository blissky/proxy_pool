"""Default runtime configuration; environment variables take precedence."""

VERSION = "3.0.0"

DB_CONN = "redis://@127.0.0.1:6379/0"
TABLE_NAME = "use_proxy"
FRONT_PROXY = ""

PROXY_FETCHER_EXCLUDE = []

HTTP_URL = "http://httpbin.org"
HTTPS_URL = "https://www.qq.com"
HTTP_STATUS_BLACKLIST = "100-199,400-999"
HTTPS_STATUS_BLACKLIST = "100-199,400-402,404-999"
VERIFY_TIMEOUT = 10
MAX_FAIL_COUNT = 0
POOL_SIZE_MIN = 20

TIMEZONE = "Asia/Shanghai"
FETCH_INTERVAL_SECONDS = 21600
CHECK_INTERVAL_SECONDS = 3600

SING_BOX_BINARY = "sing-box"
SING_BOX_RUNTIME_DIR = "data/sing-box"
SING_BOX_CHECK_CONCURRENCY = 16
