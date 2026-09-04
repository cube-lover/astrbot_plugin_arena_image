# 免费gpt灰测画图模型，逆向竞技场臭gaygay插件

> **一条命令装好，一个链接登录，一句 `/jjc 男孩` 出图。**
> 让你的 AstrBot 白嫖 Arena 上还没正式发布的灰测画图模型「蒙娜丽莎 / GPT-Image-2.5」，
> 文生图、图生图全都能用，**一分钱不花**。

<p align="center">
  <img src="https://img.shields.io/badge/AstrBot-插件-blue" alt="AstrBot 插件">
  <img src="https://img.shields.io/badge/部署-复制两条命令-brightgreen" alt="复制两条命令即可部署">
  <img src="https://img.shields.io/badge/插件配置-不用填地址-orange" alt="开箱即用">
  <img src="https://img.shields.io/badge/Cookie-自动续期-ff69b4" alt="Cookie 自动续期">
  <img src="https://img.shields.io/badge/License-MIT-lightgrey" alt="MIT">
</p>

### 为什么说它非常方便、开箱即用

- 🚀 **部署就两条命令**：`./setup.sh` 自动生成所有密码和密钥、自动认出你的 AstrBot 在哪个网络，
  `docker compose up -d` 起服务，剩下的它全帮你填好
- 🧩 **插件装完不用填任何东西**：容器地址、验证网关地址、链接签名密钥全部自动发现，
  装上重载就能用，不用去配置页面研究每一项是什么意思
- 🍪 **不用手抄 Cookie，也不会隔天就失效**：登录一次即可，Arena 每小时轮换的登录令牌插件会自己续期
- 🎨 **白嫖灰测模型**：正式渠道还没开放的画图模型（蒙娜丽莎就在里面）直接选着用
- 🖼️ **一个命令搞定两种玩法**：`/jjc 提示词` 是文生图，消息里带图或引用带图消息就自动变图生图
- 📝 **预设提示词**：长长的风格提示词存一次，之后 `/jjcp 手办 一只白猫` 一句话出图
- 🛡️ **默认安全**：管理端口只绑本机，验证链接带签名和有效期，Arena 登录态只留在你自己服务器上

> [!IMPORTANT]
> **有问题请进 QQ 群 `460973561` 交流，不点 Star 不给进。**

---

## 🎨 成品长这样（蒙娜丽莎 / GPT-Image-2.5）

<p align="center">
  <img src="docs/examples/mona-sample-promo.png" width="49%" align="middle" alt="蒙娜丽莎最新成品示例 1">
  <img src="docs/examples/mona-sample-promo-2.jpg" width="49%" align="middle" alt="蒙娜丽莎最新成品示例 2"><br>
  <img src="docs/examples/mona-sample-girl.png" width="49%" align="middle" alt="蒙娜丽莎成品示例 1">
  <img src="docs/examples/mona-sample-boy.png" width="49%" align="middle" alt="蒙娜丽莎成品示例 2">
</p>

模型来自 [https://arena.ai](https://arena.ai)（大模型竞技场）。竞技场为了做盲测，会把还没发布的
模型藏在里面给人免费试用，插件做的事就是让你的机器人也能用上这些模型。

## 这套东西是怎么跑起来的（一分钟看懂）

你不需要看懂也能装，但知道了排错会快很多。

```text
你在群里发 /jjc 男孩
        ↓
AstrBot 插件（就是这个仓库根目录的代码）
        ↓
arena-browser 容器：一个装在服务器里的 Chrome 浏览器，它替你登录着 Arena
        ↓
Arena 官网真的画出一张图，再原路发回群里
```

为什么要在服务器里塞一个浏览器？因为 Arena 有 Cloudflare 人机验证，只有真浏览器过得去。
所以流程是：**你远程操作服务器里的那个浏览器，登录一次 Arena，之后插件就一直借用它的登录态**。

默认部署会起两个容器：

| 容器 | 干什么的 | 能不能省 |
| --- | --- | --- |
| `arena-browser` | 服务器里的 Chrome + 远程桌面（你登录用的就是它） | 必须有 |
| `arena-bridge` | 一个中间层 API，把浏览器包装成 HTTP 接口 | **可以省**，见下面「省掉一个容器」 |

## 能用的命令

出图：

- `/jjc 提示词`：**最常用的一个**。文生图；消息带图或引用带图消息时自动走图生图
- `/竞技场画图 提示词`：只做文生图
- `/竞技场图生图 提示词`：只做图生图，支持当前消息图片、引用消息图片、@头像作为参考图
  - 参考图是 GIF/APNG/动图 WebP 时自动取第一帧转 PNG 上传（Arena 不接受动图附件）

挑模型：

- `/竞技场画图模型`：只显示**正式**画图模型（竞技场标了厂商的那些），附带创建时间和最近一次上游结果
- `/竞技场灰测模型`：只显示**灰测（隐身）**模型，**蒙娜丽莎在这个列表里**
- `/竞技场切换模型 编号或模型名`：切换模型，全局生效（所有群聊和私聊共用一个选择）

<details>
<summary>模型列表的一些细节（好奇再点开）</summary>

- 分成两个列表是因为放行灰测模型后画图模型有 170 多个，一条消息塞不下
- 两个列表的编号都是**全局编号**，所以各自看起来不连号，但 `/竞技场切换模型 编号`
  在哪个列表里抄的号都对得上
- 列表按创建时间从新到旧排，新上线的灰测模型排在最前面
- 同一个模型名在竞技场表里往往有好几行（`gpt-image-2 (medium)` 有 5 行），列表只保留
  最新的那一行，不再用不同编号重复刷同一个名字
- 竞技场自己标了「不给用户选」的模型两个列表都不显示，那类在上游就会被拒绝
- 创建时间来自 Arena 模型 id（UUIDv7 前 48 位）；旧的 UUIDv4 模型没有时间，此时不显示
- 报错标注形如 `⚠500 3小时前`／`⚠403 5分钟前`，成功过的显示 `✅`；记录保留 6 小时，
  重启或更新都不会丢

</details>

预设提示词（把长提示词存起来，之后一句话出图）：

- `/jjcp 预设名 补充描述`：用预设画图，补充描述可省略；带图自动图生图
- `/竞技场预设`：查看所有预设（所有人可用）
- `/竞技场预设添加 名字 提示词`（管理员）：保存一条预设，同名覆盖
  - 提示词里的 `{}` 是补充描述插入的位置，也可写 `{prompt}`／`{描述}`／`{补充}`
  - 不写占位符就是纯风格预设，补充描述会自动接在后面
- `/竞技场预设删除 名字`（管理员）
- `/竞技场预设模型 名字 编号或模型名`（管理员）：把预设固定到某个模型，
  之后 `/jjcp` 走这个模型而不是当前模型；`/竞技场预设模型 名字 无` 解绑

状态和登录（管理员）：

- `/竞技场验证`：生成一个短时链接，点开就是服务器里的浏览器，用来登录 Arena、过人机验证
- `/竞技场验证状态`：检查 Arena 登录状态、会话还剩多久
- `/竞技场重新绑定`：登录真的失效时用它重新登录
- `/竞技场状态`：查看服务状态

## 部署教程（小白照抄版）

全程只需要复制粘贴。命令都在**服务器**上执行（SSH 连上去之后）。

### 第 0 步：确认你有这些

- 一台 Linux 服务器，内存 **4 GB 以上**（浏览器吃内存）
- 装好了 Docker 20.10+ 和 Docker Compose v2
  - 不确定装没装就跑一下：`docker -v` 和 `docker compose version`，能打印版本号就行
- 已经能正常聊天的 AstrBot（建议也是 Docker 部署的）
- 服务器防火墙／安全组要**放开 6081 端口**，不然第 5 步的登录链接你点不开
- 一个能登录 [arena.ai](https://arena.ai) 的账号（Google 账号即可）

> 国内服务器直连 Arena 大概率不通，需要代理，第 1 步后面有说明。香港/海外服务器不用管。

### 第 1 步：把仓库拉下来，跑一次初始化

复制这三行：

```bash
git clone https://github.com/cube-lover/astrbot_plugin_arena_image.git
cd astrbot_plugin_arena_image/docker
chmod +x setup.sh && ./setup.sh
```

`setup.sh` 会自动帮你做完这些，**你不需要手填任何一项**：

- 创建配置文件 `.env`
- 生成管理密码、远程桌面密码、链接签名密钥
- 认出服务器的公网 IP
- 认出你的 AstrBot 在哪个 Docker 网络，并写进配置
- 检查一遍配置写得对不对

**国内服务器加代理**（海外服务器跳过）：用编辑器打开 `docker/.env`，找到 `LM_BRIDGE_PROXY_URL=`
这一行，改成你的代理地址，例如

```text
LM_BRIDGE_PROXY_URL=http://127.0.0.1:7890
```

### 第 2 步：启动服务

```bash
docker compose -f docker-compose.arena.yml --env-file .env up -d --build
```

> 第一次要下载 Chromium，**十几分钟很正常**，别以为卡死了。之后再启动只要几秒。

**怎么知道成功了？** 跑这三条，都要有反应：

```bash
docker compose -f docker-compose.arena.yml ps     # 两个容器都是 Up / healthy
curl http://127.0.0.1:8000/api/v1/health          # 返回一段 JSON，里面有模型数量
curl http://127.0.0.1:6081/health                 # 返回 ok
```

如果某个容器一直重启，看日志：`docker logs --tail 50 arena-browser`。

### 第 3 步：安装 AstrBot 插件

**方式 A：插件市场装（推荐，以后能一键更新）**

打开 AstrBot 管理面板 → 插件市场 → 搜索
**「免费gpt灰测画图模型，逆向竞技场臭gaygay插件」**，点安装。
搜不到就用「安装自定义插件」填仓库地址：

```text
https://github.com/cube-lover/astrbot_plugin_arena_image
```

装完点一下重载插件。

**方式 B：手动复制**

```bash
git clone https://github.com/cube-lover/astrbot_plugin_arena_image.git
cp -r astrbot_plugin_arena_image /path/to/astrbot/data/plugins/
rm -rf /path/to/astrbot/data/plugins/astrbot_plugin_arena_image/.git
```

`/path/to/astrbot` 换成你 AstrBot 的实际目录，然后在面板里重载插件。

### 第 4 步：插件配置——**什么都不用填**

这一步是空的，这是故意的。插件的默认值就是对的：

```text
bridge_url = http://arena-bridge:8000    ← 默认值，别动
bridge_api_key = 留空                     ← 就是留空，不是忘了填
```

只有 AstrBot 和这两个容器**不在同一台机器**时才要改 `bridge_url`，
或者**不在同一个 Docker 网络**时按下面「AstrBot 连不上服务」一节处理。

### 第 5 步：登录一次 Arena（只有这一步需要动手）

**私聊**你的机器人（群里不给发链接，因为链接等于交出一个已登录的浏览器），
用 AstrBot 管理员账号发：

```text
/竞技场验证
```

机器人会回一个链接，有效期 15 分钟。用电脑浏览器打开它，你会看到服务器里那个 Chrome 的画面，
像远程桌面一样能操作。在里面做两件事：

1. 如果出现 Cloudflare 的「确认您是真人」，点一下过掉
2. 登录你的 Arena 账号（点 Sign in，用 Google 登录最省事）

登录完了回来发：

```text
/竞技场验证状态
```

看到类似这样就成了：

```text
账号登录：已登录
会话来源：browser_cookies（还有约 55 分钟）
reCAPTCHA 动作：chat_submit
```

> 「还有约 55 分钟」说的是访问令牌的寿命，**不是叫你一小时登一次**。到点插件会自己续期，
> 你只要不主动退出登录就一直有效。

### 第 6 步：出图，玩起来

```text
/jjc 男孩
```

想用蒙娜丽莎（默认模型已经是能出图的了，想换再看这步）：

```text
/竞技场灰测模型          # 找到「蒙娜丽莎」前面的编号，比如 42
/竞技场切换模型 42
/jjc 一只戴墨镜的柴犬
```

改图（发一张图并带上命令，或者引用一张图再发命令）：

```text
/jjc 把这张图改成赛博朋克风格
```

预设提示词：

```text
/竞技场预设添加 手办 1/7 scale figure, PVC statue on round base, studio product photo, {}
/jjcp 手办 一只白猫
/竞技场预设            # 看有哪些预设
```

`{}` 是补充描述插进去的位置；不写 `{}` 就把补充描述接在提示词后面，`/jjcp 手办`
不带补充描述时也能直接出图。默认已内置赛博／手绘／写实／动漫／手办五条，
可在插件配置的 `preset_prompts` 里改，格式是每行 `名字=提示词`。

到这里就装完了。下面的内容都是**出问题**或者**想折腾**的时候再看。

## 省掉一个容器（可选，一个开关的事）

插件现在可以自己直连 `arena-browser`，把 `arena-bridge` 那一层完全绕开。
在插件配置页面把一个开关改掉就行：

```text
transport_mode: bridge  →  direct
```

改完重载插件，就这样，**别的什么都不用填**：模型列表、reCAPTCHA、出图、图生图上传、
验证链接签名全部由插件自己完成，验证网关地址和签名密钥都是插件从 `arena-browser` 里读出来的，
不用手抄，也就不会抄错或过期。

这么做的好处：

- 少一个容器、少一层转发，出图更快
- Cookie 不再复制到任何配置文件，插件直接用浏览器页面的会话，
  所以 `__cf_bm` / `arena-auth-prod-v1` 轮换时不会再出现「Cookie 失效」

想回到原来的链路，把 `transport_mode` 改回 `bridge` 即可，容器都不用动。
详细说明见 [docker/USAGE.md](docker/USAGE.md) 的「通道模式」一节。

## 配置项

一般人只会用到第一项，其余保持默认。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `transport_mode` | `bridge` | `bridge` = 走 arena-bridge 容器；`direct` = 插件直连 arena-browser，不需要 bridge |
| `bridge_url` | `http://arena-bridge:8000` | Bridge 根地址或 `/api/v1` 地址（bridge 模式） |
| `bridge_api_key` | 空 | Bridge API Key，零配置部署留空 |
| `browser_cdp_url` | `http://arena-browser:9223` | 浏览器 CDP 地址（direct 模式，一般不用改；连不上时自动改试 `host.docker.internal`／`172.17.0.1`／`127.0.0.1`，配合 `.env` 的 `ARENA_CDP_BIND=172.17.0.1` 可跨 Docker 网络） |
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

几点容易踩的：

- 输入和输出上限是分开的：模型返回的大图不会因为「输入上限 10 MB」被丢掉，
  只有超过 `max_output_image_bytes` 才失败，超过 `send_image_max_bytes` 时会先压缩再发送
- 模型选择是全局的：`/竞技场切换模型` 一次，所有群聊和私聊都跟着换
- 预设提示词保存在插件数据目录的 `prompt_presets.json`，更新插件不会丢
- Pillow 是 AstrBot 自带的，缺失时只会跳过压缩，不影响画图

## 更新插件

仓库根目录就是插件目录，AstrBot 面板里的插件市场更新和一键更新都能直接用。

如果插件是在服务器上从 git 检出部署的，用仓库自带脚本同步（**只重启 AstrBot，不动 NapCat 登录态**）：

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

Bridge 服务自己要更新时：

```bash
cd /path/to/astrbot_plugin_arena_image/docker
git pull
docker compose -f docker-compose.arena.yml --env-file .env up -d --build
```

## 安全

默认配置已经是安全的，不用额外设置：

- `arena-bridge` 的 `8000` 端口只绑宿主机 `127.0.0.1`，公网连不到
- 远程桌面 `6082` 只绑宿主机 `127.0.0.1`
- 只有 `6081` 对外开放，它发的验证链接带签名和有效期，过期直接 410；
  远程桌面的页面和 WebSocket 也由 `6081` 同端口代理，不会跳到只能本机访问的 `6082`
- CDP 端口 `9223` 默认只绑 `127.0.0.1` 和 Docker 内网
- Arena Cookie、浏览器登录态、密钥文件只保存在你自己服务器上
- `/竞技场验证`、`/竞技场重新绑定`、`/竞技场验证状态`、`/竞技场切换模型`、
  `/竞技场预设添加`、`/竞技场预设删除`、`/竞技场预设模型` 只有 AstrBot 管理员能用；
  验证链接只在私聊发放，群里请求会被拒绝（链接等于交出已登录 Arena 的浏览器）

**这些文件千万别发给别人、别传上公开仓库：**

```text
docker/.env
docker/.interactive-link-secret
docker/.novnc-password
docker/arena-data/
docker/arena-browser-data/
```

## 常见问题

### 提示「Cookie 失效」/ 出图报验证错误

先别急着重新登录，问一下状态，它会告诉你真正的原因：

```text
/竞技场验证状态
```

对着看：

| 状态里写的 | 真正的原因 | 怎么办 |
| --- | --- | --- |
| `账号登录：未登录` | 登录真的没了 | `/竞技场重新绑定`，开链接重新登录 |
| `登录未失效，令牌待续期` | 只是访问令牌到点了 | **什么都不用做**，等十几秒或再发一次命令，插件会自动续期 |
| 会话还在有效期内却报错 | Arena 的人机验证（不是 Cookie 过期） | `/竞技场验证`，在浏览器里过一次验证；重新绑定没用 |
| `reCAPTCHA 动作：sign_up` | `arena-bridge` 镜像太旧 | 重新 `up -d --build` 更新镜像 |

Arena 的登录令牌大约每小时轮换一次，插件（和 Bridge）都会自己跟着换，**不需要定期手动重新绑定**。

### AstrBot 连不上服务（最常见的部署问题）

先确认 AstrBot 容器和 arena 容器在**同一个 Docker 网络**：

```bash
docker inspect astrbot --format '{{json .NetworkSettings.Networks}}'
docker inspect arena-bridge --format '{{json .NetworkSettings.Networks}}'
```

两条命令打印的网络名有交集就说明在一起，插件里的地址填 `http://arena-bridge:8000` 就通。

**不在一起的话，填容器地址是没用的** —— Docker 默认把跨网络的包直接丢掉：填容器名解析不出来，
填容器 IP、填宿主机网关都超时，这不是「地址填错了」。宿主机地址不受这条限制
（publish 出来的端口所有容器都能连，NapCat 映射到服务器端口给 AstrBot 用就是这个原理），
只是 `9223` 默认只绑 `127.0.0.1`。三个办法任选一个：

```bash
# 办法一（推荐）：重跑 setup.sh，它会自动改好 .env 里的网络名，再重启 arena 容器
./setup.sh && docker compose -f docker-compose.arena.yml --env-file .env up -d

# 办法二：把 AstrBot 接进 arena 容器的网络，立即生效，不重启任何容器
docker network connect $(docker inspect arena-browser \
  --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' \
  | awk '{print $1}') astrbot

# 办法三：把 CDP 端口发布到 docker0 网关（本机容器都能连、公网连不到），插件仍然不用填任何东西
echo 'ARENA_CDP_BIND=172.17.0.1' >> docker/.env   # 绝对不要写 0.0.0.0
docker compose -f docker-compose.arena.yml --env-file .env up -d
```

direct 模式连不上 `arena-browser` 时插件会自己试 `host.docker.internal`、`172.17.0.1`、
`127.0.0.1`（`9223` 已绑宿主机 `127.0.0.1`，所以 AstrBot 装在宿主机上也能用；办法三正好
落在插件会试的 `172.17.0.1` 上），全试不通才报错，并把上面办法二那行命令一起给出来。

### `/竞技场验证` 给的链接打不开

- 服务器防火墙／云厂商安全组要放开 **6081** 端口
- 链接只有 15 分钟有效期，过期了重新发一次命令
- `setup.sh` 认的是服务器公网 IP；如果服务器在 NAT 后面，需要在插件配置里把
  `browser_gateway_url` 填成你实际能访问到的地址（`http://你的域名或IP:6081`）

### 模型列表里为什么没有竞技场上的某些模型（比如 nano-banana-pro）

竞技场的模型表里每一行都有一个「厂商」字段，两种情况会被区别对待：

- 厂商为空 = 灰测（隐身）模型，竞技场故意不写，这样你只看到一个代号，猜不出是谁家的。
  这类**默认已经放行**，蒙娜丽莎就是其中之一，它们统一在 `/竞技场灰测模型` 里，
  不在 `/竞技场画图模型` 里。只想留正式模型可以在 `docker/.env` 里设
  `LM_BRIDGE_ALLOW_STEALTH_MODELS=0`。
- 那一行标了「不给用户选」（`userSelectable: false`）= 只参加盲测对战，例如
  `nano-banana-pro`（Gemini 3 Pro Image）。这类不显示也不能指定，因为竞技场上游会直接回
  `Selected model is not available for user selection`，**本地放开没有用**，
  插件里改配置、改代码都绕不过去。

同一个模型名在竞技场表里往往有好几行，只要其中一行可选，这个模型就能正常用。

### 429 Too Many Requests

Arena 上游限速。插件已经会读 `Retry-After` 自动重试 2 次（`rate_limit_retries`），
仍然失败时会直接告诉你要等多久。这时不要连续重试，可换个模型或调低 `.env` 里的 `LM_BRIDGE_RPM`。

### 出图很久没反应 / 提示排队

出图是串行的：同一时间只跑一个请求，其余排队。插件会告诉你前面还有几个任务和大致等待时间，
排队超过 `max_queue_depth`（默认 5）会直接拒绝。单张图正常需要 **30~60 秒**，别急着重发。

### 成图太大发不出去

`max_output_image_bytes`（默认 64 MB）是下载上限，`send_image_max_bytes`（默认 8 MB）是发送阈值，
超过发送阈值会自动压缩/降采样成 JPEG 再发，不会丢掉已经画好的图。

### 国内服务器无法访问 Arena

在 `docker/.env` 里设置 `LM_BRIDGE_PROXY_URL=http://你的代理地址:端口`，然后重启服务：

```bash
docker compose -f docker-compose.arena.yml --env-file .env up -d
```

## 详细文档

完整部署说明、每个环境变量的含义、通道模式细节见 [docker/USAGE.md](docker/USAGE.md)。

## 目录结构

```text
.
├── main.py                   插件入口，AstrBot 加载这个文件
├── arena_direct.py           direct 模式：插件直连浏览器的实现
├── bridge_client.py          bridge 模式：Bridge HTTP 客户端
├── metadata.yaml             插件元数据
├── _conf_schema.json         插件配置项定义
├── requirements.txt
├── cover.png                 插件市场封面
├── docker/                   服务端：LMArenaBridge + 浏览器容器
│   ├── Dockerfile
│   ├── Dockerfile.arena-browser
│   ├── docker-compose.arena.yml
│   ├── setup.sh              一键初始化脚本
│   ├── src/                  Bridge 源码
│   ├── tests/                pytest 测试
│   └── USAGE.md              详细文档
├── docs/examples/            示例成图
└── scripts/deploy-plugin.sh  从 git 检出同步插件到 AstrBot 数据卷
```

`docker/`、`docs/`、`scripts/` 只是服务端和文档，AstrBot 只会加载根目录的 `main.py`，
留着不影响加载，想干净一点可以删掉。

## License

MIT
