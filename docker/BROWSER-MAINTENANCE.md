# 服务器浏览器维护

`arena-browser` 仍长期运行在服务器上；本次更新不要求改成本地浏览器。

## 版本与配置

- 镜像通过 Google 官方 HTTPS APT 源及独立签名 keyring 安装 `google-chrome-stable`，优先启动该可执行文件，不再优先选择旧 Playwright Chromium。
- 安装中文 locale、CJK/emoji 字体；启动语言为 `zh-CN`。浏览器 profile 里的语言列表属于用户持久化设置，可在 Chrome 设置中调整；不要把 profile 或 Preferences 原文件提交到仓库。
- 使用 Compose 已有的 1 GB `/dev/shm`，移除 `--disable-dev-shm-usage` 和 `--disable-background-networking`。
- 不设置 `AutomationControlled` 覆盖，不添加伪造 UA 或指纹补丁。
- Chrome 内部调试端口只监听 loopback，origin 仅允许本地；保留容器内 9223 代理。宿主机 CDP 仍默认绑定 loopback，不能暴露公网。
- 保留已有认证 HTTP(S) 代理的本地 relay 接线；Chrome 不直接使用含用户名/密码的代理 URL，日志不输出代理地址。
- 保留 `/data/browser/chrome-profile` 持久化路径、登录资料与会话恢复，并显式设置 Arena 启动页。
- Bridge 配置 `init: true`，持续回收被 PID1 领养的孤儿进程；不通过定时重启或按进程名批量杀进程来清理。

`google-chrome-stable` 在**构建镜像时**安装当时的稳定版本，并非运行中的自动更新。浏览器仍沿用原 root / `--no-sandbox` 模型，本次不是完整的容器权限或沙箱迁移。

## 更新已有部署

先确认没有正在生成、投票或手动验证的任务。构建新镜像不必先停止旧浏览器：

```bash
cd /path/to/deployment
# 先将审阅后的 Dockerfile.arena-browser、arena-browser-entrypoint.sh
# 及 Compose 的 bridge init 设置同步到部署目录，保留自己的 .env 和 secrets。
docker compose -f docker-compose.arena.yml build arena-browser
```

替换浏览器前，应在维护窗口停止**该浏览器服务**并做完整冷备份。将 profile 和旧镜像保存在私有目录；浏览器关闭后再归档，不能一边写 profile 一边把它当作可靠备份。不要删除、清空或发布 `arena-browser-data/`。

确认备份可读且校验通过后，只替换浏览器服务：

```bash
docker compose -f docker-compose.arena.yml up -d --no-deps arena-browser
```

Bridge 的 `init: true` 只有在重建该服务时生效；等待其请求结束后，可仅重建 Bridge，保持原镜像与数据：

```bash
docker compose -f docker-compose.arena.yml up -d --no-build --no-deps --force-recreate arena-bridge
```

不要因为更新浏览器而同时重启 AstrBot、QQ 或其它容器。检查 Chrome 实际版本、原登录、已有 Arena 页面、CDP 和 Bridge 健康状态；这些环境检查不是生成或投票成功证明。

### 回滚

保留旧镜像。新 Chrome 写过的 profile 不应直接交给旧 Chrome：先另存失败后的 profile，再恢复升级前的冷备份和旧镜像。不要恢复过期 Cookie 文本作为“修复”。

## 验证边界

这项维护改善浏览器版本、字体及进程生命周期，**不保证 reCAPTCHA 免验证、勾选即可通过或每次生成成功**。需要人工验证时仍由用户在服务器浏览器中完成；不要连续重放生成请求寻找一次成功。

公开仓库不包含 `.env`、网关/VNC 密钥、Cookie、API Key、浏览器登录态、生产会话记录或部署备份。
