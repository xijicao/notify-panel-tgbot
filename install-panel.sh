#!/usr/bin/env bash
{ printf '%s\n' 'set -Eeuo pipefail'; tail -n +3 "$0"; } | tr -d '\r' | bash -s -- "$@"; exit $? # 兼容 GitHub 的 CRLF 换行

# 独立的面板引导安装器：不依赖当前目录，也不会与其他项目的 install.sh 混淆。
REPO_URL="https://github.com/xijicao/notify-panel-tgbot.git"
SRC_DIR="${NOTIFY_PANEL_SRC_DIR:-/opt/notify-panel-src}"

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 root 运行此安装器。"
  exit 1
fi

echo "[notify-panel] 检查安装环境..."
if ! command -v git >/dev/null 2>&1; then
  apt-get update
  apt-get install -y git ca-certificates
fi

if [ -e "$SRC_DIR" ] && [ ! -d "$SRC_DIR/.git" ]; then
  echo "错误：$SRC_DIR 已存在但不是本项目的 Git 目录。"
  echo "请更换 NOTIFY_PANEL_SRC_DIR，或先人工检查该目录。"
  exit 1
fi

if [ -d "$SRC_DIR/.git" ]; then
  echo "[notify-panel] 更新已有源码：$SRC_DIR"
  git -C "$SRC_DIR" pull --ff-only
else
  echo "[notify-panel] 下载源码：$SRC_DIR"
  git clone --depth 1 "$REPO_URL" "$SRC_DIR"
fi

echo "[notify-panel] 开始安装..."
cd "$SRC_DIR"
exec bash "$SRC_DIR/install.sh"
