# ProxyPool

这是一个以 Redis 为唯一节点数据库、以 sing-box 为多协议出口层的代理池。Web 控制台和 8082 转发入口在同一个容器中运行，支持 HTTP、SOCKS、Shadowsocks、Trojan、VLESS、VMess 和 Hysteria2 节点。

## 启动

```bash
docker compose pull
docker compose up -d
docker compose ps
```

Compose 只拉取 GitHub Container Registry 中的 `ghcr.io/blissky/proxy_pool:latest`，不会在本地构建镜像。sing-box 由 Dockerfile 从官方签名 APT 源安装固定版本，仓库不保存二进制文件。

| 地址 | 用途 |
| --- | --- |
| `http://127.0.0.1:8083` | Web 控制台 |
| `127.0.0.1:8082` | HTTP/SOCKS5 对外代理入口 |

8082 只连接当前正式 sing-box 的本地 mixed 端口。Redis 节点选择、本地认证路由、协议转换和远端连接由同步管理器与 sing-box 协作完成。没有可用节点或 sing-box 未就绪时不会直连目标站点。

## 同步流程

```text
代理源
  -> 节点解析（仅保存在本轮内存候选集合）
  -> 单节点临时 sing-box 并发检测
  -> HTTP 检测和严格 TLS 证书检测
  -> 只将通过检测并进入新正式 sing-box 的节点写入 Redis
  -> 生成新正式 sing-box
  -> 新实例就绪后切换 8082
  -> 更新 synced/config_revision
  -> 关闭旧实例
```

每轮同步使用蓝绿切换，旧实例在新实例就绪前继续服务。临时检测实例的并发数由 `SING_BOX_CHECK_CONCURRENCY` 控制，不包含正式实例。

Web 控制台提供：

- 立即同步；
- 抓取、检测、配置生成和切换进度；
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

在 Compose 同目录的 `.env` 中设置：

```dotenv
FRONT_PROXY=socks5://user:password@host:1080
```

支持 `http://`、`https://`、`socks4://`、`socks4a://`、`socks5://` 和 `socks5h://`。8082 到本地 sing-box mixed 端口的连接不经过前置代理，避免形成代理环路。

## Compose 环境变量

| 变量 | 说明 |
| --- | --- |
| `DB_CONN` | Redis 连接 URI |
| `PROXY_LISTEN`、`PROXY_PORT` | 8082 监听地址和端口 |
| `STATS_PORT` | Web UI 端口，默认 8083 |
| `FETCH_INTERVAL_SECONDS` | 代理源刷新间隔，单位秒 |
| `CHECK_INTERVAL_SECONDS` | 完整同步和可用性检测间隔，单位秒 |
| `SING_BOX_CHECK_CONCURRENCY` | 临时检测 sing-box 最大并发数，不包含正式实例 |
| `SING_BOX_BINARY` | sing-box 命令路径，默认 `sing-box` |
| `SING_BOX_RUNTIME_DIR` | sing-box 配置和运行目录 |
| `FRONT_PROXY` | 抓取、检测和远端节点访问使用的前置代理 |
| `DATA_DIR`、`CONFIG_FILE` | Web 配置和运行数据目录 |

## 本地检查

```bash
python -m py_compile proxy_service.py core/*.py fetcher/sources/*.py
python main.py --help
```

容器部署验收应确认：8082 和 8083 正常、5010 不再监听；检测并发不超过配置值；同步期间旧 sing-box 继续服务；新实例失败时旧实例和 Redis 同步状态保持不变。
