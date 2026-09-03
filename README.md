# AstrBot Arena Image Plugin + LMArenaBridge

> [!IMPORTANT]
> **有问题请进 QQ 群 `460973561` 交流，不点 Star 不给进。**

---

## 🎨 支持 Arena 灰测模型：蒙娜丽莎（GPT-Image-2.5）

可在模型列表中选择 Arena 灰测画图模型 **「蒙娜丽莎 / GPT-Image-2.5」**，文生图和图生图均可调用。

| 最新成品效果 | 最新成品效果 | 成品效果 | 成品效果 |
| --- | --- | --- | --- |
| ![蒙娜丽莎最新成品示例 1](docs/examples/mona-sample-promo.png) | ![蒙娜丽莎最新成品示例 2](docs/examples/mona-sample-promo-2.jpg) | ![蒙娜丽莎成品示例 1](docs/examples/mona-sample-girl.png) | ![蒙娜丽莎成品示例 2](docs/examples/mona-sample-boy.png) |

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
└── .github/workflows/            GHCR 镜像发布工作流
```

## 安全

默认配置：

- `arena-bridge` 的 `8000` 端口只绑定宿主机 `127.0.0.1`
- noVNC `6082` 只绑定宿主机 `127.0.0.1`
- 短时验证链接 `6081` 对外开放，链接带签名和有效期；noVNC 页面和 WebSocket 也由 `6081` 网关同端口代理，不会跳转到本机-only 的 `6082`
- Arena Cookie、浏览器登录态、密钥文件只保存在服务器本地

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

Arena 上游限速。等待几分钟再试，不要连续请求。可在 `.env` 里调低 `LM_BRIDGE_RPM`。

### 国内服务器无法访问 Arena

在 `docker/.env` 中设置 `LM_BRIDGE_PROXY_URL`，然后重新执行 `docker compose -f docker-compose.arena.yml --env-file .env up -d`。

## License

MIT
