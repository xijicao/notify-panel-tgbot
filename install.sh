#!/usr/bin/env bash
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"; export NOTIFY_PANEL_SCRIPT_DIR="$SCRIPT_DIR"; { printf '%s\n' 'set -euo pipefail'; tail -n +3 "$0"; } | tr -d '\r' | bash -s -- "$@"; exit $? # 兼容 GitHub 的 CRLF 换行

# Debian 12, system Python standard library only; no third-party dependencies.
# 这里不安装第三方依赖，降低 VPS 资源占用。

APP_NAME="notify-panel"
APP_DIR="/opt/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
if [ -n "${NOTIFY_PANEL_SCRIPT_DIR:-}" ]; then
  SCRIPT_DIR="$NOTIFY_PANEL_SCRIPT_DIR"
else
  SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 运行：sudo bash install.sh"
  exit 1
fi

if [ ! -f "$SCRIPT_DIR/app.py" ]; then
  echo "找不到 $SCRIPT_DIR/app.py，请确认源码完整。"
  exit 1
fi

apt-get update
apt-get install -y python3
install -d -m 0755 "$APP_DIR"
install -m 0644 "$SCRIPT_DIR/app.py" "$APP_DIR/app.py"

if [ ! -f "$APP_DIR/config.env" ]; then
  SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  read -r -p "Telegram Bot Token: " TG_BOT_TOKEN
  read -r -p "Telegram Chat ID: " TG_CHAT_ID
  read -r -p "每天几点检查提醒 [09:00]: " CHECK_TIME
  CHECK_TIME="${CHECK_TIME:-09:00}"
  cat > "$APP_DIR/config.env" <<EOF
ADMIN_USERNAME=admin
ADMIN_PASSWORD=unused
AUTH_ENABLED=false
SECRET_KEY=${SECRET_KEY}
TG_BOT_TOKEN=${TG_BOT_TOKEN}
TG_CHAT_ID=${TG_CHAT_ID}
HOST=127.0.0.1
PORT=8000
CHECK_TIME=${CHECK_TIME}
PUSH_ON_START=true
PUSH_LOG_RETENTION_DAYS=90
EOF
  chmod 600 "$APP_DIR/config.env"
fi

# 直接生成面板自己的 systemd 单元，避免不同环境下复制 service 文件失败。
install -d -m 0755 "$(dirname "$SERVICE_FILE")"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Telegram Notify Panel
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/app.py
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$SERVICE_FILE"
if [ ! -s "$SERVICE_FILE" ]; then
  echo "无法创建 $SERVICE_FILE"
  exit 1
fi
systemctl daemon-reload
systemctl enable "$APP_NAME.service"
systemctl restart "$APP_NAME.service" 2>/dev/null || systemctl start "$APP_NAME.service"
systemctl --no-pager --full status "$APP_NAME.service" || true

echo
echo "安装完成：ssh -L 8000:127.0.0.1:8000 root@你的 VPS_IP"
echo "本地打开：http://127.0.0.1:8000"
echo "配置文件：$APP_DIR/config.env"
echo "数据文件：$APP_DIR/data.sqlite"
