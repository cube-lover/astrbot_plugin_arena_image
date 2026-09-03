# AstrBot Arena Image Plugin + LMArenaBridge

> [!IMPORTANT]
> **有问题请进 QQ 群 `460973561` 交流，不点 Star 不给进。**

---

## 🎨 支持 Arena 灰测模型：蒙娜丽莎（GPT-Image-2.5）

可在模型列表中选择 Arena 灰测画图模型 **「蒙娜丽莎 / GPT-Image-2.5」**，文生图和图生图均可调用。

<p align="center">
  <img src="docs/examples/mona-sample-promo.png" width="49%" alt="蒙娜丽莎最新成品示例 1">
  <img src="docs/examples/mona-sample-promo-2.jpg" width="49%" alt="蒙娜丽莎最新成品示例 2"><br>
  <img src="docs/examples/mona-sample-girl.png" width="49%" alt="蒙娜丽莎成品示例 1">
  <img src="docs/examples/mona-sample-boy.png" width="49%" alt="蒙娜丽莎成品示例 2">
</p>

官网入口：[https://arena.ai](https://arena.ai)

AstrBot 的 Arena 画图插件和配套 LMArenaBridge Docker 服务。支持文生图、图生图、模型列表、按会话切换模型、Arena 登录态自动采集。

```text
AstrBot 插件
    ↓
arena-bridge（Python API）
    ↓
arena-browser（Chrome + noVNC + CDP）
    ↓
Arena
```

## 功能

- `/jjc 提示词`：统一文生图/图生图命令，消息带图或引用带图消息时自动走图生图
- `/竞技场画图 提示词`：文生图
- `/竞技场图生图 提示词`：图生图，支持当前消息图片、引用消息图片、@头像作为参考图
- `/竞技场画图模型`：只显示画图模型列表，并显示模型状态
- `/竞技场切换模型 编号或模型名`：按群聊/私聊保存模型
- `/竞技场状态`：查看 Bridge 状态
- `/竞技场验证`：生成短时服务器浏览器链接，完成 Arena 登录和 CF/Turnstile 验证
- `/竞技场验证状态`：检查 Arena/CF/登录状态
- `/竞技场重新绑定`：Cookie 失效时重新绑定

## 快速部署

### 1. 准备

要求：

- Linux 服务器，建议 4 GB 以上内存
- Docker 20.10+
- Docker Compose v2
- 已部署 AstrBot，AstrBot 最好也在 Docker 中

### 2. 初始化

```bash
git clone https://github.com/cube-lover/astrbot_plugin_arena_image.git
cd astrbot_plugin_arena_image/docker
chmod +x setup.sh
./setup.sh
```

`setup.sh` 会自动：

- 创建 `.env`
- 自动生成管理密码和 VNC 密码
- 自动检测服务器公网 IP
- 自动检测 AstrBot 所在 Docker 网络
- 生成 `.interactive-link-secret` 和 `.novnc-password`
- 校验 Compose 配置

Bridge API Key 默认留空。服务只监听 `127.0.0.1` 和 Docker 内网，不需要填 API Key。

国内服务器可选配置代理：编辑 `docker/.env`，设置 `LM_BRIDGE_PROXY_URL=http://你的代理地址:端口`；香港/海外服务器保持为空即可。详见 [docker/USAGE.md](docker/USAGE.md)。

### 3. 启动 Docker

```bash
docker compose -f docker-compose.arena.yml --env-file .env up -d --build
```

检查状态：

```bash
docker compose -f docker-compose.arena.yml ps
curl http://127.0.0.1:8000/api/v1/health
curl http://127.0.0.1:6081/health
```

### 4. 安装 AstrBot 插件

方式 A：插件市场/仓库链接安装

在 AstrBot 管理面板的插件市场中填入：

```text
https://github.com/cube-lover/astrbot_plugin_arena_image
```

安装后重载插件。

方式 B：手动复制

```bash
cp -r astrbot_plugin_arena_image /path/to/astrbot/data/plugins/
```

然后重载 AstrBot 插件。

插件默认配置：

```text
bridge_url = http://arena-bridge:8000
bridge_api_key = 留空
```

如果 AstrBot 和 Bridge 不在同一台机器或不同 Docker 网络，把 `bridge_url` 改成实际地址。

### 5. 首次登录 Arena

在聊天里执行：

```text
/竞技场验证
```

打开返回的短时链接，进入服务器浏览器：

1. 完成 Cloudflare / Turnstile 验证
2. 登录 Arena 账号

完成后执行：

```text
/竞技场验证状态
```

看到 Arena 登录状态正常即可。

### 6. 画图

```text
/jjc 男孩
```

带图消息或引用带图消息：

```text
/jjc 把这张图改成赛博朋克风格
```

## 详细文档

完整部署说明见 [docker/USAGE.md](docker/USAGE.md)。

## 目录结构

```text
.
├── astrbot_plugin_arena_image/   AstrBot 插件
├── docker/                       LMArenaBridge 服务和两容器部署
│   ├── Dockerfile
│   ├── Dockerfile.arena-browser
│   ├── docker-compose.arena.yml
│   ├── setup.sh
│   └── USAGE.md
├── scripts/deploy-plugin.sh      从 git 检出同步插件到 AstrBot 数据卷
└── .github/workflows/            GHCR 镜像发布工作流
```

## 更新插件

AstrBot 的一键更新只认仓库根目录就是插件目录的布局，本仓库把插件放在子目录里，
所以服务器上用仓库自带脚本同步（只重启 AstrBot，不动 NapCat 登录态）：

```bash
git clone https://github.com/cube-lover/astrbot_plugin_arena_image.git /opt/astrbot-plugins-src/astrbot_plugin_arena_image
cd /opt/astrbot-plugins-src/astrbot_plugin_arena_image
scripts/deploy-plugin.sh
```

之后每次更新：

```bash
cd /opt/astrbot-plugins-src/astrbot_plugin_arena_image
git pull
scripts/deploy-plugin.sh
```

脚本会自动定位 AstrBot 数据卷、备份旧目录、同步插件、清理 `main.py.bak-*` 残留，
最后只重启 `astrbot` 容器。可用 `--dry-run` 先看要做什么，`--no-restart` 跳过重启。

## 安全

默认配置：

- `arena-bridge` 的 `8000` 端口只绑定宿主机 `127.0.0.1`
- noVNC `6082` 只绑定宿主机 `127.0.0.1`
- 短时验证链接 `6081` 对外开放，链接带签名和有效期；noVNC 页面和 WebSocket 也由 `6081` 网关同端口代理，不会跳转到本机-only 的 `6082`
- Arena Cookie、浏览器登录态、密钥文件只保存在服务器本地
- `/竞技场验证`、`/竞技场重新绑定`、`/竞技场验证状态`、`/竞技场切换模型` 只有 AstrBot 管理员能用；
  验证链接只在私聊发放，群里请求会被拒绝（链接等于交出已登录 Arena 的浏览器）

不要把以下文件发给其他人：

```text
.env
.interactive-link-secret
.novnc-password
docker/arena-data/
docker/arena-browser-data/
```

## 常见问题

### AstrBot 连不上 Bridge

确认 AstrBot 容器和 `arena-bridge` 在同一个 Docker 网络：

```bash
docker inspect astrbot --format '{{json .NetworkSettings.Networks}}'
docker inspect arena-bridge --format '{{json .NetworkSettings.Networks}}'
```

插件里 Bridge 地址应填：

```text
http://arena-bridge:8000
```

### 提示 Cookie 失效

```text
/竞技场重新绑定
```

打开新链接，登录后执行 `/竞技场验证状态`。

### 429 Too Many Requests

Arena 上游限速。插件已经会读 `Retry-After` 自动重试 2 次（`rate_limit_retries`），
仍然失败时会直接告诉你要等多久，这时不要连续重试，可换个模型或调低 `.env` 里的 `LM_BRIDGE_RPM`。

### 出图很久没反应 / 提示排队

出图是串行的：同一时间只跑一个请求，其余排队。插件会回报前面还有几个任务和大致等待时间，
排队超过 `max_queue_depth`（默认 5）会直接拒绝。

### 成图太大发不出去

`max_output_image_bytes`（默认 64 MB）是下载上限，`send_image_max_bytes`（默认 8 MB）是发送阈值，
超过发送阈值会自动压缩/降采样成 JPEG 再发，不会丢掉已经画好的图。

### 国内服务器无法访问 Arena

在 `docker/.env` 中设置 `LM_BRIDGE_PROXY_URL`，然后重新执行 `docker compose -f docker-compose.arena.yml --env-file .env up -d`。

## License

MIT
