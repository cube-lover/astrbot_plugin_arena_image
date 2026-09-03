# astrbot_plugin_arena_image

> [!IMPORTANT]
> **有问题请进 QQ 群 `460973561` 交流，不点 Star 不给进。**

---

## 🎨 支持 Arena 灰测模型：蒙娜丽莎（GPT-Image-2.5）

安装并完成 Arena 登录后，可在模型列表中选择 **「蒙娜丽莎 / GPT-Image-2.5」**，文生图和图生图均可调用。

<p align="center">
  <img src="../docs/examples/mona-sample-promo.png" width="49%" alt="蒙娜丽莎最新成品示例 1">
  <img src="../docs/examples/mona-sample-promo-2.jpg" width="49%" alt="蒙娜丽莎最新成品示例 2"><br>
  <img src="../docs/examples/mona-sample-girl.png" width="49%" alt="蒙娜丽莎成品示例 1">
  <img src="../docs/examples/mona-sample-boy.png" width="49%" alt="蒙娜丽莎成品示例 2">
</p>



通过 LMArenaBridge 为 AstrBot 提供 Arena 画图能力：文生图、图生图、模型列表、按会话切换模型、Arena 登录态自动采集。

## 安装

本插件需要配合仓库里的 `docker/` 服务使用。完整部署流程见仓库根目录 [README](../README.md) 和 [docker/USAGE.md](../docker/USAGE.md)。

把插件安装到 AstrBot 后，默认配置为：

```text
bridge_url = http://arena-bridge:8000
bridge_api_key = 留空
```

## 指令

```text
/jjc 提示词
/竞技场画图 提示词
/竞技场图生图 提示词
/竞技场画图模型
/竞技场状态
```

管理员专用（`/竞技场验证`、`/竞技场重新绑定` 还必须在私聊里发）：

```text
/竞技场切换模型 编号或模型名
/竞技场验证
/竞技场验证状态
/竞技场重新绑定
```

`/jjc` 是统一命令：无图时走文生图，消息带图/引用带图/@头像时自动走图生图。

每个群聊或私聊独立保存模型选择。

### 权限与验证链接

- 切换模型、Arena 登录/重新绑定属于全局副作用，只有 AstrBot 管理员可以执行。
- `/竞技场验证` 返回的服务器浏览器链接等于把已登录 Arena/Google 会话的浏览器交出去，
  因此只在私聊发放；在群里执行会被拒绝，`/竞技场验证状态` 在群里只回状态不回链接。

## 配置项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `bridge_url` | `http://arena-bridge:8000` | Bridge 根地址或 `/api/v1` 地址 |
| `bridge_api_key` | 空 | Bridge API Key，零配置部署留空 |
| `default_model` | `gpt-image-2 (medium)` | 会话未选模型时使用 |
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

输入和输出上限是分开的：模型返回的大图不会再因为“输入上限 10 MB”被丢掉，
只有超过 `max_output_image_bytes` 才失败，而超过 `send_image_max_bytes` 时会先压缩再发送。

## 需求

- AstrBot
- 已部署同仓库的 LMArenaBridge Docker 服务
- Pillow（AstrBot 自带；缺失时只会跳过压缩，不影响画图）
