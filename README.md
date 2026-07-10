# Telegram 通知面板

一个适合 1C1G Debian VPS 的到期提醒面板，使用 Python 标准库、SQLite 和 Telegram Bot API。

## 特点

不使用 Docker，也不安装第三方 Python 依赖。

Web 面板只监听 `127.0.0.1`，通过 SSH 隧道访问。

Telegram 支持 `/items`、`/items 30` 和 `/help`。

支持每天、每周、每月、每季度、每年和自定义天数周期。

`push_log` 默认只保留 90 天，避免数据库无限增长。

旧数据库中的分类、金额、备注字段仍然兼容；新增提醒页面默认只显示常用字段。

## 安装

项目地址：<https://github.com/xijicao/notify-panel-tgbot>

首次安装：

```bash
cd /root
git clone https://github.com/xijicao/notify-panel-tgbot.git notify-panel-src
cd notify-panel-src
sudo bash install.sh
```

安装脚本会创建 `/opt/notify-panel`，生成 `config.env`，并注册 `notify-panel.service`。

## SSH 隧道

VPS 上不需要开放 8000 端口：

```bash
ssh -L 8000:127.0.0.1:8000 root@你的 VPS_IP
```

然后在本地浏览器打开 <http://127.0.0.1:8000>。

## giffgaff 保号示例

选择“自定义天数”，设置为 `180`，不要选择“每月”。点击“完成”后，下次日期会固定顺延 180 天。

```text
名称：giffgaff 保号
提前提醒：7 天
重复周期：自定义天数
自定义天数：180
```

## 日常维护

```bash
systemctl status notify-panel
journalctl -u notify-panel -n 100 --no-pager
systemctl restart notify-panel
```

如果希望缩短数据库清理周期，在 `/opt/notify-panel/config.env` 设置：

```env
PUSH_LOG_RETENTION_DAYS=60
```

## 备份

只需要备份以下两个文件：

```text
/opt/notify-panel/config.env
/opt/notify-panel/data.sqlite
```

请勿将 `config.env` 或 `data.sqlite` 提交到公开 GitHub 仓库。

## 升级

以后可以直接从 GitHub 更新。安装目录中的配置和数据库不会被覆盖：

```bash
cd /root/notify-panel-src
git pull --ff-only
sudo bash install.sh
```

脚本会保留现有配置和数据库，只替换程序并重启服务。
