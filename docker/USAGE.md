# AstrBot Arena Image Plugin + LMArenaBridge Deployment Guide

> [!IMPORTANT]
> **有问题请进 QQ 群 `460973561` 交流，不点 Star 不给进。**

---

## 🎨 支持 Arena 灰测模型：蒙娜丽莎（GPT-Image-2.5）

部署完成后，可在画图模型列表中选择 **「蒙娜丽莎 / GPT-Image-2.5」**，文生图和图生图均可调用。

成品示例：

| 最新成品效果 | 最新成品效果 | 成品效果 | 成品效果 |
| --- | --- | --- | --- |
| ![蒙娜丽莎最新成品示例 1](../docs/examples/mona-sample-promo.png) | ![蒙娜丽莎最新成品示例 2](../docs/examples/mona-sample-promo-2.jpg) | ![蒙娜丽莎成品示例 1](../docs/examples/mona-sample-girl.png) | ![蒙娜丽莎成品示例 2](../docs/examples/mona-sample-boy.png) |

官网入口：[https://arena.ai](https://arena.ai)

本文档面向第一次部署的用户，从零开始完成：Docker 服务启动、AstrBot 插件安装、Arena 登录、画图测试。

运行形态：

```text
AstrBot 插件
    ↓
arena-bridge（OpenAI 兼容 API / Cookie 管理 / 模型列表）
    ↓
arena-browser（服务器 Chrome + noVNC + CDP + 验证链接网关）
    ↓
Arena 网站
```

只有两个业务容器：

1. `arena-bridge`
2. `arena-browser`

---

## 一、环境要求

| 项目 | 要求 |
| --- | --- |
| 系统 | Linux x86_64 |
| 内存 | 建议 4 GB 以上 |
| 磁盘 | 建议 10 GB 以上 |
| Docker | 20.10+ |
| Docker Compose | v2 |
| 网络 | 需要能访问 Docker Hub / GHCR / Arena |
| AstrBot | 已部署，推荐 Docker 部署 |

低于 4 GB 内存可能会出现浏览器容器不稳定。

---

## 二、获取代码

```bash
git clone https://github.com/cube-lover/astrbot_plugin_arena_image.git
cd astrbot_plugin_arena_image/docker
```

---

## 三、初始化配置

```bash
chmod +x setup.sh
./setup.sh
```

脚本会自动完成：

- 从 `.env.example` 创建 `.env`
- 自动生成 `LM_BRIDGE_ADMIN_PASSWORD`
- 自动生成 `ARENA_NOVNC_PASSWORD`
- 自动检测服务器公网 IP 并填入两个 URL
- 自动检测 AstrBot 所在 Docker 网络名
- 生成 `.interactive-link-secret`
- 生成 `.novnc-password`
- 校验 Compose 配置

零配置说明：

- `LM_BRIDGE_API_KEY` 保持为空
- Bridge API 不需要填 Key
- 插件里的 `bridge_api_key` 也留空

如果脚本无法自动识别你的环境，手动编辑 `.env`：

```dotenv
ASTRBOT_NETWORK=你的 AstrBot Docker 网络名
LM_BRIDGE_ADMIN_PASSWORD=强管理密码
LM_BRIDGE_API_KEY=
LM_BRIDGE_RPM=30
LM_BRIDGE_BROWSER_CDP_URL=http://arena-browser:9223
LM_BRIDGE_BROWSER_GATEWAY_URL=http://你的服务器IP或域名:6081
LM_BRIDGE_BROWSER_VNC_URL=http://你的服务器IP或域名:6082/vnc.html?autoconnect=true&resize=scale
ARENA_NOVNC_PASSWORD=强VNC密码
```

### 代理配置（可选）

香港、海外等可以直连 Arena 的服务器不需要填写代理，保持下面这一行为空即可：

```dotenv
LM_BRIDGE_PROXY_URL=
```

国内服务器如果无法稳定访问 Arena，可以填写一个服务器可访问的 HTTP/HTTPS 或 SOCKS5 代理：

```dotenv
# HTTP 代理示例
LM_BRIDGE_PROXY_URL=http://192.168.1.100:7890

# SOCKS5 代理示例
LM_BRIDGE_PROXY_URL=socks5://192.168.1.100:1080
```

说明：

- 代理会同时作用于 `arena-bridge` 和 `arena-browser`
- 代理会在运行阶段生效；重新 `--build` 时也会作为 Docker Build 代理传入
- Docker 内网、`localhost`、`arena-bridge`、`arena-browser` 已自动排除，不需要手动再配 `NO_PROXY`
- 如果代理部署在你自己的电脑上，请填写服务器能访问到的地址；除非代理也在服务器上，否则不要填 `127.0.0.1`
- 修改代理后需要重建容器：

```bash
docker compose -f docker-compose.arena.yml --env-file .env up -d
```

---

## 四、启动服务

```bash
docker compose -f docker-compose.arena.yml --env-file .env up -d --build
```

查看状态：

```bash
docker compose -f docker-compose.arena.yml ps
```

正常应看到：

```text
arena-bridge   Up (healthy)
arena-browser  Up
```

查看日志：

```bash
docker logs -f arena-bridge
docker logs -f arena-browser
```

健康检查：

```bash
curl http://127.0.0.1:8000/api/v1/health
curl http://127.0.0.1:6081/health
```

返回：

```json
{"status":"healthy"}
```

首次启动时 `arena-bridge` 需要 30–90 秒完成初始化和模型列表加载。

---

## 五、安装 AstrBot 插件

### 方式 A：插件市场 / 仓库链接安装

1. 打开 AstrBot 管理面板
2. 进入插件管理
3. 选择从仓库安装
4. 填入：

```text
https://github.com/cube-lover/astrbot_plugin_arena_image
```

5. 安装后重载插件

### 方式 B：手动复制

```bash
cp -r ../astrbot_plugin_arena_image /path/to/astrbot/data/plugins/
```

常见 AstrBot 插件路径：

```text
/data/astrbot/data/plugins/astrbot_plugin_arena_image/
```

复制后重载插件。

### 插件配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| Bridge 地址 | `http://arena-bridge:8000` | AstrBot 与 Bridge 同网络时不用改 |
| Bridge API Key | 空 | 零配置，留空即可 |
| 默认模型 | `gpt-image-2 (medium)` | 可用命令切换 |
| 请求超时 | `300` | 秒 |
| 模型缓存时间 | `30` | 秒 |
| 模型列表数量 | `50` | |
| 只显示画图模型 | 开启 | |
| 参考图最大数量 | `4` | 图生图 |

如果 AstrBot 不在 Docker 里，或不在同一网络：

```text
bridge_url = http://服务器IP:8000
```

注意：默认 `8000` 只绑定服务器本机 `127.0.0.1`。如果 AstrBot 不在同一台机器，需要改端口映射或通过反向代理访问。

---

## 六、首次登录 Arena

在 AstrBot 聊天里执行：

```text
/竞技场验证
```

插件会返回一个短时签名链接。用你的浏览器打开：

1. 点击“进入验证浏览器”
2. 完成 Cloudflare / Turnstile 验证
3. 登录 Arena 账号

完成后执行：

```text
/竞技场验证状态
```

应看到类似：

```text
Cloudflare：已通过
Arena 会话：已获取
登录状态：已登录
```

Cookie 会自动保存。后续 Cookie 失效时执行：

```text
/竞技场重新绑定
```

重新打开链接、登录、执行 `/竞技场验证状态`。

---

## 七、画图测试

### 文生图

```text
/jjc 男孩
```

或：

```text
/竞技场画图 男孩
```

### 图生图

直接发带图消息：

```text
/jjc 把这张图改成蓝色主色调
```

引用一条带图消息：

```text
/jjc 保持构图，改成赛博朋克风格
```

@某人使用头像作为参考图：

```text
/jjc @某人 把头像改成动漫风格
```

### 查看和切换模型

```text
/竞技场画图模型
/竞技场切换模型 27
/竞技场切换模型 gpt-image-2 (medium)
```

模型选择按群聊/私聊独立保存。

---

## 八、端口和安全

默认端口：

| 端口 | 绑定 | 用途 |
| --- | --- | --- |
| 8000 | `127.0.0.1` | Bridge API |
| 6081 | `0.0.0.0` | 短时验证链接网关，同时代理 noVNC 页面和 WebSocket |
| 6082 | `127.0.0.1` | noVNC |
| 9223 | Docker 内网 | CDP 代理 |

安全建议：

- 不要把 `8000` 直接暴露公网
- 正常情况下只需要开放 `6081`；验证链接会在同端口加载 noVNC 静态资源并代理 WebSocket，不需要开放 `6082`
- 如需远程访问管理页面，用 SSH 隧道：

```bash
ssh -L 8000:127.0.0.1:8000 user@server
```

- 如需直接打开验证浏览器，将 compose 中：

```yaml
- "127.0.0.1:6082:6080"
```

改为：

```yaml
- "6082:6080"
```

并确保 `ARENA_NOVNC_PASSWORD` 是强密码。

- 也可以通过反向代理暴露 `6082`，并把 `.env` 里的两个 URL 改成你的域名。

---

## 九、数据与备份

需要备份的只有这些：

```text
docker/.env
docker/.interactive-link-secret
docker/.novnc-password
docker/arena-data/
docker/arena-browser-data/
```

不要把这些文件发给任何人：

```text
.env
.interactive-link-secret
.novnc-password
arena-data/
arena-browser-data/
```

这些文件包含密码、Cookie 或 Arena 登录态。

---

## 十、维护命令

```bash
# 查看容器
docker compose -f docker-compose.arena.yml ps

# 重启 Bridge
docker compose -f docker-compose.arena.yml restart arena-bridge

# 重启浏览器
docker compose -f docker-compose.arena.yml restart arena-browser

# 升级
git pull
docker compose -f docker-compose.arena.yml --env-file .env up -d --build

# 查看日志
docker logs -f arena-bridge
docker logs -f arena-browser
```

---

## 十一、常见问题排查

### 1. AstrBot 连不上 Bridge

检查网络：

```bash
docker inspect astrbot --format '{{json .NetworkSettings.Networks}}'
docker inspect arena-bridge --format '{{json .NetworkSettings.Networks}}'
```

两个容器必须在同一个网络。插件 Bridge 地址应为：

```text
http://arena-bridge:8000
```

### 2. Bridge 显示 unhealthy

```bash
docker logs --tail 200 arena-bridge
curl http://127.0.0.1:8000/api/v1/health
```

首次启动请等待 90 秒。

### 3. 模型列表为空

```text
/竞技场验证
/竞技场验证状态
```

然后重新执行：

```text
/竞技场画图模型
```

### 4. 提示 Cookie 失效

```text
/竞技场重新绑定
```

打开链接，完成登录和验证，再执行：

```text
/竞技场验证状态
```

### 5. 403 Forbidden

通常是 Cloudflare 或 Arena 登录问题：

```text
/竞技场重新绑定
```

### 6. 429 Too Many Requests

Arena 上游限速。等待几分钟，不要连续请求。可调低：

```dotenv
LM_BRIDGE_RPM=10
```

### 7. 图生图失败但文生图正常

检查消息里是否真的带了图片。查看 Bridge 日志：

```bash
docker logs --tail 200 arena-bridge
```

重点找：

```text
Attachments: 0 images
Attachments: 1 images
```

如果显示 `0 images`，说明图片没有进入 Bridge，检查 AstrBot 消息段解析。

### 8. 画图很慢

Arena 图像模型生成时间通常在 20–90 秒，属于正常范围。请求超时配置默认 300 秒。

### 9. 国内服务器无法访问 Arena

在 `.env` 中配置：

```dotenv
LM_BRIDGE_PROXY_URL=http://你的代理地址:端口
```

然后执行：

```bash
docker compose -f docker-compose.arena.yml --env-file .env up -d
```

---

## 十二、隐私说明

本仓库不包含任何账号、Cookie、Token、API Key、浏览器登录态。

运行后产生的敏感数据全部在本机：

```text
.env
.interactive-link-secret
.novnc-password
arena-data/
arena-browser-data/
```

`.gitignore` 已排除这些内容。请勿手动提交或分享。
