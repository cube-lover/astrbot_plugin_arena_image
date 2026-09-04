# AstrBot Arena Image Plugin + LMArenaBridge Deployment Guide

> [!IMPORTANT]
> **有问题请进 QQ 群 `460973561` 交流，不点 Star 不给进。**

---

## 🎨 支持 Arena 灰测模型：蒙娜丽莎（GPT-Image-2.5）

部署完成后，可在画图模型列表中选择 **「蒙娜丽莎 / GPT-Image-2.5」**，文生图和图生图均可调用。

成品示例：

<p align="center">
  <img src="../docs/examples/mona-sample-promo.png" width="49%" align="middle" alt="蒙娜丽莎最新成品示例 1">
  <img src="../docs/examples/mona-sample-promo-2.jpg" width="49%" align="middle" alt="蒙娜丽莎最新成品示例 2"><br>
  <img src="../docs/examples/mona-sample-girl.png" width="49%" align="middle" alt="蒙娜丽莎成品示例 1">
  <img src="../docs/examples/mona-sample-boy.png" width="49%" align="middle" alt="蒙娜丽莎成品示例 2">
</p>

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
LM_BRIDGE_ALLOW_STEALTH_MODELS=1
LM_BRIDGE_BROWSER_CDP_URL=http://arena-browser:9223
LM_BRIDGE_BROWSER_GATEWAY_URL=http://你的服务器IP或域名:6081
LM_BRIDGE_BROWSER_VNC_URL=http://你的服务器IP或域名:6082/vnc.html?autoconnect=true&resize=scale
ARENA_NOVNC_PASSWORD=强VNC密码
```

### 灰测（隐身）模型

Arena 给还在灰度测试的模型不写 `organization`（厂商）字段，这样你在竞技场里只看到一个代号，
猜不出是谁家的模型。本项目默认把这类模型全部放行：

```dotenv
LM_BRIDGE_ALLOW_STEALTH_MODELS=1
```

填 `0` 则退回上游 LMArenaBridge 的行为，只有 `luna-lisa-alpha`（蒙娜丽莎）能用，其余灰测
模型返回 403。

另一类模型是竞技场自己标了 `userSelectable: false` 的（只出现在盲测对战里），例如
`gpt-image-2 (medium)` 的部分行、`nano-banana-pro`。它们不会出现在模型列表里，也不能指定，
因为竞技场上游会直接拒绝 `Selected model is not available for user selection`，放开也没有用。

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

仓库根目录就是插件目录，把整个检出复制过去即可：

```bash
cp -r .. /path/to/astrbot/data/plugins/astrbot_plugin_arena_image
rm -rf /path/to/astrbot/data/plugins/astrbot_plugin_arena_image/.git
```

常见 AstrBot 插件路径：

```text
/data/astrbot/data/plugins/astrbot_plugin_arena_image/
```

复制后重载插件。服务器上已有 git 检出时更推荐 `scripts/deploy-plugin.sh`，
它会自动排除 `docker/`、`docs/`、`scripts/` 并只重启 AstrBot。

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
| 预设提示词 | 5 条内置 | 每行 `名字=提示词`，聊天里存的同名预设优先 |

如果 AstrBot 不在 Docker 里，或不在同一网络：

```text
bridge_url = http://服务器IP:8000
```

注意：默认 `8000` 只绑定服务器本机 `127.0.0.1`。如果 AstrBot 不在同一台机器，需要改端口映射或通过反向代理访问。

### 通道模式：bridge / direct（可省掉 arena-bridge 容器）

插件配置里的 `transport_mode` 决定画图请求走哪条路：

| 模式 | 说明 | 需要的容器 |
| --- | --- | --- |
| `bridge`（默认） | 插件 → `arena-bridge` → `arena-browser`，和以前完全一样 | arena-bridge + arena-browser |
| `direct` | 插件直接连 `arena-browser` 的 CDP，在已登录的页面里发请求 | 只要 arena-browser |

direct 模式把 Bridge 做的事（模型列表、reCAPTCHA、出图请求、图生图上传、验证链接签名）挪进插件，
少一个容器、少一层转发；Cookie 不再被复制到任何配置文件里，而是每次都用浏览器页面自己的会话
（`credentials: 'include'`），所以 `__cf_bm`、`arena-auth-prod-v1` 轮换时不会出现“Cookie 失效”。

切到 direct 只改插件配置，不用改 Docker。**要填的只有一个开关**：

| 配置项 | 填什么 |
| --- | --- |
| `transport_mode` | `direct` ← 只有这一项是必填 |
| `browser_gateway_url` | 一般不用填。`setup.sh` 会把 `.env` 里的 `LM_BRIDGE_BROWSER_GATEWAY_URL` 写进 `arena-browser-data/gateway-url.txt`，插件自己读；老部署没有这个文件时才填 `http://服务器IP:6081` |
| `interactive_link_secret` | 不用填。插件通过 CDP 读浏览器里的 `/run/secrets/interactive_link_secret`，和网关用的是同一个文件，永远不会不一致 |
| `browser_cdp_url` | 不用填，默认 `http://arena-browser:9223`；AstrBot 不在同一个 Docker 网络时才改 |
| `browser_vnc_url` | 可选。填了它也能推出 `:6081` 网关地址，并在拿不到密钥时作为兜底链接 |
| `interactive_link_ttl` | 验证链接有效期，默认 `900` 秒 |
| `allow_stealth_models` | 放行灰测模型，默认开启（bridge 模式下由 `.env` 控制） |

老部署（`setup.sh` 之前装的）补上这个文件就同样零配置，不用重启容器：

```bash
cd "$(dirname "$(find /opt -maxdepth 3 -name docker-compose.arena.yml | head -1)")"
sed -n 's/^LM_BRIDGE_BROWSER_GATEWAY_URL=//p' .env | tail -1 > arena-browser-data/gateway-url.txt
cat arena-browser-data/gateway-url.txt      # 应该是 http://服务器IP:6081
```

签名密钥不需要手抄，因为插件读的就是网关校验用的那个文件。只有非标准部署
（密钥没挂进 `arena-browser`）才需要手填，位置是 compose 同级目录：

```bash
find /opt -maxdepth 3 -name '.interactive-link-secret' 2>/dev/null
# 只在确实要手填时看内容，不要贴到聊天/工单里
```

如果 AstrBot 不在同一个 Docker 网络，把 `browser_cdp_url` 改成能通的地址，
并注意 `9223` 默认只绑定服务器本机。

**回退：** 把 `transport_mode` 改回 `bridge` 保存即可，不需要动容器；
`arena-bridge` 一直留着，direct 模式只是不用它。要退回旧版本插件用
`git checkout v0.5.0 && scripts/deploy-plugin.sh`（只重启 astrbot，不动 NapCat）。

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

模型列表分成两个命令：放行灰测模型后画图模型有 170 多个，一条消息塞不下。

```text
/竞技场画图模型     正式模型（竞技场写了厂商的）
/竞技场灰测模型     灰测（隐身）模型，蒙娜丽莎在这里
/竞技场切换模型 27
/竞技场切换模型 gpt-image-2 (medium)
```

两个列表里的编号都是全局编号，所以各自看起来不连号（例如灰测列表可能是 1、3、7），
但不管从哪个列表抄号，`/竞技场切换模型 编号` 都指向你看到的那一行。

模型选择是全局的：切换一次，所有群聊和私聊都跟着换。

### 预设提示词

把长提示词存成短名字，之后一条命令直接出图。

```text
/竞技场预设添加 手办 1/7 scale figure, PVC statue on round base, studio product photo, {}
/jjcp 手办 一只白猫
/jjcp 手办                     不带补充描述也能出图
/竞技场预设                     查看所有预设
/竞技场预设删除 手办
/竞技场预设模型 手办 27         把预设固定到 27 号模型
/竞技场预设模型 手办 无         解绑，改用当前模型
```

- `{}` 是补充描述插进去的位置，也可以写 `{prompt}`、`{描述}`、`{补充}`
- 不写占位符就是纯风格预设，补充描述自动接在提示词后面
- `/jjcp` 带图或引用带图消息时自动走图生图
- 添加、删除、绑定模型只有管理员能用；`/竞技场预设` 和 `/jjcp` 所有人可用
- 预设存在插件数据目录的 `prompt_presets.json`，更新插件不会丢；
  也可以在插件配置的 `preset_prompts` 里预置，格式是每行 `名字=提示词`

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

### 10. direct 模式：验证链接打不开 / 提示连不上浏览器

按报错分两种：

- “还差一个地址：请把 browser_gateway_url 填成 …”：插件既没读到
  `arena-browser-data/gateway-url.txt`，配置里也没填网关地址。按上面第五节的三行命令
  补上那个文件，或者直接在插件配置里填 `http://服务器IP:6081`。
- “拿不到验证链接：网关地址有了，但签名密钥…读不到”：`.interactive-link-secret`
  没挂进 `arena-browser`（检查 compose 的 `secrets:`），此时可以手填
  `interactive_link_secret` 作为临时办法。
- “连不上服务器浏览器”“服务器浏览器响应超时”：`arena-browser` 没起来或 `browser_cdp_url` 不通。

```bash
docker compose -f docker-compose.arena.yml ps arena-browser
docker exec astrbot python3 -c "import urllib.request,json;print(json.load(urllib.request.urlopen('http://arena-browser:9223/json/version'))['Browser'])"
```

任何时候都可以把 `transport_mode` 改回 `bridge` 恢复原来的链路。

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
