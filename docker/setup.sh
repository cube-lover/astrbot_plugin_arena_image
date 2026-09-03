#!/usr/bin/env bash
# 一键初始化：自动生成密码、自动检测服务器公网 IP 和 Docker 网络。
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info() { echo -e "${GREEN}[完成]${NC} $1"; }
warn() { echo -e "${YELLOW}[注意]${NC} $1"; }
fail() { echo -e "${RED}[失败]${NC} $1"; exit 1; }

rand_hex() {
  # 输出 $1 字节的十六进制（即 $1 * 2 个字符）
  openssl rand -hex "$1" 2>/dev/null || od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'
}

detect_public_ip() {
  local url ip
  for url in "https://api.ipify.org" "https://ifconfig.me" "https://icanhazip.com"; do
    ip=$(curl -4 -s --max-time 5 "$url" 2>/dev/null | tr -d '[:space:]') || continue
    if [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      echo "$ip"
      return 0
    fi
  done
  return 1
}

detect_network() {
  if docker network inspect astrbot_default >/dev/null 2>&1; then
    echo "astrbot_default"
    return 0
  fi
  local nets count
  nets=$(docker network ls --filter type=custom --format '{{.Name}}' 2>/dev/null \
    | grep -vE '^(bridge|host|none)$' || true)
  count=$(printf '%s' "$nets" | grep -c . || true)
  if [[ "$count" -eq 1 ]]; then
    echo "$nets"
    return 0
  fi
  return 1
}

command -v docker >/dev/null 2>&1 \
  || fail "未检测到 Docker，请先安装 Docker 再运行本脚本"

# ---------- 1. 创建 .env ----------
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  info "已创建 .env"
else
  info ".env 已存在，跳过创建"
fi

# 统一换行符，避免 Windows 编辑过的文件带 CRLF 导致匹配失败
sed -i 's/\r$//' .env 2>/dev/null || true

# ---------- 2. 自动生成密码 ----------
# 零配置模式：LM_BRIDGE_API_KEY 保持为空，Bridge 只监听
# 127.0.0.1 和 Docker 内网，不需要填写 API Key。
if grep -q 'CHANGE_ME' .env; then
  sed -i "s|^LM_BRIDGE_ADMIN_PASSWORD=.*|LM_BRIDGE_ADMIN_PASSWORD=$(rand_hex 16)|" .env
  sed -i "s|^LM_BRIDGE_API_KEY=.*|LM_BRIDGE_API_KEY=|" .env
  # x11vnc 密码最长 8 位
  sed -i "s|^ARENA_NOVNC_PASSWORD=.*|ARENA_NOVNC_PASSWORD=$(rand_hex 4)|" .env
  info "已自动生成管理密码和 VNC 密码"
else
  info "密码和 API Key 已配置，跳过生成"
fi

# ---------- 3. 自动检测服务器公网 IP ----------
if grep -q 'http://HOST:' .env; then
  if PUBLIC_IP=$(detect_public_ip); then
    sed -i "s|http://HOST:6081|http://${PUBLIC_IP}:6081|g" .env
    sed -i "s|http://HOST:6082|http://${PUBLIC_IP}:6082|g" .env
    info "已自动填入服务器公网 IP"
  else
    warn "无法自动获取公网 IP，请手动把 .env 里的 HOST 替换成你的服务器 IP 或域名"
  fi
else
  info "服务器地址已配置，跳过检测"
fi

# ---------- 4. 自动检测 Docker 网络 ----------
if grep -qE '^ASTRBOT_NETWORK=astrbot_default$' .env; then
  if NET=$(detect_network); then
    sed -i "s|^ASTRBOT_NETWORK=.*|ASTRBOT_NETWORK=${NET}|" .env
    info "已自动填入 Docker 网络：${NET}"
  else
    warn "无法自动确定 AstrBot 所在 Docker 网络，请手动修改 .env 里的 ASTRBOT_NETWORK"
    echo "当前所有 Docker 网络："
    docker network ls
  fi
else
  info "Docker 网络已配置，跳过检测"
fi

# ---------- 4.1 兼容旧版 .env ----------
if ! grep -q '^LM_BRIDGE_PROXY_URL=' .env; then
  cat >> .env <<'EOF'

# Optional HTTP/HTTPS proxy for servers in mainland China.
# Leave empty on servers that can reach Arena directly.
LM_BRIDGE_PROXY_URL=
EOF
  info "已补充可选代理配置 LM_BRIDGE_PROXY_URL"
fi

# ---------- 5. 生成密钥文件 ----------
if [[ ! -f .interactive-link-secret ]]; then
  rand_hex 32 > .interactive-link-secret
  chmod 600 .interactive-link-secret
  info "已生成 .interactive-link-secret"
else
  info ".interactive-link-secret 已存在"
fi

if [[ ! -f .novnc-password ]]; then
  rand_hex 4 > .novnc-password
  chmod 600 .novnc-password
  info "已生成 .novnc-password"
else
  info ".novnc-password 已存在"
fi

# ---------- 6. 校验 ----------
docker compose -f docker-compose.arena.yml --env-file .env config --quiet \
  || fail "Docker Compose 配置校验未通过，请检查 .env"
info "Docker Compose 配置校验通过"

# ---------- 7. 输出下一步操作 ----------
cat <<EOF

==============================================
 初始化完成！接下来只需 3 步：

 1. 启动服务：
    docker compose -f docker-compose.arena.yml --env-file .env up -d --build

 2. 把 astrbot_plugin_arena_image/ 目录复制到 AstrBot 插件目录，
    插件默认已指向 http://arena-bridge:8000，API Key 留空即可。

 3. 在聊天里发送：
      /竞技场验证
    打开返回的链接完成 Arena 登录，然后发送：
      /竞技场验证状态

 看到「已登录」后就可以用 /jjc 提示词 画图了。
==============================================
EOF
