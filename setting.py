"""Default runtime configuration; environment variables take precedence."""

VERSION = "3.0.0"

DB_CONN = "redis://@127.0.0.1:6379/0"
TABLE_NAME = "use_proxy"
FRONT_PROXY = ""

PROXY_FETCHER_EXCLUDE = []

HTTP_URL = "http://httpbin.org"
HTTPS_URL = "https://www.qq.com"
VERIFY_TIMEOUT = 10
MAX_FAIL_COUNT = 0
POOL_SIZE_MIN = 20
PROXY_REGION = False

TIMEZONE = "Asia/Shanghai"
FETCH_INTERVAL_SECONDS = 300
CHECK_INTERVAL_SECONDS = 120

SING_BOX_BINARY = "sing-box"
SING_BOX_RUNTIME_DIR = "data/sing-box"
SING_BOX_CHECK_CONCURRENCY = 4
