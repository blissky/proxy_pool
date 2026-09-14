# ProxyPool

这是一个以 Redis 为唯一节点数据库、以 sing-box 为多协议出口层的代理池。Web 控制台和 8082 转发入口在同一个容器中运行，支持 HTTP、SOCKS、Shadowsocks、Trojan、VLESS、VMess 和 Hysteria2 节点。

## 启动

```bash
docker compose pull
docker compose up -d
docker compose ps
```

Compose 只拉取 GitHub Container Registry 中的 `ghcr.io/blissky/proxy_pool:latest`，不会在本地构建镜像。sing-box 由 Dockerfile 从官方签名 APT 源安装固定版本，仓库不保存二进制文件。

`FRONT_PROXY` 是可选配置。该值为空时，代理源下载、节点检测和正式节点连接均从服务器直接发起；配置后，这三条链路统一经过指定的前置代理。

| 地址 | 用途 |
| --- | --- |
| `http://127.0.0.1:8083` | Web 控制台 |
| `http://127.0.0.1:8083/api/proxies?token=<TOKEN>` | 只读节点订阅导出（默认导出带账号密码的 8082 出口链接） |
| `127.0.0.1:8082` | HTTP/SOCKS5 对外代理入口（支持带账号密码锁定单条出口） |

首次打开 Web 控制台时需要输入 Compose 中 `WEBUI_ACCESS_TOKEN`配置的 Access Token。默认值为 `sk-change-me`，仅用于首次启动，正式部署必须更换。登录状态保存在服务端内存中，连续 30 分钟没有键盘、鼠标、触摸、滚动或控制操作后需要重新登录；容器重启后所有会话也会失效。

会话 Cookie 使用 `HttpOnly`和`SameSite=Strict`，Access Token 不保存在浏览器存储中。默认部署使用明文 HTTP，在不可信网络中应通过 HTTPS 反向代理访问 8083，否则 Access Token 和会话仍可能被旁路读取。8082 代理入口不使用此 WebUI Access Token。

8082 只连接当前正式 sing-box 的本地 mixed 端口。Redis 节点选择、本地认证路由、协议转换和远端连接由同步管理器与 sing-box 协作完成。没有可用节点或 sing-box 未就绪时不会直连目标站点。

8082 支持两种调用方式：

- **不带账号密码**：沿用原有的自动轮询，由服务端在该 revision 的可用节点中随机挑一条出口；
- **带账号密码**：客户端在 HTTP `Proxy-Authorization` 或 SOCKS5 用户名密码认证中携带某一节点的凭据时，该请求被固定到这一条出口（一组账号密码 = 一条出口）。凭据直接复用 sing-box 的 per-node 凭据，因此凭据不匹配任何活动节点时会被拒绝（HTTP 407 / SOCKS5 认证失败），不会回落到其他出口。

## 抓取与同步流程

```text
代理源
  -> 节点解析、规范化和跨来源去重
  -> 新节点以 synced=false 写入 Redis

Redis 节点快照
  -> 一个检测 sing-box 承载全部节点的认证选路
  -> 并发执行 HTTPS 优先检测，失败时回退 HTTP 检测
  -> 暂存本轮可用节点和待删除节点
  -> 生成并启动新正式 sing-box
  -> 新实例就绪后原子切换 8082 和 Redis revision
  -> 删除本轮确认失效的快照节点
  -> 关闭旧实例
```

抓取和同步是两条独立链路。容器启动时先完成首次抓取，再开始首次同步；随后分别按 `FETCH_INTERVAL_SECONDS` 和 `CHECK_INTERVAL_SECONDS` 调度，间隔均从对应任务完成后开始计时。抓取不会触发检测，同步也不会重新抓取。

检测期间发生的新抓取可以写入 Redis，新节点保持 `synced=false` 并等待下一轮检测。同步只提交任务开始时的快照，通过字段级合并保留并行抓取更新的来源信息，并使用批次 epoch 防止较早启动的抓取任务复活本轮已经确认失效的节点。

每轮检测只运行一个检测用 sing-box 进程。所有待检测节点配置为独立 outbound，并通过本地 mixed 入站的用户名和密码选择指定节点。`SING_BOX_CHECK_CONCURRENCY` 只控制同时发出的节点检测请求数量，不代表 sing-box 进程数，默认值为 16。

```text
HTTPS_URL 严格证书检测成功
  -> 节点可用，且 tls=true，不再检测 HTTP_URL
HTTPS_URL 检测失败、HTTP_URL 检测成功
  -> 节点可用，但 tls=false
HTTPS_URL 和 HTTP_URL 均检测失败
  -> 节点不可用，进入本轮待删除集合
```

待删除集合只会在新正式实例成功启动、切换和提交后生效。任何系统性检测错误、新实例启动失败或 Redis 提交失败都会保留旧正式实例及旧 Redis 激活状态。

Web 控制台提供：

- 独立的“立即抓取”和“立即检测并同步”操作；
- 独立的抓取、检测、配置生成和切换进度及倒计时；
- sing-box 运行状态、active 端口和配置版本；
- Redis 连接状态和节点统计；
- 已入库节点、支持 TLS 节点和当前启用的代理源数量；
- 来源抓取日志和节点列表；
- 节点的协议、TLS 支持状态、同步状态、来源和最近检测时间。

## 8082 账号密码出口（一组账号密码 = 一条出口）

导出或从 Web 控制台拿到某节点凭据后，直接把它拼进代理 URL 即可锁定出口：

```bash
# 带账号密码（推荐给需要“一账号一出口”的调用方，例如注册工具）
curl -x "http://u-xxxx:p-yyyy@127.0.0.1:8082" https://api.ipify.org
# SOCKS5 同理
curl -x "socks5h://u-xxxx:p-yyyy@127.0.0.1:8082" https://api.ipify.org
```

客户端凭据的判定规则（固定行为，无开关）：

| 协议 | 带凭据 | 不带凭据 |
| --- | --- | --- |
| HTTP | 按凭据锁定出口，凭据无效返回 407 | 沿用自动轮询 |
| SOCKS5 | 客户端只宣告 0x02 时使用凭据 | 宣告 0x00（或同时宣告 0x00/0x02）时保持匿名轮询 |

固定出口的请求失败时同样会累计该节点的运行时失败计数，达到 `FAIL_THRESHOLD` 后节点被移出运行时选择。

## 节点订阅导出（`/api/proxies`）

`GET /api/proxies?token=<WEBUI_ACCESS_TOKEN>` 是只读导出接口，唯一凭据是查询参数 `token`（等于 `WEBUI_ACCESS_TOKEN`），缺失或错误返回 401。服务端屏蔽请求日志，token 不会进入日志或响应体。

| 参数 | 取值 | 说明 |
| --- | --- | --- |
| `token` | `WEBUI_ACCESS_TOKEN` | 必填 |
| `tls_only` | `1`/`true`/`yes`/`on` | 只导出通过严格 HTTPS 校验（`tls=true`）的节点；其他取值返回 400，空池返回 200 与空正文 |
| `format` | `endpoint`（默认）/ `share` | `endpoint` 导出带账号密码的 8082 出口链接；`share` 导出节点分享链接（直连远端节点，不经过 8082 与本地 sing-box） |

```bash
# 默认：一行一个带账号密码的 8082 出口链接，可直接导入只接受 http/socks5 的调用方
curl -s "http://127.0.0.1:8083/api/proxies?token=sk-change-me"
# http://u-xxxx:p-yyyy@127.0.0.1:8082

# 只导出通过严格 HTTPS 校验的节点
curl -s "http://127.0.0.1:8083/api/proxies?token=sk-change-me&tls_only=1"

# 兼容：导出节点分享链接（vless://、hysteria2:// 等）
curl -s "http://127.0.0.1:8083/api/proxies?token=sk-change-me&format=share"
```

导出链接的地址部分由环境变量决定，便于按部署形态切换（同一台机器的 127.0.0.1、宿主 IP、域名或 Compose 容器名）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `EXPORT_PROXY_HOST` | `127.0.0.1` | 导出链接主机，可填 IP、域名或容器名（如 `proxy_pool`） |
| `EXPORT_PROXY_SCHEME` | `http` | 导出链接协议：`http`、`https`、`socks5`、`socks5h` |

导出端口恒为监听端口 `PROXY_PORT`（默认 8082），不支持单独覆盖。

`format=endpoint` 只导出当前正式 sing-box 已加载 revision 的节点，避免给出运行实例不认的凭据；同步切换途中若过滤后为空则回落到全部可用节点，防止订阅方把整池误判为失效。`format=share` 的分享链接是直连远端节点，不受 revision 限制。

## 前置代理

前置代理可用于下载代理源和 sing-box 访问远端节点：

```text
代理池服务器 -> 前置代理 -> 远端节点 -> 目标站点
```

在 `docker-compose.yml` 的 `FRONT_PROXY` 环境变量中设置：

```dotenv
FRONT_PROXY=socks5://user:password@host:1080
```

支持 `http://`、`https://`、`socks4://`、`socks4a://`、`socks5://` 和 `socks5h://`。8082 到本地 sing-box mixed 端口的连接不经过前置代理，避免形成代理环路。

配置 `FRONT_PROXY` 后，代理源下载、检测节点连接和正式节点连接全部经过它；连接失败时任务或节点连接直接失败，不会绕过已配置的前置代理。该值为空时使用服务器直连，因此代理源和远端节点可以看到代理池服务器的出口 IP。无论是否配置前置代理，未匹配到节点的请求仍由 sing-box 的最终 `block` 路由拒绝，不会直接连接客户端请求的目标站点。

## Compose 环境变量

| 变量 | 说明 |
| --- | --- |
| `DB_CONN` | Redis 连接 URI |
| `PROXY_LISTEN`、`PROXY_PORT` | 8082 监听地址和端口 |
| `STATS_PORT` | Web UI 端口，默认 8083 |
| `PROXY_TIMEOUT` | 客户端握手、上游连接和阻塞写入超时，单位秒，默认 5；不限制已建立隧道的总时长或 AI 响应等待时间 |
| `FAIL_THRESHOLD` | 节点连续连接失败后的运行时熔断阈值，默认 2；成功建立一次连接会清零该节点的失败计数 |
| `WEBUI_ACCESS_TOKEN` | Web 控制台 Access Token，默认 `sk-change-me`，正式部署必须更换 |
| `WEBUI_SESSION_TIMEOUT_SECONDS` | Web 控制台无用户操作后的会话过期时间，单位秒，默认 1800 |
| `FETCH_INTERVAL_SECONDS` | 抓取完成后的代理源刷新间隔，单位秒，默认 21600 |
| `CHECK_INTERVAL_SECONDS` | 同步完成后的可用性检测间隔，单位秒，默认 3600 |
| `HTTP_URL` | HTTPS 检测失败后判断代理是否仍可用的 HTTP 回退地址 |
| `HTTPS_URL` | 优先检测 TLS 支持的 HTTPS 地址，启用证书校验；成功后不再检测 HTTP |
| `VERIFY_TIMEOUT` | 单个检测地址的访问超时时间，单位秒 |
| `SING_BOX_CHECK_CONCURRENCY` | 单个检测 sing-box 上同时探测的节点数，默认 16 |
| `SING_BOX_BINARY` | sing-box 命令路径，默认 `sing-box` |
| `SING_BOX_RUNTIME_DIR` | sing-box 配置和运行目录 |
| `FRONT_PROXY` | 抓取、检测和远端节点访问使用的可选前置代理；为空时直连 |
| `EXPORT_PROXY_HOST` | `/api/proxies` 导出链接主机，默认 `127.0.0.1`，可填 IP、域名或容器名 |
| `EXPORT_PROXY_SCHEME` | `/api/proxies` 导出链接协议，默认 `http` |
| `DATA_DIR`、`CONFIG_FILE` | Web 配置和运行数据目录 |

## 本地检查

```bash
python -m py_compile proxy_service.py core/*.py fetcher/sources/*.py
python -m unittest discover -s tests -t .
python main.py --help
```

容器部署验收应确认：8082 和 8083 正常、5010 不再监听；抓取与同步分别计时且互不触发；每轮只有一个检测 sing-box 运行进程；检测请求并发不超过配置值；同步期间旧 sing-box 继续服务；新实例失败时旧实例和 Redis 同步状态保持不变。
