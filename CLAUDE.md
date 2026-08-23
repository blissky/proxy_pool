# CLAUDE.md

本项目是 Redis 代理池和 sing-box 多协议出口服务。根目录生产源码、Docker 配置和 Web UI 是维护对象；参考源码不修改，运行时生成的配置和凭据不提交。

## 架构

- `proxy_service.py` 同时提供 8082 HTTP/SOCKS5 入口和 8083 Web 控制台。
- `core/node.py` 定义结构化 `ProxyNode`；Redis 以节点指纹为 hash key 保存节点及检测状态。
- `core/node_parser.py` 解析 ss、trojan、vless、vmess、hysteria2 URI 和 Base64 订阅。
- `core/store.py` 是唯一 Redis 节点存储层。
- `core/singbox.py` 负责 sing-box 配置、临时单节点检测、并发限制和正式实例蓝绿切换。
- `core/sync.py` 串行执行抓取、检测、正式配置激活和状态提交；抓取候选只保存在内存，只有检测通过并进入新正式配置的节点才能写入 Redis。
- `fetcher/sources/` 每个来源使用独立 `.py` 文件，下载必须经过前置代理。
- `proxy_chain.py` 提供代理协议和本地认证连接；8082 到本地 sing-box 不得套用前置代理。
- `proxy_pool.sh` 只启动内置 Redis 和 `proxy_service.py`，不启动 5010 API。
- `web/` 是 8083 Web 控制台；不另建文件型代理池。

5010 原项目 REST API 已移除，不得恢复。8083 只承载 Web UI 和控制接口，8082 是唯一对外代理入口。

## 节点状态

节点的 `tls` 和 `synced` 必须序列化为真正的 JSON 布尔值。`tls` 表示严格证书校验的 HTTPS 目标访问能力，不表示节点连接服务器的 TLS 配置。`synced` 表示节点是否存在于当前正式 sing-box 配置。

远端认证字段和 sing-box mixed 入站本地认证字段必须分开；日志和 Web UI 不得输出密码、UUID 或私钥。Redis 是唯一节点数据库，sing-box JSON 只能作为运行时配置。

## sing-box 规则

- Dockerfile 从可信官方源安装固定版本预编译 sing-box，仓库不放二进制。
- 每个临时检测进程只包含一个 outbound，使用空闲端口和独立配置目录。
- `SING_BOX_CHECK_CONCURRENCY` 只限制临时检测进程，不包含正式实例。
- 正式实例必须使用本地 `mixed` inbound 和 `auth_user` 路由。
- 每次同步先启动并探测新实例，再切换 8082 active 端口，最后关闭旧实例。
- 新实例校验或启动失败时保留旧实例和 Redis 当前同步状态。
- sing-box 最终失败路由必须阻断，禁止通过 `direct` 泄漏服务器出口。

## 来源

新来源必须继承 `BaseFetcher` 并独立成文件。当前订阅来源：

- `v2rayfree`：free-nodes/v2rayfree 的 `sub` 文件；
- `free-servers`：Pawdroid/Free-servers 的 `sub` 文件。

来源扫描默认启用 `fetcher/sources/` 中所有 `BaseFetcher.enabled` 为真的来源；生产配置不得通过排除列表只保留单一来源。

订阅文本先 Base64 解码，再解析 ss、trojan、vless、vmess、hysteria2 链接。单条坏链接只能记录并跳过，不得中断整个来源。来源下载、解析和远端节点访问均遵守前置代理配置。

## 配置和命令

```bash
python main.py --listen 0.0.0.0 --port 8082 --stats-port 8083
docker compose pull
docker compose up -d
```

关键环境变量：`DB_CONN`、`PROXY_PORT`、`STATS_PORT`、`FETCH_INTERVAL_SECONDS`、`CHECK_INTERVAL_SECONDS`、`SING_BOX_CHECK_CONCURRENCY`、`SING_BOX_BINARY`、`SING_BOX_RUNTIME_DIR`、`FRONT_PROXY`、`DATA_DIR`、`CONFIG_FILE`。

## 验证

```bash
python -m py_compile proxy_service.py core/*.py fetcher/sources/*.py
python -c "from core.node_parser import parse_node_uri; print(parse_node_uri('vless://uuid@example.com:443'))"
```

修改节点模型、Redis 序列化、前置代理、sing-box 进程或 8082 协议处理时，必须补充对应测试。部署验收应确认 5010 不监听、8082/8083 正常、TLS 检测证书校验有效、同步蓝绿切换可回滚、临时检测进程按并发限制回收。
