# 免费gpt灰测画图模型，逆向竞技场臭gaygay插件

> AstrBot 的 Arena 画图插件 + 配套 LMArenaBridge 服务：白嫖 Arena 灰测画图模型
> 「蒙娜丽莎 / GPT-Image-2.5」，支持文生图、图生图。

> [!IMPORTANT]
> **有问题请进 QQ 群 `460973561` 交流，不点 Star 不给进。**

---

## 🎨 支持 Arena 灰测模型：蒙娜丽莎（GPT-Image-2.5）

可在模型列表中选择 Arena 灰测画图模型 **「蒙娜丽莎 / GPT-Image-2.5」**，文生图和图生图均可调用。

<p align="center">
  <img src="docs/examples/mona-sample-promo.png" width="49%" align="middle" alt="蒙娜丽莎最新成品示例 1">
  <img src="docs/examples/mona-sample-promo-2.jpg" width="49%" align="middle" alt="蒙娜丽莎最新成品示例 2"><br>
  <img src="docs/examples/mona-sample-girl.png" width="49%" align="middle" alt="蒙娜丽莎成品示例 1">
  <img src="docs/examples/mona-sample-boy.png" width="49%" align="middle" alt="蒙娜丽莎成品示例 2">
</p>

官网入口：[https://arena.ai](https://arena.ai)

AstrBot 的 Arena 画图插件和配套 LMArenaBridge Docker 服务。支持文生图、图生图、模型列表、模型切换、预设提示词、Arena 登录态自动采集。

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
  - 参考图是 GIF/APNG/动图 WebP 时自动取第一帧转 PNG 上传（Arena 不接受动图附件）
- `/竞技场画图模型`：只显示**正式**画图模型（竞技场写了厂商的那些），附带创建时间和最近一次上游结果
- `/竞技场灰测模型`：只显示**灰测（隐身）**模型，蒙娜丽莎在这个列表里
  - 分成两个列表是因为放行灰测模型后画图模型有 170 多个，一条消息塞不下
  - 两个列表的编号都是**全局编号**，所以各自看起来不连号，但 `/竞技场切换模型 编号`
    在哪个列表里抄的号都对得上
  - 列表按创建时间从新到旧排，新上线的灰测模型排在最前面
  - 同一个模型名在竞技场表里往往有好几行（`gpt-image-2 (medium)` 有 5 行），列表只保留
    最新的那一行，不再用不同编号重复刷同一个名字
  - 竞技场自己标了「不给用户选」的模型两个列表都不显示，那类在上游就会被拒绝
  - 创建时间来自 Arena 模型 id（UUIDv7 前 48 位）；旧的 UUIDv4 模型没有时间，此时不显示
  - 报错标注形如 `⚠500 3小时前`／`⚠403 5分钟前`，成功过的显示 `✅`；记录保存在
    Bridge 的 `model_health.json` 里，保留 6 小时，重启或更新 Bridge 都不会丢
- `/竞技场切换模型 编号或模型名`：切换模型，全局生效（所有群聊和私聊共用一个选择）
- `/jjcp 预设名 补充描述`：用预设提示词画图，补充描述可省略；带图自动图生图
- `/竞技场预设`：查看所有预设提示词（所有人可用）
- `/竞技场预设添加 名字 提示词`（管理员）：保存一条预设，同名覆盖
  - 提示词里的 `{}` 是补充描述插入的位置，也可写 `{prompt}`／`{描述}`／`{补充}`
  - 不写占位符就是纯风格预设，补充描述会自动接在后面
- `/竞技场预设删除 名字`（管理员）：删除一条预设
- `/竞技场预设模型 名字 编号或模型名`（管理员）：把预设固定到某个模型，
  之后 `/jjcp` 走这个模型而不是当前模型；`/竞技场预设模型 名字 无` 解绑
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

方式 A：插件市场安装（推荐）

在 AstrBot 管理面板的插件市场里搜索 **「免费gpt灰测画图模型，逆向竞技场臭gaygay插件」**，
或直接填入仓库地址：

```text
https://github.com/cube-lover/astrbot_plugin_arena_image
```

安装后重载插件。仓库根目录就是插件目录，之后可以直接用面板的一键更新。

方式 B：手动复制

```bash
git clone https://github.com/cube-lover/astrbot_plugin_arena_image.git
cp -r astrbot_plugin_arena_image /path/to/astrbot/data/plugins/
rm -rf /path/to/astrbot/data/plugins/astrbot_plugin_arena_image/.git
```

然后重载 AstrBot 插件。`docker/`、`docs/`、`scripts/` 是服务端和文档，留在插件目录里不影响加载，
想要干净一点可以删掉，或者用 `scripts/deploy-plugin.sh` 自动排除（见下方「更新插件」）。

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

预设提示词：先把长提示词存起来，之后一个短命令就能用。

```text
/竞技场预设添加 手办 1/7 scale figure, PVC statue on round base, studio product photo, {}
/jjcp 手办 一只白猫
/竞技场预设            # 看有哪些预设
```

`{}` 是补充描述插进去的位置；不写 `{}` 就把补充描述接在提示词后面，`/jjcp 手办`
不带补充描述时也能直接出图。默认已内置赛博／手绘／写实／动漫／手办五条，
可在插件配置的 `preset_prompts` 里改，格式是每行 `名字=提示词`。

## 配置项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `transport_mode` | `bridge` | `bridge` = 走 arena-bridge 容器；`direct` = 插件直连 arena-browser，不需要 bridge |
| `bridge_url` | `http://arena-bridge:8000` | Bridge 根地址或 `/api/v1` 地址（bridge 模式） |
| `bridge_api_key` | 空 | Bridge API Key，零配置部署留空 |
| `browser_cdp_url` | `http://arena-browser:9223` | 浏览器 CDP 地址（direct 模式，一般不用改） |
| `browser_gateway_url` | 空 | 验证链接网关；留空时插件读 `arena-browser-data/gateway-url.txt`（`setup.sh` 写入），老部署才需要手填 `http://服务器IP:6081` |
| `interactive_link_secret` | 空 | 留空即可：插件通过 CDP 读浏览器里的 `/run/secrets/interactive_link_secret`，和网关校验用的是同一个文件 |
| `interactive_link_ttl` | 900 | 验证链接有效期（秒，direct 模式） |
| `allow_stealth_models` | 开启 | 放行灰测（隐身）模型（direct 模式；bridge 模式由 `.env` 控制） |
| `default_model` | `gpt-image-2 (medium)` | 没有切换过模型时使用 |
| `request_timeout` | 300 | 模型请求和图片下载超时（秒） |
| `max_image_bytes` | 10 MB | **输入**参考图上限（图生图上传用） |
| `max_output_image_bytes` | 64 MB | **输出**图片下载上限，超过才报错 |
| `send_image_max_bytes` | 8 MB | 超过就自动压缩/降采样后再发（需要 Pillow） |
| `max_input_images` | 4 | 一次图生图最多几张参考图 |
| `max_output_images` | 1 | 一次请求最多发几张成图 |
| `max_saved_outputs` | 32 | 本地保留的成图数量 |
| `max_queue_depth` | 5 | 出图是串行的，排队超过这个数直接拒绝 |
| `rate_limit_retries` | 2 | 上游 429 后自动重试次数 |
| `rate_limit_max_wait` | 30 | 限流重试单次最长等待秒数，优先遵循 `Retry-After` |
| `preset_prompts` | 5 条内置 | 预设提示词，每行 `名字=提示词`；聊天里存的同名预设优先 |

输入和输出上限是分开的：模型返回的大图不会再因为「输入上限 10 MB」被丢掉，
只有超过 `max_output_image_bytes` 才失败，而超过 `send_image_max_bytes` 时会先压缩再发送。

模型选择是全局的：`/竞技场切换模型` 一次，所有群聊和私聊都跟着换。
预设提示词保存在插件数据目录的 `prompt_presets.json`，更新插件不会丢。
Pillow 是 AstrBot 自带的，缺失时只会跳过压缩，不影响画图。

`transport_mode = direct` 时插件自己完成模型列表、reCAPTCHA、出图、图生图上传和验证链接签名，
只需要 `arena-browser` 一个容器；Cookie 不再复制到任何配置文件，而是直接用浏览器页面的会话，
所以 `__cf_bm` / `arena-auth-prod-v1` 轮换时不会再出现「Cookie 失效」。
切过去只要把 `transport_mode` 改成 `direct`：网关地址和签名密钥都由插件通过 CDP 从
`arena-browser` 里读，不用手抄，也就不会抄错或过期。想回到原来的链路，把
`transport_mode` 改回 `bridge` 即可，不需要动容器。
详细步骤见 [docker/USAGE.md](docker/USAGE.md) 的「通道模式」一节。

## 详细文档

完整部署说明见 [docker/USAGE.md](docker/USAGE.md)。

## 目录结构

```text
.
├── main.py                   插件入口，AstrBot 加载这个文件
├── bridge_client.py          Bridge HTTP 客户端
├── metadata.yaml             插件元数据
├── _conf_schema.json         插件配置项定义
├── requirements.txt
├── cover.png                 插件市场封面
├── docker/                   LMArenaBridge 服务和两容器部署
│   ├── Dockerfile
│   ├── Dockerfile.arena-browser
│   ├── docker-compose.arena.yml
│   ├── setup.sh
│   ├── src/                  Bridge 源码
│   ├── tests/                pytest 测试
│   └── USAGE.md
├── docs/examples/            示例成图
└── scripts/deploy-plugin.sh  从 git 检出同步插件到 AstrBot 数据卷
```

`docker/`、`docs/`、`scripts/` 只是服务端和文档，AstrBot 只会加载根目录的 `main.py`。

## 更新插件

仓库根目录就是插件目录，AstrBot 面板里的插件市场更新和一键更新都能直接用。

如果插件是在服务器上从 git 检出部署的，用仓库自带脚本同步（只重启 AstrBot，不动 NapCat 登录态）：

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

脚本会自动定位 AstrBot 数据卷、备份旧目录、同步插件（自动排除 `docker/`、`docs/`、`scripts/`、
`__pycache__/`）、清理 `main.py.bak-*` 残留，最后只重启 `astrbot` 容器。
可用 `--dry-run` 先看要做什么，`--no-restart` 跳过重启。

## 安全

默认配置：

- `arena-bridge` 的 `8000` 端口只绑定宿主机 `127.0.0.1`
- noVNC `6082` 只绑定宿主机 `127.0.0.1`
- 短时验证链接 `6081` 对外开放，链接带签名和有效期；noVNC 页面和 WebSocket 也由 `6081` 网关同端口代理，不会跳转到本机-only 的 `6082`
- Arena Cookie、浏览器登录态、密钥文件只保存在服务器本地
- `/竞技场验证`、`/竞技场重新绑定`、`/竞技场验证状态`、`/竞技场切换模型`、
  `/竞技场预设添加`、`/竞技场预设删除`、`/竞技场预设模型` 只有 AstrBot 管理员能用；
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

先看 `/竞技场验证状态`，它会告出真正的原因：

```text
/竞技场验证状态
```

```text
账号登录：已登录
会话来源：browser_cookies（还有约 31 分钟）
reCAPTCHA 动作：chat_submit
```

- `账号登录：未登录` 或没有任何会话 → 运行 `/竞技场重新绑定`，打开链接登录后再查一次状态。
- 会话还在有效期内却报错 → 这是 Arena 的 reCAPTCHA / Cloudflare 风控，不是 Cookie 过期。
  运行 `/竞技场验证` 让服务器浏览器重新过一次验证即可，重新绑定不会有帮助；插件此时会明确
  提示「Arena 拒绝了这次请求，但服务器上保存的 Arena 会话还没有过期」。
- `reCAPTCHA 动作` 在已登录时应该是 `chat_submit`；显示 `sign_up` 说明 Bridge 版本太旧，
  它会用注册用的动作去出图，Arena 一定回 403，请更新 `arena-bridge` 镜像。

Arena 的 `arena-auth-prod-v1` 大约每小时轮换一次。Bridge 会自己把最新的会话写回令牌池，
所以不需要定期手动重新绑定。

### 模型列表里为什么没有竞技场上的某些模型

竞技场的模型表里每一行都有一个「厂商」字段，两种情况会被区别对待：

- 厂商为空 = 灰测（隐身）模型，竞技场故意不写，这样你只看到一个代号，猜不出是谁家的。
  这类**默认已经放行**，蒙娜丽莎就是其中之一，它们统一在 `/竞技场灰测模型` 里，
  不在 `/竞技场画图模型` 里。只想留正式模型可以在 `docker/.env` 里设
  `LM_BRIDGE_ALLOW_STEALTH_MODELS=0`。
- 那一行标了「不给用户选」（`userSelectable: false`）= 只参加盲测对战，例如
  `nano-banana-pro`（Gemini 3 Pro Image）。这类不显示也不能指定，因为竞技场上游会直接回
  `Selected model is not available for user selection`，本地放开没有用。

同一个模型名在竞技场表里往往有好几行，只要其中一行可选，这个模型就能正常用。

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
