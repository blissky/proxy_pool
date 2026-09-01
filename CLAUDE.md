# CLAUDE.md

本项目是 Redis 代理池和 sing-box 多协议出口服务。根目录生产源码、Docker 配置和 Web UI 是维护对象；参考源码不修改，运行时生成的配置和凭据不提交。

## 架构

- `proxy_service.py` 同时提供 8082 HTTP/SOCKS5 入口、8083 Web 控制台及服务端会话认证。
- `core/node.py` 定义结构化 `ProxyNode`；Redis 以节点指纹为 hash key 保存节点及检测状态。
- `core/node_parser.py` 解析 ss、trojan、vless、vmess、hysteria2 URI 和 Base64 订阅。
- `core/store.py` 是唯一 Redis 节点存储层。
- `core/singbox.py` 负责 sing-box 配置、单进程多节点认证检测、请求并发限制和正式实例蓝绿切换。
- `core/sync.py` 独立调度抓取与检测同步；抓取候选先以未同步状态写入 Redis，检测使用 Redis 快照并在新正式实例就绪后提交状态。
- `fetcher/sources/` 每个来源使用独立 `.py` 文件；配置 `FRONT_PROXY` 时经过前置代理，为空时直连。
- `proxy_chain.py` 提供代理协议和本地认证连接；8082 到本地 sing-box 不得套用前置代理。
- `proxy_pool.sh` 只启动内置 Redis 和 `proxy_service.py`，不启动 5010 API。
- `web/` 是 8083 Web 控制台和登录页；不另建文件型代理池。

5010 原项目 REST API 已移除，不得恢复。8083 只承载 Web UI 和控制接口，8082 是唯一对外代理入口。

## 节点状态

节点的 `tls` 和 `synced` 必须序列化为真正的 JSON 布尔值。`tls` 表示严格证书校验的 HTTPS 目标访问能力，不表示节点连接服务器的 TLS 配置。`synced` 表示节点是否存在于当前正式 sing-box 配置。抓取不得覆盖检测状态，检测提交不得覆盖并行抓取合并的来源。节点模型和 Redis JSON 不保存地区字段，不得恢复 `region` 或 `PROXY_REGION`。

远端认证字段和 sing-box mixed 入站本地认证字段必须分开；日志和 Web UI 不得输出密码、UUID 或私钥。Redis 是唯一节点数据库，sing-box JSON 只能作为运行时配置。

## Web 控制台认证

- 8083 控制页面和 API 必须经过 `WEBUI_ACCESS_TOKEN` 登录，只有 `/login`、`/login.html`、`/auth/login` 和不含业务数据的 `/health` 可以公开。
- 默认 Access Token `sk-change-me` 只用于首次启动，生产部署必须更换；Token 不得写入日志、URL、Cookie 或浏览器存储，也不得硬编码到页面或回显给用户。
- 登录成功后只下发随机会话 Cookie，Cookie 必须保持 `HttpOnly` 和 `SameSite=Strict`；会话只保存在服务端内存，容器重启后失效。
- `WEBUI_SESSION_TIMEOUT_SECONDS` 默认 1800。自动 `/stats`、`/logs`、`/pool` 轮询不得刷新会话，只有真实用户活动或控制操作可以续期。
- API 会话过期返回 401，页面访问重定向到登录页。Docker 健康检查只访问公开的 `/health`。
- 默认 HTTP 部署不提供传输加密；在不可信网络暴露 8083 时必须使用 HTTPS 反向代理。

## sing-box 规则

- Dockerfile 从可信官方源安装固定版本预编译 sing-box，仓库不放二进制。
- 每轮检测只启动一个检测用 sing-box 运行进程，所有快照节点使用独立 outbound 和 `auth_user` 路由。
- `SING_BOX_CHECK_CONCURRENCY` 只限制共享 mixed 入站上的并发节点探测请求，默认值为 16。
- 正式实例必须使用本地 `mixed` inbound 和 `auth_user` 路由。
- 每次同步先启动并探测新实例，再切换 8082 active 端口，最后关闭旧实例。
- 新实例校验或启动失败时保留旧实例和 Redis 当前同步状态。
- sing-box 最终失败路由必须阻断，禁止通过 `direct` 泄漏服务器出口。

抓取与同步分别由 `FETCH_INTERVAL_SECONDS` 和 `CHECK_INTERVAL_SECONDS` 控制，间隔从对应任务完成后开始计算。启动时先完成首次抓取，再启动首次同步；之后两条耗时链路可以并行，但 Redis 提交必须短时互斥并按字段所有权合并。

## 来源

新来源必须继承 `BaseFetcher` 并独立成文件。当前订阅来源：

- `v2rayfree`：free-nodes/v2rayfree 的 `sub` 文件；
- `free-servers`：Pawdroid/Free-servers 的 `sub` 文件。
- `v2rayshare`：static.v2rayshare.net 按日期生成的 Base64 订阅文件；
- `freeclashnode`：node.freeclashnode.com 按日期生成的五个 Base64 订阅文件，五个文件合并为一个来源。

来源扫描默认启用 `fetcher/sources/` 中所有 `BaseFetcher.enabled` 为真的来源；生产配置不得通过排除列表只保留单一来源。

订阅文本先 Base64 解码，再解析 ss、trojan、vless、vmess、hysteria2 链接。单条坏链接只能记录并跳过，不得中断整个来源。来源下载、检测和正式远端节点访问在 `FRONT_PROXY` 非空时必须统一使用该代理，连接失败时不得绕过；为空时三条链路均直连。

## 配置和命令

```bash
python main.py --listen 0.0.0.0 --port 8082 --stats-port 8083
docker compose pull
docker compose up -d
```

关键环境变量：`DB_CONN`、`PROXY_LISTEN`、`PROXY_PORT`、`STATS_PORT`、`PROXY_TIMEOUT`、`FAIL_THRESHOLD`、`WEBUI_ACCESS_TOKEN`、`WEBUI_SESSION_TIMEOUT_SECONDS`、`FETCH_INTERVAL_SECONDS`、`CHECK_INTERVAL_SECONDS`、`HTTP_URL`、`HTTPS_URL`、`VERIFY_TIMEOUT`、`SING_BOX_CHECK_CONCURRENCY`、`SING_BOX_BINARY`、`SING_BOX_RUNTIME_DIR`、`FRONT_PROXY`、`DATA_DIR`、`CONFIG_FILE`。

`PROXY_TIMEOUT` 默认 5 秒，只控制客户端握手、上游连接和阻塞写入，不限制已建立隧道的总时长或 AI 响应等待时间。`FAIL_THRESHOLD` 默认 2，节点连续连接失败达到阈值后退出当前随机池，成功建立连接会清零失败计数。这两个值只允许通过环境变量配置，不得重新加入 Web 持久化配置。项目不存在请求级 `retries` 配置；不要把 Dockerfile 的 `HEALTHCHECK --retries` 与代理请求重试混淆。

## 验证

```bash
python -m py_compile proxy_service.py core/*.py fetcher/sources/*.py
python -c "from core.node_parser import parse_node_uri; print(parse_node_uri('vless://uuid@example.com:443'))"
```

修改节点模型、Redis 序列化、Web 认证、前置代理、sing-box 进程或 8082 协议处理时，必须补充对应测试。部署验收应确认 5010 不监听、8082/8083 正常、未登录控制 API 返回 401、空闲会话会过期、`/health` 不泄露业务数据、TLS 检测证书校验有效、同步蓝绿切换可回滚、每轮仅有一个检测运行进程且请求并发受限。
