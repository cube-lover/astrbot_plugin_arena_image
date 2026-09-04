# LMArenaBridge + AstrBot 画图插件

## 🎨 支持 Arena 灰测模型：蒙娜丽莎（GPT-Image-2.5）

可在模型列表中选择 **「蒙娜丽莎 / GPT-Image-2.5」**，文生图和图生图均可调用。

本项目把 LMArenaBridge 和 AstrBot 画图插件组合成一条本地服务链：

```text
AstrBot ── Docker network ──> arena-bridge ──> Arena
```

插件通过 Bridge 的 OpenAI 兼容接口获取模型列表，并提供文生图、图生图、会话级模型切换和状态查询。

## 功能

| 指令 | 作用 |
| --- | --- |
| `/竞技场画图 提示词` | 使用当前模型生成图片 |
| `/竞技场图生图 提示词` | 读取当前消息或引用消息中的图片后生成 |
| `/竞技场模型列表` | 刷新并显示 Bridge 返回的模型列表 |
| `/竞技场切换模型 模型名或编号` | 为当前群聊/私聊保存模型选择 |
| `/竞技场状态` | 查看 Bridge、模型数量和当前模型 |
| `/竞技场验证` | 打开服务器浏览器，手动完成 Arena/Cloudflare 验证 |
| `/竞技场重新绑定` | Cookie 缺失或过期时重新获取并自动保存 Arena 会话 |
| `/竞技场验证状态` | 检查验证结果并保存服务器浏览器会话 |

命令使用“竞技场”前缀，避免与服务器上已有的其他生图插件命令冲突；
每个命令同时提供对应的 `arena...` 短别名。

生成结果会先下载到 AstrBot 插件数据目录，再以本地图片消息发送；旧结果会按配置自动清理。

## Bridge 部署

### 1. 准备网络和环境变量

先确认 AstrBot 所在 Docker 网络名称：

```bash
docker network ls
```

复制环境模板并填写强密码和 API Key：

```bash
cp .env.example .env
```

`ASTRBOT_NETWORK` 必须是 AstrBot 实际使用的网络名。两个容器加入同一个网络后，AstrBot 才能通过 `http://arena-bridge:8000` 访问 Bridge。

### 2. 启动

```bash
docker compose -f docker-compose.arena.yml --env-file .env up -d --build
docker compose -f docker-compose.arena.yml logs -f arena-bridge
```

配置和模型缓存持久化在项目目录的 `arena-data/`。有效 API Key 同步保存到：

```text
arena-data/api-key.txt
```

入口脚本不会把 API Key 写入日志。首次未设置 `LM_BRIDGE_API_KEY` 时，可用下面的命令读取自动生成的 Key：

```bash
docker compose -f docker-compose.arena.yml exec arena-bridge \
  sh -c 'cat /data/api-key.txt'
```

### 3. 初始化 Arena 会话

Bridge 管理页面只绑定到宿主机回环地址，建议通过 SSH 隧道访问：

```bash
ssh -L 8000:127.0.0.1:8000 USER@HOST
```

然后打开 <http://127.0.0.1:8000/login>，使用 `LM_BRIDGE_ADMIN_PASSWORD` 登录，在 Dashboard 中维护 Arena auth token 并刷新模型列表。不要把 token 写入仓库、聊天记录或公开日志。

健康检查：

```bash
curl http://127.0.0.1:8000/api/v1/health
curl -H "Authorization: Bearer YOUR_BRIDGE_API_KEY" \
  http://127.0.0.1:8000/api/v1/models
```

`degraded` 表示服务进程已启动但模型或会话数据尚未准备好；容器健康检查会将 `healthy` 和 `degraded` 都视为可继续启动的状态。

如果 Arena 返回 Cloudflare/Turnstile 验证，插件会提示运行
`/竞技场验证`。该命令会在服务器浏览器中打开 Arena，并返回一个短时
签名链接；打开后点击“进入验证浏览器”即可使用服务器上的 noVNC，
不需要在群消息中传递 VNC 密码。服务器浏览器会话中的
`cf_clearance`、`__cf_bm` 和 `arena-auth-prod-v1` 会自动保存到
`arena-data/config.json`，一次性的 Turnstile token 不会持久化。

Cookie 缺失或过期时可直接运行 `/竞技场重新绑定`。完成 Arena 登录、
CF/Turnstile 验证后，Bridge 会自动读取新的会话 Cookie，写入浏览器
Cookie 存储，并在后续请求中优先使用；如果后台刷新成功，则不需要
再次手动登录。

启用免输密码的验证链接时，先在项目根目录执行：

```bash
./setup.sh
```

脚本会创建 `.env`、`.interactive-link-secret` 和 `.novnc-password`。
随后设置：

```dotenv
LM_BRIDGE_BROWSER_GATEWAY_URL=http://HOST:6081
```

短时签名服务内置于 `arena-browser`，不再需要独立 gateway 容器。它会
验证签名链接，并在同一个 `6081` 端口下代理 noVNC 页面和 WebSocket；
密码不会进入 QQ/AstrBot 消息、HTTP 请求路径或网关日志。若未配置网关，
Bridge 仍兼容返回原始 noVNC 地址。

## AstrBot 插件安装

推荐在 AstrBot 插件市场里直接安装（仓库根目录就是插件目录）。手动安装时，把仓库根目录复制成：

```text
data/plugins/astrbot_plugin_arena_image/
```

目录至少应包含：

```text
metadata.yaml
_conf_schema.json
requirements.txt
main.py
bridge_client.py
```

在 AstrBot 管理面板中重载插件并填写：

| 配置项 | 同一 Docker 网络 | AstrBot 在宿主机 |
| --- | --- | --- |
| `bridge_url` | `http://arena-bridge:8000` | `http://127.0.0.1:8000` |
| `bridge_api_key` | `.env` 中的 Key 或 `arena-data/api-key.txt` | 同左 |
| `default_model` | `gpt-image-2 (medium)` | 同左 |

插件启动时不会主动触发 Arena 请求；执行 `/竞技场画图模型` 时才会刷新模型列表。

## 本地测试

在项目根目录执行：

```powershell
python -m compileall -q src astrbot_plugin_arena_image docker-entrypoint.py
python -m pytest tests/test_arena_image_plugin.py -q
python -m pytest tests/ -q
docker compose -f docker-compose.arena.yml --env-file .env.example config --quiet
docker build -t lmarenabridge:local .
```

原项目中的浏览器测试需要 `camoufox`、`playwright` 及其浏览器运行时；插件专项测试不依赖 AstrBot 运行时，可在 Bridge 源码环境中直接执行。

## 目录说明

- `src/`：LMArenaBridge FastAPI 服务。
- 仓库根目录：AstrBot 插件本体（`main.py`、`bridge_client.py`、`metadata.yaml` 等）。
- `Dockerfile`、`docker-compose.arena.yml`：Bridge 容器化配置。
- `docker-entrypoint.py`：初始化 `/data/config.json`、`/data/models.json` 和 API Key 文件。
- `/data/model_health.json`：每个模型最近一次上游结果（500/403/超时/成功），由 Bridge 自动
  写入并在启动时读回，保留 6 小时，用于 `/api/v1/model-health` 和插件的模型列表标注。
- `tests/test_arena_image_plugin.py`：Bridge 客户端、入口初始化和插件清单测试。

## 运行边界

- 插件只使用 Bridge `/api/v1/models` 实际返回的模型，不内置伪造的模型列表。
- 只有 Bridge 返回图片输出能力的模型才适合 `/画图`；模型返回纯文本时，插件会把文本错误明确反馈。
- 图生图参考图会转换为 Data URI，仅在插件到 Bridge 的请求中使用，不写入日志。
- `docker-compose.arena.yml` 将宿主机端口限制为 `127.0.0.1:8000`，如需外部访问应使用 SSH 隧道或经过认证的反向代理。
