# ProxyPool

这是一个以 Redis 为唯一节点数据库、以 sing-box 为多协议出口层的代理池。Web 控制台和 8082 转发入口在同一个容器中运行，支持 HTTP、SOCKS、Shadowsocks、Trojan、VLESS、VMess 和 Hysteria2 节点。

## 启动

```bash
docker compose pull
docker compose up -d
docker compose ps
```

Compose 只拉取 GitHub Container Registry 中的 `ghcr.io/blissky/proxy_pool:latest`，不会在本地构建镜像。sing-box 由 Dockerfile 从官方签名 APT 源安装固定版本，仓库不保存二进制文件。

部署前必须在 `docker-compose.yml` 中把 `FRONT_PROXY` 设置为可用的前置代理。该值为空或连接失败时，抓取和同步任务会明确失败，不会回退到服务器直连。

| 地址 | 用途 |
| --- | --- |
| `http://127.0.0.1:8083` | Web 控制台 |
| `127.0.0.1:8082` | HTTP/SOCKS5 对外代理入口 |

首次打开 Web 控制台时需要输入 Compose 中 `WEBUI_ACCESS_TOKEN`配置的 Access Token。默认值为 `sk-change-me`，仅用于首次启动，正式部署必须更换。登录状态保存在服务端内存中，连续 30 分钟没有键盘、鼠标、触摸、滚动或控制操作后需要重新登录；容器重启后所有会话也会失效。

会话 Cookie 使用 `HttpOnly`和`SameSite=Strict`，Access Token 不保存在浏览器存储中。默认部署使用明文 HTTP，在不可信网络中应通过 HTTPS 反向代理访问 8083，否则 Access Token 和会话仍可能被旁路读取。8082 代理入口不使用此 WebUI Access Token。

8082 只连接当前正式 sing-box 的本地 mixed 端口。Redis 节点选择、本地认证路由、协议转换和远端连接由同步管理器与 sing-box 协作完成。没有可用节点或 sing-box 未就绪时不会直连目标站点。

## 抓取与同步流程

```text
代理源
  -> 节点解析、规范化和跨来源去重
  -> 新节点以 synced=false 写入 Redis

Redis 节点快照
  -> 一个检测 sing-box 承载全部节点的认证选路
  -> 并发 HTTP 检测和严格 TLS 证书检测
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
HTTP_URL 检测失败
  -> 节点不可用，进入本轮待删除集合
HTTP_URL 检测成功、HTTPS_URL 严格证书检测失败
  -> 节点可用，但 tls=false
HTTP_URL 和 HTTPS_URL 均成功
  -> 节点可用，且 tls=true
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

## 前置代理

前置代理用于下载代理源和 sing-box 访问远端节点：

```text
代理池服务器 -> 前置代理 -> 远端节点 -> 目标站点
```

在 `docker-compose.yml` 的 `FRONT_PROXY` 环境变量中设置：

```dotenv
FRONT_PROXY=socks5://user:password@host:1080
```

支持 `http://`、`https://`、`socks4://`、`socks4a://`、`socks5://` 和 `socks5h://`。8082 到本地 sing-box mixed 端口的连接不经过前置代理，避免形成代理环路。

前置代理是外连安全边界，不是可选加速配置。代理源下载、检测节点连接和正式节点连接全部经过它；未配置或连接失败时禁止直连，以免向代理源或远端节点暴露代理池服务器 IP。

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
| `HTTP_URL` | 判断代理是否可用的 HTTP 检测地址 |
| `HTTPS_URL` | 检测 TLS 支持的 HTTPS 地址，启用证书校验 |
| `VERIFY_TIMEOUT` | 单个检测地址的访问超时时间，单位秒 |
| `SING_BOX_CHECK_CONCURRENCY` | 单个检测 sing-box 上同时探测的节点数，默认 16 |
| `SING_BOX_BINARY` | sing-box 命令路径，默认 `sing-box` |
| `SING_BOX_RUNTIME_DIR` | sing-box 配置和运行目录 |
| `FRONT_PROXY` | 抓取、检测和远端节点访问强制使用的前置代理；不能为空 |
| `DATA_DIR`、`CONFIG_FILE` | Web 配置和运行数据目录 |

## 本地检查

```bash
python -m py_compile proxy_service.py core/*.py fetcher/sources/*.py
python main.py --help
```

容器部署验收应确认：8082 和 8083 正常、5010 不再监听；抓取与同步分别计时且互不触发；每轮只有一个检测 sing-box 运行进程；检测请求并发不超过配置值；同步期间旧 sing-box 继续服务；新实例失败时旧实例和 Redis 同步状态保持不变。
