# CLAUDE.md

Redis + sing-box 多协议代理池。一个 Python 进程（`proxy_service.py`）同时提供 8082 代理入口、8083 Web 控制台和后台抓取/检测调度。没有其他服务端口。

## 目录

- `proxy_service.py`：8082 HTTP/SOCKS5 入口（`ProxyServer`）、8083 控制台（`start_control_server`，单线程 `HTTPServer`）、会话认证（`WebAuth`）、内存日志（`Logger`）、运行时选点（`RuntimePool`）。`main.py` 只是兼容入口。
- `core/node.py`：`ProxyNode` 模型。`node_id` 是协议、出站配置和远端凭据的 SHA-256 指纹。
- `core/node_parser.py`：解析 ss/trojan/vless/vmess/hysteria2 URI、Base64 订阅，以及粘贴导入文本（`parse_import_text`）。
- `core/share_link.py`：把节点导出为标准分享链接。
- `core/store.py`：`NodeStore`，唯一的 Redis 访问层，节点存在一个 hash 里（key 为 `node_id`，值为 JSON）。
- `core/singbox.py`：生成 sing-box 配置；`SourceValidator`（入库前 `sing-box check`）、`NodeDetector`（单进程批量检测）、`SingBoxSupervisor`（正式实例蓝绿切换）。
- `core/sync.py`：`SyncManager`，调度抓取、检测同步和粘贴导入。
- `helper/fetch.py`：自动发现 `fetcher/sources/*.py` 中的 `BaseFetcher` 子类并并发抓取。
- `fetcher/sources/`：每个来源一个文件；`_` 开头的文件是共享辅助模块，不会被当作来源。
- `proxy_chain.py`：底层 HTTP/SOCKS 连接和带严格证书校验的探测请求。`front_proxy.py`：解析 `FRONT_PROXY`。
- `web/`：`index.html` 控制台、`pool.html` 节点列表、`login.html` 登录页，都是无构建步骤的单文件页面。
- `handler/configHandler.py` + `setting.py`：环境变量优先的配置；`proxy_service.Config` 管理写入 `CONFIG_FILE` 的 listen/port/stats_port/max_clients。

## 数据流和状态规则

- 抓取：来源结果去重 → `SourceValidator.validate` 按来源整批校验 → `NodeStore.upsert_candidates`。新节点 `synced=False`、`tls=False`；已有节点只合并来源，不覆盖检测字段（`tls`、`synced`、`config_revision`、计数、入站凭据）。
- 粘贴导入（`POST /import` → `SyncManager.import_nodes`）：走同一个 `upsert_candidates`，但用 `SourceValidator.split_invalid` 二分剔除单个坏节点，来源记为 `manual`。
- 检测：对 Redis 快照启动一个检测用 sing-box，所有节点用独立 outbound 和 `auth_user` 路由，并发数由 `SING_BOX_CHECK_CONCURRENCY` 限制。先测 `HTTPS_URL`（严格证书校验，通过即 `tls=True`），失败再测 `HTTP_URL`。
- 同步：`SingBoxSupervisor.activate` 先 check、启动、确认端口就绪，再在锁内切换 active 实例并调用 `commit_detection` 写 Redis；提交失败回滚到旧实例。检测失败的快照节点从 Redis 删除；本轮无可用节点且已有正式实例时放弃激活。
- `synced` 表示节点在当前正式 sing-box 配置中；`tls` 表示能通过严格证书校验访问 HTTPS 目标，与节点自身的 TLS 配置无关（节点出站 TLS 在 sing-box 中设为 `insecure`）。两者在 JSON 中必须是真正的布尔值。
- 8082 运行时连接失败达到 `FAIL_THRESHOLD` 时调用 `mark_unsynced`，不删除节点。
- 抓取、导入与检测提交可以并行，Redis 写入都在 `NodeStore.lock` 内完成；`SyncManager.failed_ids_by_epoch` 防止慢抓取批次把刚被检测删除的节点写回。
- sing-box 配置的 `route.final` 固定为 `block`，未匹配的流量不能直连泄漏服务器出口。

## 安全约束

- 8083 除 `/login`、`/login.html`、`/auth/login`、`/health` 外都要求会话 Cookie；`/api/proxies` 例外，使用 `token` 查询参数做常量时间比较。
- 会话只在内存中，Cookie 为 `HttpOnly; SameSite=Strict`。`/stats`、`/logs`、`/pool` 轮询不续期，只有 `touch=True` 的请求续期。
- 日志、API 响应和页面不得输出远端密码、UUID、私钥或 Access Token。解析错误只报告行号；涉及前置代理的错误先经 `SyncManager._safe_error` 脱敏。
- 远端节点凭据（`remote_*`、`outbound_config`）与本地 mixed 入站凭据（`inbound_*`）分开存储，分享链接只用前者，8082 导出链接只用后者。
- `FRONT_PROXY` 非空时，来源下载、检测和正式实例出站都经过它（sing-box 中为 `detour`），失败不回落直连；8082 到本地 sing-box 不走前置代理。

## 约定

- 新来源：在 `fetcher/sources/` 新建文件，继承 `BaseFetcher`，设置唯一 `name`；`fetch()` 产出 `host:port` 字符串（配合 `proxy_type = "http"|"socks"`）或 `ProxyNode`。单条坏数据记录日志后跳过，不得中断整个来源。
- 用户可见日志和 Web 文案使用中文。
- 前端保持现有单文件内联 CSS/JS 风格，插值到 HTML 前用 `esc()` 转义。
- `PROXY_TIMEOUT`、`FAIL_THRESHOLD` 只从环境变量读取，不放进 `CONFIG_FILE`。

## 命令

```bash
python main.py --listen 0.0.0.0 --port 8082 --stats-port 8083   # 本地运行，需要 Redis 和 sing-box
bash proxy_pool.sh start --fg                                    # 同时启动本机 Redis（Docker 中的启动方式）
docker compose pull && docker compose up -d
```

## 验证

```bash
python -m py_compile proxy_service.py main.py core/*.py helper/*.py fetcher/sources/*.py
python -m unittest discover -s tests -v
```

- `tests/` 被 `.gitignore` 和 `.dockerignore` 排除，测试只保存在本地，使用标准库 `unittest`。
- 开发机通常没有 sing-box：测试中用假的 Redis 连接（`NodeStore(connection=...)`）并替换 `SourceValidator._check_source`，控制台接口用 `start_control_server` 绑定端口 0 测试。
- 修改节点模型、Redis 序列化、Web 认证、前置代理、sing-box 进程管理或 8082 协议处理时，需要补充对应测试。
- `LogHandler` 导入时会在仓库根目录创建 `log/`（已被忽略）。
