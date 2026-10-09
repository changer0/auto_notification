#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REMOTE_HOST="berry"
REMOTE_DIR="/home/berry/auto_notification"

for command_name in ssh rsync; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        printf '错误：找不到命令 %s\n' "$command_name" >&2
        exit 1
    fi
done

if [[ ! -f "$SCRIPT_DIR/notify.py" || ! -f "$SCRIPT_DIR/README.md" ]]; then
    printf '错误：脚本目录缺少 notify.py 或 README.md：%s\n' "$SCRIPT_DIR" >&2
    exit 1
fi

printf '准备同步 %s 到 %s:%s\n' "$SCRIPT_DIR" "$REMOTE_HOST" "$REMOTE_DIR"
ssh "$REMOTE_HOST" "mkdir -p -- '$REMOTE_DIR'"

# 不删除远端文件；保留 berry 上的本地配置、运行日志和专用脚本。
rsync -av \
    --exclude='/.git/' \
    --exclude='/__pycache__/' \
    --exclude='/.video_agent/' \
    --exclude='/.zcodeignore' \
    --exclude='/pending/' \
    --exclude='retry_queue.log*' \
    --exclude='retry_thsottiaux.log*' \
    --exclude='config.ini' \
    --exclude='*.local.ini' \
    --exclude='*.secret.ini' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    "$SCRIPT_DIR/" "$REMOTE_HOST:$REMOTE_DIR/"

ssh "$REMOTE_HOST" "test -x '$REMOTE_DIR/notify.py' && test -f '$REMOTE_DIR/README.md'"
printf '同步完成：%s:%s\n' "$REMOTE_HOST" "$REMOTE_DIR"
