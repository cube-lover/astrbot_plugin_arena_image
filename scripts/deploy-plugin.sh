#!/usr/bin/env bash
# 把当前 git 检出里的 astrbot_plugin_arena_image 同步到 AstrBot 数据卷。
# 插件位于仓库子目录，AstrBot 的一键更新识别不了这种布局，用本脚本更新。
set -euo pipefail

CONTAINER="${ASTRBOT_CONTAINER:-astrbot}"
VOLUME="${ASTRBOT_VOLUME:-astrbot_astrbot_data}"
DATA_DIR="${ASTRBOT_DATA_DIR:-}"
PLUGIN_NAME="astrbot_plugin_arena_image"
DRY_RUN=0
RESTART=1

usage() {
    cat <<'EOF'
用法: scripts/deploy-plugin.sh [选项]

  --container NAME   AstrBot 容器名（默认 astrbot）
  --volume NAME      AstrBot 数据卷名（默认 astrbot_astrbot_data）
  --data-dir PATH    直接指定宿主机上的 AstrBot data 目录
  --dry-run          只打印将要执行的操作
  --no-restart       同步后不重启 AstrBot 容器
  -h, --help         显示本帮助
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --container) CONTAINER="$2"; shift 2 ;;
        --volume) VOLUME="$2"; shift 2 ;;
        --data-dir) DATA_DIR="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --no-restart) RESTART=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
    esac
done

log() { printf '[deploy] %s\n' "$*"; }
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[dry-run] %s\n' "$*"
        return 0
    fi
    "$@"
}

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE_DIR="$REPO_ROOT/$PLUGIN_NAME"

[ -f "$SOURCE_DIR/main.py" ] || { echo "找不到插件源码: $SOURCE_DIR" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "需要 docker 命令" >&2; exit 1; }

if [ -z "$DATA_DIR" ]; then
    DATA_DIR="$(docker inspect "$CONTAINER" \
        --format '{{range .Mounts}}{{if eq .Destination "/AstrBot/data"}}{{.Source}}{{end}}{{end}}' \
        2>/dev/null || true)"
fi
if [ -z "$DATA_DIR" ]; then
    DATA_DIR="$(docker volume inspect "$VOLUME" --format '{{.Mountpoint}}' 2>/dev/null || true)"
fi
[ -n "$DATA_DIR" ] || { echo "无法定位 AstrBot data 目录，请用 --data-dir 指定" >&2; exit 1; }
[ -d "$DATA_DIR/plugins" ] || {
    echo "不像 AstrBot data 目录（缺少 plugins/）: $DATA_DIR" >&2
    exit 1
}

TARGET_DIR="$DATA_DIR/plugins/$PLUGIN_NAME"
BACKUP_DIR="$DATA_DIR/plugins_backup/$PLUGIN_NAME-$(date +%Y%m%d-%H%M%S)"
VERSION="$(sed -n 's/^version:[[:space:]]*//p' "$SOURCE_DIR/metadata.yaml" | head -n 1)"

log "仓库:      $REPO_ROOT"
log "data 目录: $DATA_DIR"
log "目标:      $TARGET_DIR"
log "版本:      ${VERSION:-未知}"

if [ -d "$TARGET_DIR" ]; then
    log "备份现有插件到 $BACKUP_DIR"
    run mkdir -p "$(dirname "$BACKUP_DIR")"
    run cp -a "$TARGET_DIR" "$BACKUP_DIR"
    # 早期手工部署留下的 main.py.bak-* 不会被加载，但会干扰排查
    for stale in "$TARGET_DIR"/main.py.bak-*; do
        [ -e "$stale" ] || continue
        log "移除历史备份文件 $(basename "$stale")"
        run rm -f "$stale"
    done
fi

run mkdir -p "$TARGET_DIR"
if command -v rsync >/dev/null 2>&1; then
    run rsync -a --delete \
        --exclude '__pycache__/' \
        --exclude '*.pyc' \
        --exclude 'main.py.bak-*' \
        "$SOURCE_DIR/" "$TARGET_DIR/"
elif [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] tar 同步 %s -> %s\n' "$SOURCE_DIR" "$TARGET_DIR"
else
    log "没有 rsync，改用 tar 同步"
    find "$TARGET_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    tar -C "$SOURCE_DIR" \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='main.py.bak-*' \
        -cf - . | tar -C "$TARGET_DIR" -xf -
fi

if [ "$RESTART" -eq 1 ]; then
    # 只重启 AstrBot：NapCat 等容器的登录态必须保留
    log "重启容器 $CONTAINER"
    run docker restart "$CONTAINER" >/dev/null
    run sleep 8
    if [ "$DRY_RUN" -eq 0 ]; then
        docker logs --tail 60 "$CONTAINER" 2>&1 | grep -i 'arena_image' || true
    fi
else
    log "已跳过重启，改动在 AstrBot 重启或重载插件后生效"
fi

log "完成：$PLUGIN_NAME ${VERSION:-} -> $TARGET_DIR"
