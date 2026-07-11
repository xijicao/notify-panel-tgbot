# Telegram 通知面板

适合 1C1G Debian VPS 的轻量级 Telegram 到期提醒面板，使用 Python 标准库、SQLite 和 Telegram Bot API，不使用 Docker，也不安装第三方 Python 依赖。

## 一键安装（推荐）

无论当前在哪个目录，都可以直接执行：

```bash
curl -fsSL https://raw.githubusercontent.com/xijicao/notify-panel-tgbot/main/install-panel.sh -o /tmp/notify-panel-install.sh
bash /tmp/notify-panel-install.sh
```

安装器会自动：

1. 检查 root 权限；
2. 安装 Git（如果系统没有）；
3. 将源码下载或更新到 `/opt/notify-panel-src`；
4. 安装程序到 `/opt/notify-panel`；
5. 注册并启动 `notify-panel.service`。

这条命令不依赖 `/root/install.sh`，不会误调用其他项目的安装脚本。

## 手动安装

```bash
apt-get update
apt-get install -y git ca-certificates
git clone https://github.com/xijicao/notify-panel-tgbot.git /opt/notify-panel-src
bash /opt/notify-panel-src/install.sh
```

安装脚本会自动定位自身目录，因此不需要先 `cd` 到源码目录。

首次安装时会询问 Telegram Bot Token、Chat ID 和每日检查时间。配置保存在：

```text
/opt/notify-panel/config.env
```

## SSH 隧道访问

面板默认只监听 `127.0.0.1`，不需要开放 8000 端口。在本地电脑执行：

```bash
ssh -L 8000:127.0.0.1:8000 root@你的 VPS_IP
```

然后打开 <http://127.0.0.1:8000>。

## 服务管理

```bash
systemctl status notify-panel --no-pager
journalctl -u notify-panel -n 100 --no-pager
systemctl restart notify-panel
```

## 更新

以后仍然使用同一条一键命令即可。安装器检测到 `/opt/notify-panel-src` 已存在时，会先执行 `git pull --ff-only`，然后更新程序并重启服务；不会覆盖现有配置和数据库。

## 备份

只需备份：

```text
/opt/notify-panel/config.env
/opt/notify-panel/data.sqlite
```

不要把这两个文件提交到公开 GitHub 仓库。
