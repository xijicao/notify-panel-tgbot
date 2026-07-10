from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.env"
DB_PATH = APP_DIR / "data.sqlite"
COOKIE_NAME = "notify_panel_session"

CATEGORIES = ["手机卡", "银行转账", "VPS", "域名证书", "其他"]
REPEATS = {
    "none": "不循环",
    "daily": "每天",
    "weekly": "每周",
    "monthly": "每月",
    "quarterly": "每季度",
    "yearly": "每年",
    "custom_days": "自定义天数",
}


def ensure_config_file() -> None:
    if CONFIG_PATH.exists():
        return
    secret_key = secrets.token_urlsafe(32)
    CONFIG_PATH.write_text(
        "\n".join(
            [
                "ADMIN_USERNAME=admin",
                "ADMIN_PASSWORD=请改成强密码",
                "AUTH_ENABLED=false",
                f"SECRET_KEY={secret_key}",
                "TG_BOT_TOKEN=",
                "TG_CHAT_ID=",
                "HOST=127.0.0.1",
                "PORT=8000",
                "CHECK_TIME=09:00",
                "PUSH_ON_START=true",
                "PUSH_LOG_RETENTION_DAYS=90",
                "",
            ]
        ),
        encoding="utf-8",
    )


def load_config() -> dict[str, str]:
    ensure_config_file()
    config: dict[str, str] = {}
    for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        config[key.strip()] = value.strip()
    for key, value in os.environ.items():
        if key in config:
            config[key] = value
    return config


CONFIG = load_config()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Single writer process; WAL and busy_timeout reduce transient SQLite locks.
    # 这个面板数据量很小，保留标准库实现即可。

    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '其他',
                due_date TEXT NOT NULL,
                lead_days INTEGER NOT NULL DEFAULT 7,
                repeat TEXT NOT NULL DEFAULT 'none',
                repeat_days INTEGER NOT NULL DEFAULT 0,
                amount REAL NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
        if "repeat_days" not in columns:
            conn.execute("ALTER TABLE items ADD COLUMN repeat_days INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS push_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                notice_date TEXT NOT NULL,
                due_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(item_id, notice_date, due_date)
            )
            """
        )
        cleanup_push_log(conn)


def cleanup_push_log(conn: sqlite3.Connection) -> None:
    """Remove old push records so the SQLite file does not grow forever."""
    try:
        retention_days = max(30, int(CONFIG.get("PUSH_LOG_RETENTION_DAYS", "90")))
    except ValueError:
        retention_days = 90
    cutoff = (date.today() - timedelta(days=retention_days)).isoformat()
    conn.execute("DELETE FROM push_log WHERE notice_date < ?", (cutoff,))


def h(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = 28
    while True:
        try:
            date(year, month, last_day + 1)
            last_day += 1
        except ValueError:
            break
    return date(year, month, min(day.day, last_day))


def next_due(day: date, repeat: str, repeat_days: int = 0) -> date | None:
    if repeat == "daily":
        return day + timedelta(days=1)
    if repeat == "weekly":
        return day + timedelta(days=7)
    if repeat == "monthly":
        return add_months(day, 1)
    if repeat == "quarterly":
        return add_months(day, 3)
    if repeat == "yearly":
        return add_months(day, 12)
    if repeat == "custom_days" and repeat_days > 0:
        return day + timedelta(days=repeat_days)
    return None


def days_text(due_date: str) -> str:
    days = (parse_date(due_date) - date.today()).days
    if days < 0:
        return f"逾期 {abs(days)} 天"
    if days == 0:
        return "今天到期"
    if days == 1:
        return "明天到期"
    return f"{days} 天后"


def repeat_label(item: sqlite3.Row | dict[str, object]) -> str:
    repeat = str(item["repeat"] or "none")
    if repeat == "custom_days":
        days = int(item["repeat_days"] or 0)
        return f"每 {days} 天" if days > 0 else "自定义天数"
    return REPEATS.get(repeat, "不循环")


def signed_session(username: str) -> str:
    expires = int(time.time()) + 86400 * 14
    payload = f"{username}:{expires}:{secrets.token_urlsafe(10)}"
    sig = hmac.new(CONFIG["SECRET_KEY"].encode(), payload.encode(), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{token}.{sig}"


def verify_session(token: str | None) -> bool:
    if not token:
        return False
    try:
        payload_part, sig = token.split(".", 1)
        padding = "=" * (-len(payload_part) % 4)
        payload = base64.urlsafe_b64decode((payload_part + padding).encode()).decode()
        expected = hmac.new(CONFIG["SECRET_KEY"].encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        username, expires, _nonce = payload.split(":", 2)
        return username == CONFIG["ADMIN_USERNAME"] and int(expires) > int(time.time())
    except Exception:
        return False


def telegram_configured() -> bool:
    return bool(CONFIG.get("TG_BOT_TOKEN") and CONFIG.get("TG_CHAT_ID"))


def auth_enabled() -> bool:
    return CONFIG.get("AUTH_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


def send_telegram(text: str) -> tuple[bool, str]:
    if not telegram_configured():
        return False, "Telegram Bot Token 或 Chat ID 未配置"

    ok, result = telegram_request(
        "sendMessage",
        {
            "chat_id": CONFIG["TG_CHAT_ID"],
            "text": text,
            "disable_web_page_preview": True,
        },
    )
    return (True, "推送成功") if ok else (False, str(result))


def telegram_request(method: str, payload: dict[str, object], timeout: int = 15) -> tuple[bool, str | dict[str, object]]:
    url = f"https://api.telegram.org/bot{CONFIG['TG_BOT_TOKEN']}/{method}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return True, data.get("result", {})
            return False, data.get("description", "Telegram 返回失败")
    except Exception as exc:
        return False, str(exc)


def reminder_message(item: sqlite3.Row) -> str:
    amount = float(item["amount"] or 0)
    amount_line = f"\n金额：¥{amount:g}" if amount else ""
    notes_line = f"\n备注：{item['notes']}" if item["notes"] else ""
    return (
        f"【到期提醒】{item['title']}\n"
        f"分类：{item['category']}\n"
        f"到期：{item['due_date']}（{days_text(item['due_date'])}）\n"
        f"提前提醒：{item['lead_days']} 天\n"
        f"循环：{repeat_label(item)}"
        f"{amount_line}"
        f"{notes_line}"
    )


def due_items() -> list[sqlite3.Row]:
    today = date.today()
    with db() as conn:
        rows = conn.execute("SELECT * FROM items ORDER BY due_date ASC, id ASC").fetchall()
    result = []
    for item in rows:
        remind_day = parse_date(item["due_date"]) - timedelta(days=int(item["lead_days"]))
        if today >= remind_day:
            result.append(item)
    return result


def run_due_check() -> tuple[int, list[str]]:
    today_text = date.today().isoformat()
    sent = 0
    errors: list[str] = []
    for item in due_items():
        with db() as conn:
            exists = conn.execute(
                "SELECT 1 FROM push_log WHERE item_id = ? AND notice_date = ? AND due_date = ?",
                (item["id"], today_text, item["due_date"]),
            ).fetchone()
        if exists:
            continue
        ok, message = send_telegram(reminder_message(item))
        if ok:
            sent += 1
            with db() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO push_log (item_id, notice_date, due_date, created_at) VALUES (?, ?, ?, ?)",
                    (item["id"], today_text, item["due_date"], datetime.now().isoformat(timespec="seconds")),
                )
        else:
            errors.append(f"{item['title']}: {message}")
    return sent, errors


def items_message(days_limit: int | None = None) -> str:
    rows = all_items()
    today = date.today()
    if days_limit is not None:
        rows = [
            item for item in rows
            if (parse_date(item["due_date"]) - today).days <= days_limit
        ]
    if not rows:
        return "当前没有提醒事项。"

    title = "提醒列表" if days_limit is None else f"{days_limit} 天内提醒"
    lines = [f"【{title}】"]
    for index, item in enumerate(rows[:20], 1):
        amount = float(item["amount"] or 0)
        amount_text = f" ¥{amount:g}" if amount else ""
        lines.append(
            f"{index}. {item['title']}\n"
            f"   {item['due_date']} · {days_text(item['due_date'])} · {item['category']} · {repeat_label(item)}{amount_text}"
        )
    if len(rows) > 20:
        lines.append(f"还有 {len(rows) - 20} 条未显示，请到面板查看。")
    return "\n".join(lines)


def help_message() -> str:
    return "\n".join(
        [
            "【通知面板 Bot 命令】",
            "/items - 查看全部提醒",
            "/items 30 - 查看 30 天内提醒",
            "/help - 查看命令",
        ]
    )


def parse_items_days(text: str) -> int | None:
    parts = text.split()
    if len(parts) < 2:
        return None
    try:
        days = int(parts[1])
    except ValueError:
        return None
    return max(1, min(days, 3650))


def send_command_reply(text: str) -> None:
    ok, message = send_telegram(text)
    if not ok:
        print(f"Telegram command reply failed: {message}")


def handle_bot_command(message: dict[str, object]) -> None:
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return
    chat_id = str(chat.get("id", ""))
    if chat_id != str(CONFIG.get("TG_CHAT_ID", "")):
        return
    text = str(message.get("text", "")).strip()
    command = text.split()[0].split("@", 1)[0].lower() if text else ""
    if command in {"/start", "/help"}:
        send_command_reply(help_message())
    elif command == "/items":
        send_command_reply(items_message(parse_items_days(text)))


def bot_command_loop() -> None:
    offset = 0
    while True:
        if not telegram_configured():
            time.sleep(60)
            continue
        ok, result = telegram_request(
            "getUpdates",
            {
                "offset": offset,
                "timeout": 25,
                "allowed_updates": ["message"],
            },
            timeout=35,
        )
        if not ok:
            print(f"Telegram getUpdates failed: {result}")
            time.sleep(30)
            continue
        if not isinstance(result, list):
            time.sleep(5)
            continue
        for update in result:
            if not isinstance(update, dict):
                continue
            update_id = int(update.get("update_id", offset))
            offset = max(offset, update_id + 1)
            message = update.get("message")
            if isinstance(message, dict):
                handle_bot_command(message)


def scheduler() -> None:
    last_run = ""
    if CONFIG.get("PUSH_ON_START", "true").lower() == "true":
        run_due_check()
    while True:
        now = datetime.now()
        stamp = f"{now.date().isoformat()} {now.strftime('%H:%M')}"
        if now.strftime("%H:%M") == CONFIG.get("CHECK_TIME", "09:00") and stamp != last_run:
            run_due_check()
            last_run = stamp
        time.sleep(30)


def all_items() -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM items ORDER BY due_date ASC, id ASC").fetchall()


def get_item(item_id: str) -> sqlite3.Row | None:
    if not item_id:
        return None
    with db() as conn:
        return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def notice_url(message: str) -> str:
    return f"/?notice={urllib.parse.quote(message)}"


def dashboard(edit_id: str = "", notice: str = "") -> str:
    items = all_items()
    editing = get_item(edit_id) if edit_id else None
    warning = ""
    if auth_enabled() and CONFIG.get("ADMIN_PASSWORD") == "请改成强密码":
        warning = "<div class='warn'>请先修改 config.env 里的 ADMIN_PASSWORD，别用默认密码。</div>"
    if not telegram_configured():
        warning += "<div class='warn'>Telegram 尚未配置，测试推送和自动提醒不会发送。</div>"
    rows = "\n".join(item_card(item) for item in items) or "<div class='empty'>还没有提醒事项。</div>"
    return page(
        "通知面板",
        f"""
        <header class="topbar">
          <div>
            <p>{date.today().isoformat()} · 每天 {h(CONFIG.get('CHECK_TIME', '09:00'))} 检查</p>
            <h1>到期通知面板</h1>
          </div>
          {logout_button()}
        </header>
        {warning}
        {"<div class='notice'>" + h(notice) + "</div>" if notice else ""}
        <section class="grid">
          <form class="panel form" method="post" action="/save">
            <h2>{'编辑提醒' if editing else '新增提醒'}</h2>
            <input type="hidden" name="id" value="{h(editing['id'] if editing else '')}">
            <label>名称<input required name="title" value="{h(editing['title'] if editing else '')}" placeholder="DMIT VPS 续费"></label>
            <!-- 分类、金额、备注仍保存在数据库，但默认不占用表单空间。 -->
            <input type="hidden" name="category" value="{h(editing['category'] if editing else 'VPS')}">
            <label>到期日期<input required type="date" name="due_date" value="{h(editing['due_date'] if editing else date.today().isoformat())}"></label>
            <label>提前几天提醒<input required type="number" min="0" name="lead_days" value="{h(editing['lead_days'] if editing else 7)}"></label>
            <label>重复周期<select name="repeat">{options_dict(REPEATS, editing['repeat'] if editing else 'yearly')}</select></label>
            <label>自定义重复天数<input type="number" min="1" max="3650" name="repeat_days" value="{h(editing['repeat_days'] if editing else '')}" placeholder="例如 180"></label>
            <input type="hidden" name="amount" value="{h(editing['amount'] if editing else '')}">
            <input type="hidden" name="notes" value="{h(editing['notes'] if editing else '')}">
            <button class="primary" type="submit">保存</button>
          </form>

          <div class="panel">
            <div class="panel-head">
              <h2>提醒列表</h2>
              <form method="post" action="/test"><button type="submit">测试推送</button></form>
            </div>
            <div class="list">{rows}</div>
          </div>
        </section>
        """,
    )


def item_card(item: sqlite3.Row) -> str:
    days = (parse_date(item["due_date"]) - date.today()).days
    cls = "bad" if days < 0 else "soon" if days <= int(item["lead_days"]) else "ok"
    amount = float(item["amount"] or 0)
    return f"""
    <article class="item {cls}">
      <div>
        <strong>{h(item['title'])}</strong>
        <p>{h(item['category'])} · {h(item['due_date'])} · {h(days_text(item['due_date']))} · {h(repeat_label(item))}</p>
        <p>{'¥' + h(f'{amount:g}') + ' · ' if amount else ''}{h(item['notes'])}</p>
      </div>
      <div class="actions">
        <a href="/?edit={h(item['id'])}">编辑</a>
        <form method="post" action="/done"><input type="hidden" name="id" value="{h(item['id'])}"><button type="submit">完成</button></form>
        <form method="post" action="/delete"><input type="hidden" name="id" value="{h(item['id'])}"><button class="danger" type="submit">删除</button></form>
      </div>
    </article>
    """


def login_page(error: str = "") -> str:
    return page(
        "登录",
        f"""
        <main class="login">
          <form class="panel login-card" method="post" action="/login">
            <h1>通知面板</h1>
            <p>登录后管理 Telegram 到期提醒。</p>
            {"<div class='warn'>" + h(error) + "</div>" if error else ""}
            <label>用户名<input name="username" value="admin" autocomplete="username"></label>
            <label>密码<input name="password" type="password" autocomplete="current-password"></label>
            <button class="primary" type="submit">登录</button>
          </form>
        </main>
        """,
    )


def logout_button() -> str:
    if not auth_enabled():
        return "<span class='ssh-only'>SSH-only</span>"
    return '<form method="post" action="/logout"><button>退出</button></form>'


def options(values: list[str], current: str) -> str:
    return "".join(f'<option value="{h(value)}" {"selected" if value == current else ""}>{h(value)}</option>' for value in values)


def options_dict(values: dict[str, str], current: str) -> str:
    return "".join(f'<option value="{h(key)}" {"selected" if key == current else ""}>{h(label)}</option>' for key, label in values.items())


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{h(title)}</title>
  <style>
    :root {{ --bg:#f4f6f8; --card:#fff; --text:#172033; --muted:#667085; --line:#d9e0e7; --blue:#1f6feb; --red:#d92d20; --green:#16834f; --yellow:#b7791f; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--text); background:var(--bg); font-family:"Microsoft YaHei","Segoe UI",sans-serif; }}
    button,input,select,textarea {{ font:inherit; }}
    button,a {{ border:0; border-radius:8px; padding:9px 12px; color:var(--text); background:#eef2f6; text-decoration:none; cursor:pointer; }}
    .primary {{ color:#fff; background:var(--blue); }}
    .danger {{ color:#fff; background:var(--red); }}
    .topbar {{ display:flex; justify-content:space-between; align-items:center; gap:16px; padding:28px 32px 12px; }}
    .topbar h1,.topbar p,h2 {{ margin:0; }}
    .topbar p {{ color:var(--muted); }}
    .ssh-only {{ border-radius:8px; padding:9px 12px; color:#13643e; background:#e8f6ef; }}
    .grid {{ display:grid; grid-template-columns:360px minmax(0,1fr); gap:18px; padding:18px 32px 32px; }}
    .panel {{ border:1px solid var(--line); border-radius:8px; padding:18px; background:var(--card); box-shadow:0 12px 34px rgba(15,23,42,.08); }}
    .panel-head {{ display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:14px; }}
    .form {{ display:grid; gap:12px; align-content:start; }}
    label {{ display:grid; gap:6px; color:var(--muted); font-size:13px; }}
    input,select,textarea {{ width:100%; border:1px solid var(--line); border-radius:8px; padding:10px; color:var(--text); background:#fff; }}
    .list {{ display:grid; gap:10px; }}
    .item {{ display:grid; grid-template-columns:minmax(0,1fr) auto; gap:14px; border:1px solid var(--line); border-left:5px solid var(--green); border-radius:8px; padding:14px; background:#fff; }}
    .item.soon {{ border-left-color:var(--yellow); }}
    .item.bad {{ border-left-color:var(--red); }}
    .item strong {{ font-size:17px; }}
    .item p {{ margin:6px 0 0; color:var(--muted); line-height:1.45; }}
    .actions {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; justify-content:flex-end; }}
    .actions form {{ margin:0; }}
    .warn,.notice,.empty {{ margin:12px 32px 0; border-radius:8px; padding:12px 14px; background:#fff7df; color:#7a4c00; }}
    .notice {{ background:#e8f6ef; color:#13643e; }}
    .empty {{ margin:0; color:var(--muted); background:#f8fafc; }}
    .login {{ min-height:100vh; display:grid; place-items:center; padding:20px; }}
    .login-card {{ width:min(420px,100%); display:grid; gap:14px; }}
    .login-card h1,.login-card p {{ margin:0; }}
    @media (max-width:850px) {{ .grid {{ grid-template-columns:1fr; padding:16px; }} .topbar {{ padding:20px 16px 8px; }} .item {{ grid-template-columns:1fr; }} .actions {{ justify-content:flex-start; }} .warn,.notice {{ margin-left:16px; margin-right:16px; }} }}
  </style>
</head>
<body>{body}</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/":
            self.redirect("/")
            return
        if auth_enabled() and not self.is_logged_in():
            self.html(login_page())
            return
        query = urllib.parse.parse_qs(parsed.query)
        self.html(dashboard(query.get("edit", [""])[0], query.get("notice", [""])[0]))

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        data = self.form_data()
        if parsed.path == "/login":
            self.handle_login(data)
            return
        if auth_enabled() and not self.is_logged_in():
            self.redirect("/")
            return
        if parsed.path == "/logout":
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            self.end_headers()
        elif parsed.path == "/save":
            self.handle_save(data)
        elif parsed.path == "/delete":
            self.handle_delete(data)
        elif parsed.path == "/done":
            self.handle_done(data)
        elif parsed.path == "/test":
            ok, message = send_telegram("通知面板测试：Telegram 推送正常。")
            self.redirect(f"/?notice={urllib.parse.quote(message if ok else '测试失败：' + message)}")
        else:
            self.redirect("/")

    def is_logged_in(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie"))
        token = cookie.get(COOKIE_NAME)
        return verify_session(token.value if token else None)

    def form_data(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    def handle_login(self, data: dict[str, str]) -> None:
        username_ok = hmac.compare_digest(data.get("username", ""), CONFIG["ADMIN_USERNAME"])
        password_ok = hmac.compare_digest(data.get("password", ""), CONFIG["ADMIN_PASSWORD"])
        if username_ok and password_ok:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", f"{COOKIE_NAME}={signed_session(CONFIG['ADMIN_USERNAME'])}; Path=/; Max-Age=1209600; HttpOnly; SameSite=Lax")
            self.end_headers()
            return
        self.html(login_page("用户名或密码错误"), status=HTTPStatus.UNAUTHORIZED)

    def handle_save(self, data: dict[str, str]) -> None:
        title = data.get("title", "").strip()
        due_date = data.get("due_date", "").strip()
        if not title or not due_date:
            self.redirect(notice_url("名称和到期日期不能为空"))
            return
        values = (
            title,
            data.get("category", "其他"),
            due_date,
            int(data.get("lead_days") or 0),
            data.get("repeat", "none"),
            int(data.get("repeat_days") or 0),
            float(data.get("amount") or 0),
            data.get("notes", "").strip(),
        )
        with db() as conn:
            if data.get("id"):
                conn.execute(
                    "UPDATE items SET title=?, category=?, due_date=?, lead_days=?, repeat=?, repeat_days=?, amount=?, notes=? WHERE id=?",
                    values + (data["id"],),
                )
            else:
                conn.execute(
                    "INSERT INTO items (title, category, due_date, lead_days, repeat, repeat_days, amount, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values + (datetime.now().isoformat(timespec="seconds"),),
                )
        self.redirect(notice_url("已保存"))

    def handle_delete(self, data: dict[str, str]) -> None:
        with db() as conn:
            conn.execute("DELETE FROM push_log WHERE item_id = ?", (data.get("id"),))
            conn.execute("DELETE FROM items WHERE id = ?", (data.get("id"),))
        self.redirect(notice_url("已删除"))

    def handle_done(self, data: dict[str, str]) -> None:
        item = get_item(data.get("id", ""))
        if not item:
            self.redirect("/")
            return
        next_day = next_due(parse_date(item["due_date"]), item["repeat"], int(item["repeat_days"] or 0))
        with db() as conn:
            if next_day:
                conn.execute("UPDATE items SET due_date = ? WHERE id = ?", (next_day.isoformat(), item["id"]))
            else:
                conn.execute("DELETE FROM push_log WHERE item_id = ?", (item["id"],))
                conn.execute("DELETE FROM items WHERE id = ?", (item["id"],))
        self.redirect(notice_url("已完成"))

    def html(self, content: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] {self.address_string()} {fmt % args}")


def main() -> None:
    init_db()
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=bot_command_loop, daemon=True).start()
    host = CONFIG.get("HOST", "127.0.0.1")
    port = int(CONFIG.get("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"通知面板已启动：http://{host}:{port}")
    print(f"配置文件：{CONFIG_PATH}")
    print(f"数据库：{DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
