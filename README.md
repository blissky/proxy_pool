# ProxyPool

基于 Redis 和 sing-box 的多协议代理池。定时从公开来源抓取节点，用 sing-box 统一检测可用性，再把可用节点发布到一个正式 sing-box 实例，对外只暴露一个 HTTP/SOCKS5 入口。

- 8082：对外代理入口，同端口支持 HTTP（含 CONNECT）和 SOCKS5。
- 8083：Web 控制台，需 Access Token 登录。
- 支持节点类型：http、socks5、ss、trojan、vless、vmess、hysteria2。

## 快速开始

```bash
docker compose pull
docker compose up -d
```

镜像为 `ghcr.io/blissky/proxy_pool:latest`，内置 Redis 和固定版本的 sing-box。示例 `docker-compose.yml` 只把 8082/8083 绑定到 `127.0.0.1`，需要外网访问时由宿主机反向代理发布。

启动前务必修改 `WEBUI_ACCESS_TOKEN`，默认值 `sk-change-me` 仅用于首次体验。8083 是明文 HTTP，在不可信网络暴露时请放在 HTTPS 反向代理之后。

数据目录挂载在 `./data`，保存 Web 配置 `config.json` 和 sing-box 运行时配置。Redis 不落盘，容器重启后节点会重新抓取。

## 工作流程

1. 抓取：并发运行 `fetcher/sources/` 下所有启用的来源，解析为结构化节点并按指纹去重。每个来源的配置先经 `sing-box check` 校验，不通过的来源整批不入库。新节点以“未同步”状态写入 Redis。
2. 检测：对 Redis 快照启动一个检测用 sing-box 进程，每个节点独立出站，并发探测 `HTTPS_URL`（严格证书校验），失败（含返回 `HTTPS_STATUS_BLACKLIST` 中的状态码）再探测 `HTTP_URL`（同样受 `HTTP_STATUS_BLACKLIST` 约束）。HTTPS 通过的节点标记为支持 TLS；两者都失败的节点从 Redis 删除。
3. 同步：用可用节点生成新的正式 sing-box 配置，启动并确认就绪后切换 8082 的上游，再关闭旧实例。新实例失败时保留旧实例；本轮无可用节点时也保留旧配置。

启动时先完成一次抓取，再开始检测同步。之后两者分别按 `FETCH_INTERVAL_SECONDS`、`CHECK_INTERVAL_SECONDS` 调度，间隔从上一次任务结束开始计算。

## 使用 8082 代理

不带认证时随机选择一个已同步节点。HTTPS（CONNECT 或 SOCKS5 目标端口 443）只会选到支持 TLS 的节点。

```bash
curl -x http://127.0.0.1:8082 https://www.bing.com -I
curl -x socks5h://127.0.0.1:8082 https://www.bing.com -I
```

带上某个节点的账号密码时固定走该节点出口，账号密码可从下方导出接口获取。凭据不匹配任何活动节点时，HTTP 返回 407，SOCKS5 认证失败。

节点连续连接失败达到 `FAIL_THRESHOLD` 次后会退出当前随机池，等待下一轮检测。

## Web 控制台

访问 `http://127.0.0.1:8083`，用 Access Token 登录。

- 立即抓取 / 立即检测并同步：手动触发对应任务，任务运行中重复点击返回 409。
- 粘贴导入：粘贴节点后导入，支持每行一个的明文节点（ss/trojan/vless/vmess/hysteria2 链接、`http://`、`socks5://`、`host:port`）或整段 Base64 订阅。导入的节点同样先经 sing-box 校验，校验失败的单个节点会被剔除，其余以“未同步”状态入库，来源记为 `manual`。
- 节点列表：查看协议、端点、TLS、同步状态、来源和检测记录，可删除单个节点。
- 实时日志：展示抓取、检测、导入和 8082 连接日志。日志不输出节点密码、UUID 等凭据。

登录会话只保存在服务端内存，空闲超过 `WEBUI_SESSION_TIMEOUT_SECONDS` 后失效，页面的自动轮询不会续期。

## 节点导出

```
GET /api/proxies?token=<WEBUI_ACCESS_TOKEN>[&format=endpoint|share][&tls_only=1]
```

- `format=endpoint`（默认）：每个可用节点一行 `scheme://用户名:密码@EXPORT_PROXY_HOST:PROXY_PORT`，使用这组凭据连接 8082 即固定该节点出口。
- `format=share`：导出远端节点的标准分享链接（v2rayN / Clash 通用格式），无法表示为分享链接的节点会跳过并记录日志。
- `tls_only=1`：只导出支持 TLS 的节点。

该接口用查询参数鉴权，Token 会出现在 URL 中，请只在可信链路上调用。

## 代理来源

新增来源时在 `fetcher/sources/` 下新建文件，继承 `BaseFetcher`，`fetch()` 产出 `host:port` 字符串或 `ProxyNode`，纯端点来源通过 `proxy_type` 声明 `http` 或 `socks`。以下划线开头的文件不会被当作来源加载。

## 配置

所有配置通过环境变量设置，括号内为代码默认值（`docker-compose.yml` 中部分值不同）。

| 变量 | 说明 |
| --- | --- |
| `DB_CONN` | Redis 连接串（`redis://@127.0.0.1:6379/0`） |
| `TABLE_NAME` | Redis 节点 hash 名（`use_proxy`） |
| `PROXY_LISTEN` / `PROXY_PORT` / `STATS_PORT` | 监听地址、8082 代理端口、8083 控制台端口 |
| `PROXY_TIMEOUT` | 客户端握手、上游连接和阻塞写入超时秒数（5），不限制已建立隧道的时长 |
| `FAIL_THRESHOLD` | 连续连接失败多少次后退出随机池（2） |
| `WEBUI_ACCESS_TOKEN` | 控制台和导出接口的 Access Token（`sk-change-me`） |
| `WEBUI_SESSION_TIMEOUT_SECONDS` | 控制台会话空闲超时（1800） |
| `FETCH_INTERVAL_SECONDS` | 抓取间隔（21600） |
| `CHECK_INTERVAL_SECONDS` | 检测同步间隔（3600） |
| `HTTPS_URL` / `HTTP_URL` | 检测目标（`https://www.qq.com` / `http://httpbin.org`） |
| `HTTPS_STATUS_BLACKLIST` / `HTTP_STATUS_BLACKLIST` | 探测状态码黑名单，命中即判为该次探测失败；英文逗号分隔，`-` 表示连续区间，范围 100-999，留空不拉黑。默认 `100-199,400-402,404-999` / `100-199,400-999`（HTTPS 放行 403，HTTP 不放行）。探测使用 HEAD 请求 |
| `VERIFY_TIMEOUT` | 单个检测请求超时秒数（10） |
| `SING_BOX_CHECK_CONCURRENCY` | 检测进程上的并发探测数（16） |
| `SING_BOX_BINARY` / `SING_BOX_RUNTIME_DIR` | sing-box 可执行文件和运行时配置目录 |
| `FRONT_PROXY` | 可选前置代理，支持 `http`、`https`、`socks4(a)`、`socks5(h)`；留空直连 |
| `EXPORT_PROXY_HOST` / `EXPORT_PROXY_SCHEME` | `/api/proxies` 导出链接的主机（`127.0.0.1`）和协议（`http`，可选 `https`、`socks5`、`socks5h`） |
| `TIMEZONE` | 按日期订阅来源使用的时区（`Asia/Shanghai`） |
| `PROXY_FETCHER_EXCLUDE` | 逗号分隔的来源类名，用于临时排除来源 |
| `CONFIG_FILE` / `DATA_DIR` | Web 配置文件路径和数据目录 |

`FRONT_PROXY` 非空时，来源下载、节点检测和正式实例访问远端节点都经过它，连接失败不会绕过；为空时三者都直连。8082 到本地 sing-box 的连接始终不走前置代理。

## 本地运行

需要 Python 3.11+、Redis 和 sing-box（可执行文件在 `PATH` 中，或用 `SING_BOX_BINARY` 指定）。

```bash
pip install -r requirements.txt
python main.py --listen 0.0.0.0 --port 8082 --stats-port 8083
```

也可以用 `bash proxy_pool.sh start [--fg]` 同时启动本机 Redis 和服务，`stop` / `restart` / `status` 管理进程。命令行参数会写入 `CONFIG_FILE`。

## 许可证

见 [LICENSE](LICENSE)。
