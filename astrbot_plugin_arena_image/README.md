# astrbot_plugin_arena_image

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
/竞技场切换模型 编号或模型名
/竞技场状态
/竞技场验证
/竞技场验证状态
/竞技场重新绑定
```

`/jjc` 是统一命令：无图时走文生图，消息带图/引用带图/@头像时自动走图生图。

每个群聊或私聊独立保存模型选择。

## 需求

- AstrBot
- 已部署同仓库的 LMArenaBridge Docker 服务
