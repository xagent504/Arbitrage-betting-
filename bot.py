import os
import re
import html
import json
import sqlite3
import asyncio
import random
import logging
from urllib.parse import urlparse, urljoin

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("fb-comment-tg-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or 0)
DB_PATH = os.getenv("DB_PATH", "bot_data.db").strip()

RUNTIME = {
    "running": False,
    "sent_count": 0,
    "fail_count": 0,
    "last_status": "Idle",
    "worker_task": None,
}

USER_SESSION: dict[int, dict] = {}


def is_authorized(user_id: int) -> bool:
    if ADMIN_ID == 0:
        return True
    return user_id == ADMIN_ID


# -------------------- SQLite Database --------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cookie TEXT NOT NULL,
            post_id TEXT NOT NULL,
            comments_json TEXT NOT NULL,
            min_delay INTEGER NOT NULL,
            max_delay INTEGER NOT NULL
        )
    """)
    cur.execute("SELECT COUNT(*) FROM settings WHERE id = 1")
    if cur.fetchone()[0] == 0:
        default_cookie = os.getenv("FB_COOKIE", "").strip()
        default_post = os.getenv("POST_ID", "").strip()
        default_comments = [
            c.strip()
            for c in os.getenv("COMMENTS", "Nice post!|Great update!|Awesome!").split("|")
            if c.strip()
        ]
        default_min = int(os.getenv("MIN_DELAY", "25"))
        default_max = int(os.getenv("MAX_DELAY", "60"))

        cur.execute(
            """
            INSERT INTO settings (id, cookie, post_id, comments_json, min_delay, max_delay)
            VALUES (1, ?, ?, ?, ?, ?)
            """,
            (default_cookie, default_post, json.dumps(default_comments), default_min, default_max),
        )
    conn.commit()
    conn.close()


def get_settings() -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT cookie, post_id, comments_json, min_delay, max_delay FROM settings WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    return {
        "cookie": row[0],
        "post_id": row[1],
        "comments": json.loads(row[2]),
        "min_delay": row[3],
        "max_delay": row[4],
    }


def update_setting(field: str, value):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if field == "comments":
        cur.execute("UPDATE settings SET comments_json = ? WHERE id = 1", (json.dumps(value),))
    elif field == "delay":
        min_d, max_d = value
        cur.execute("UPDATE settings SET min_delay = ?, max_delay = ? WHERE id = 1", (min_d, max_d))
    else:
        cur.execute(f"UPDATE settings SET {field} = ? WHERE id = 1", (value,))
    conn.commit()
    conn.close()


# -------------------- Clean Chat Manager --------------------
def get_user_ctx(user_id: int) -> dict:
    if user_id not in USER_SESSION:
        USER_SESSION[user_id] = {
            "mode": None,
            "all_msg_ids": [],
            "dashboard_msg_id": None,
        }
    return USER_SESSION[user_id]


def track_msg(user_id: int, message_id: int):
    ctx = get_user_ctx(user_id)
    if message_id not in ctx["all_msg_ids"]:
        ctx["all_msg_ids"].append(message_id)


async def wipe_previous_messages(bot, chat_id: int, user_id: int, keep_msg_id: int | None = None):
    """পেছনের সব ট্র্যাক করা মেসেজ ডিলিট করে (শুধু keep_msg_id বাদে)।"""
    ctx = get_user_ctx(user_id)
    ctx["mode"] = None
    remaining = []
    for mid in list(ctx["all_msg_ids"]):
        if keep_msg_id and mid == keep_msg_id:
            remaining.append(mid)
            continue
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass
    ctx["all_msg_ids"] = remaining


# -------------------- Facebook Client --------------------
def parse_cookie_string(raw_cookie: str) -> dict:
    cookies = {}
    for part in raw_cookie.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def normalize_post_target(post_input: str) -> str:
    """ইউজার পূর্ণ URL দিলে বা শুধু Post ID দিলে সেটিকে mbasic URL-এ রূপান্তর করে।"""
    post_input = post_input.strip()
    if post_input.startswith("http://") or post_input.startswith("https://"):
        parsed = urlparse(post_input)
        path_and_query = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        return f"https://mbasic.facebook.com{path_and_query}"
    if post_input.isdigit():
        return f"https://mbasic.facebook.com/{post_input}"
    return f"https://mbasic.facebook.com/{post_input.lstrip('/')}"


class FBCommentClient:
    def __init__(self, cookie_str: str):
        self.session = requests.Session()
        self.session.cookies.update(parse_cookie_string(cookie_str))
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; SM-G991B) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
            "Connection": "keep-alive",
        })

    def check_session(self) -> tuple[bool, str]:
        try:
            resp = self.session.get("https://mbasic.facebook.com/", timeout=20)
            page = resp.text
            if "login_form" in page or "checkpoint" in resp.url or "c_user" not in self.session.cookies:
                return False, "Cookie invalid, expired, or checkpoint."
            uid = self.session.cookies.get("c_user", "Unknown")
            return True, f"Valid (UID: {uid})"
        except Exception as exc:
            return False, f"Error: {exc}"

    def _extract_comment_form(self, page_html: str, base_url: str) -> tuple[str | None, dict]:
        """পোস্টের HTML থেকে আসল কমেন্ট ফর্মের action URL ও সব hidden input বের করে।"""
        forms = re.findall(r"<form\b[^>]*>.*?</form>", page_html, flags=re.DOTALL | re.IGNORECASE)
        for form_html in forms:
            if "comment_text" not in form_html:
                continue

            action_match = re.search(r'action="([^"]+)"', form_html, flags=re.IGNORECASE)
            if not action_match:
                continue

            action_url = urljoin(base_url, html.unescape(action_match.group(1)))
            inputs = {}
            for inp in re.findall(r"<input\b[^>]*>", form_html, flags=re.IGNORECASE):
                name_m = re.search(r'name="([^"]+)"', inp, flags=re.IGNORECASE)
                val_m = re.search(r'value="([^"]*)"', inp, flags=re.IGNORECASE)
                if name_m:
                    inputs[html.unescape(name_m.group(1))] = html.unescape(val_m.group(1)) if val_m else ""

            return action_url, inputs
        return None, {}

    def send_comment(self, post_target: str, text: str) -> tuple[bool, str]:
        post_url = normalize_post_target(post_target)
        try:
            # ১) আগে পোস্ট পেজ লোড করে আসল কমেন্ট ফর্ম ও ডায়নামিক টোকেন নেওয়া
            get_resp = self.session.get(post_url, timeout=20)
            page_html = get_resp.text

            if "login_form" in page_html or "checkpoint" in get_resp.url:
                return False, "Login required or checkpoint."

            action_url, payload = self._extract_comment_form(page_html, get_resp.url)
            if not action_url:
                return False, "Comment form not found (Post private/restricted or URL invalid)."

            payload["comment_text"] = text
            headers = {
                "Referer": get_resp.url,
                "Origin": "https://mbasic.facebook.com",
                "Content-Type": "application/x-www-form-urlencoded",
            }

            # ২) আসল ফর্ম অ্যাকশনে কমেন্ট সাবমিট করা
            post_resp = self.session.post(action_url, data=payload, headers=headers, timeout=20)
            resp_text = post_resp.text

            if "checkpoint" in post_resp.url:
                return False, "Account hit checkpoint."
            if "you can't use this feature right now" in resp_text.lower() or "blocked" in resp_text.lower():
                return False, "Account temporarily blocked from commenting."

            # ৩) রেসপন্স পেজে কমেন্টের অংশ এসেছে কি না যাচাই করা
            snippet = text.strip()[:15]
            if snippet and snippet in html.unescape(resp_text):
                return True, f"Verified Sent: {snippet}"

            if post_resp.status_code in (200, 302) and "error" not in post_resp.url.lower():
                return True, f"Submitted (HTTP {post_resp.status_code})"

            return False, f"Post rejected (HTTP {post_resp.status_code})"
        except Exception as exc:
            return False, f"Request error: {exc}"


# -------------------- Keyboards & Dashboard --------------------
def home_keyboard() -> InlineKeyboardMarkup:
    status_btn = (
        InlineKeyboardButton("⏹ Stop Worker", callback_data="stop_worker")
        if RUNTIME["running"]
        else InlineKeyboardButton("▶️ Start Worker", callback_data="start_worker")
    )
    return InlineKeyboardMarkup([
        [status_btn, InlineKeyboardButton("🔍 Check Cookie", callback_data="check_cookie")],
        [
            InlineKeyboardButton("🍪 Set Cookie", callback_data="set_cookie"),
            InlineKeyboardButton("🔗 Set Post URL/ID", callback_data="set_post"),
        ],
        [
            InlineKeyboardButton("💬 Set Comments", callback_data="set_comments"),
            InlineKeyboardButton("⏱ Set Delay", callback_data="set_delay"),
        ],
        [
            InlineKeyboardButton("🗑 Clear All Data", callback_data="clear_data_confirm"),
            InlineKeyboardButton("🔄 Refresh", callback_data="home"),
        ],
    ])


def nav_keyboard(back_to: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Back", callback_data=back_to),
            InlineKeyboardButton("🏠 Home", callback_data="home"),
        ]
    ])


def build_dashboard_text() -> str:
    cfg = get_settings()
    cookie_status = "✅ Saved" if cfg["cookie"] else "❌ Not Set"
    post_status = f"`{cfg['post_id'][:32]}`" if cfg["post_id"] else "❌ Not Set"
    run_status = "🟢 Running" if RUNTIME["running"] else "🔴 Stopped"
    comments_preview = ", ".join(cfg["comments"][:3]) if cfg["comments"] else "None"

    return (
        "🤖 **FB Auto Comment Bot (Verified Form Mode)**\n\n"
        f"• **Status:** {run_status}\n"
        f"• **Cookie:** {cookie_status}\n"
        f"• **Target Post:** {post_status}\n"
        f"• **Comments ({len(cfg['comments'])}):** `{comments_preview}`\n"
        f"• **Delay:** `{cfg['min_delay']}s - {cfg['max_delay']}s`\n"
        f"• **Sent / Failed:** `{RUNTIME['sent_count']} / {RUNTIME['fail_count']}`\n"
        f"• **Last Log:** `{RUNTIME['last_status']}`"
    )


async def show_clean_home(bot, chat_id: int, user_id: int, message_to_edit=None):
    ctx = get_user_ctx(user_id)
    text = build_dashboard_text()
    kb = home_keyboard()

    if message_to_edit:
        await wipe_previous_messages(bot, chat_id, user_id, keep_msg_id=message_to_edit.message_id)
        try:
            await message_to_edit.edit_text(text, parse_mode="Markdown", reply_markup=kb)
            ctx["dashboard_msg_id"] = message_to_edit.message_id
            track_msg(user_id, message_to_edit.message_id)
            return
        except Exception:
            pass

    await wipe_previous_messages(bot, chat_id, user_id, keep_msg_id=ctx["dashboard_msg_id"])
    if ctx["dashboard_msg_id"]:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=ctx["dashboard_msg_id"],
                text=text,
                parse_mode="Markdown",
                reply_markup=kb,
            )
            return
        except Exception:
            pass

    sent = await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb)
    ctx["dashboard_msg_id"] = sent.message_id
    track_msg(user_id, sent.message_id)


# -------------------- Worker Loop --------------------
async def comment_worker_loop(app: Application, chat_id: int, user_id: int):
    cfg = get_settings()
    client = FBCommentClient(cfg["cookie"])
    ok, msg = await asyncio.to_thread(client.check_session)

    if not ok:
        RUNTIME["running"] = False
        RUNTIME["last_status"] = msg
        alert = await app.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Worker বন্ধ হয়েছে: `{msg}`",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )
        track_msg(user_id, alert.message_id)
        return

    idx = 0
    consecutive_failures = 0

    while RUNTIME["running"]:
        cfg = get_settings()
        comments = cfg["comments"]
        post_target = cfg["post_id"]

        if not comments or not post_target:
            RUNTIME["running"] = False
            RUNTIME["last_status"] = "Missing comments or post target."
            break

        text = comments[idx % len(comments)]
        idx += 1

        success, status_msg = await asyncio.to_thread(client.send_comment, post_target, text)

        if success:
            RUNTIME["sent_count"] += 1
            RUNTIME["last_status"] = status_msg
            consecutive_failures = 0
        else:
            RUNTIME["fail_count"] += 1
            RUNTIME["last_status"] = status_msg
            consecutive_failures += 1

            if consecutive_failures >= 3:
                RUNTIME["running"] = False
                alert = await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ টানা ৩ বার ব্যর্থ হওয়ায় Worker নিরাপদে বন্ধ করা হয়েছে।\nকারণ: `{status_msg}`",
                    parse_mode="Markdown",
                    reply_markup=nav_keyboard("home"),
                )
                track_msg(user_id, alert.message_id)
                break

        delay = random.randint(cfg["min_delay"], cfg["max_delay"])
        for _ in range(delay):
            if not RUNTIME["running"]:
                break
            await asyncio.sleep(1)


# -------------------- Handlers --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if not is_authorized(user_id):
        await update.message.reply_text("⛔ আপনি এই বট ব্যবহারের জন্য অনুমোদিত নন।")
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    await wipe_previous_messages(context.bot, chat_id, user_id, keep_msg_id=None)
    ctx = get_user_ctx(user_id)
    ctx["dashboard_msg_id"] = None
    await show_clean_home(context.bot, chat_id, user_id)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    await query.answer()

    if not is_authorized(user_id):
        await query.edit_message_text("⛔ অনুমোদিত নয়।")
        return

    data = query.data
    ctx = get_user_ctx(user_id)
    ctx["dashboard_msg_id"] = query.message.message_id
    track_msg(user_id, query.message.message_id)

    if data == "home":
        await show_clean_home(context.bot, chat_id, user_id, message_to_edit=query.message)

    elif data == "set_cookie":
        await wipe_previous_messages(context.bot, chat_id, user_id, keep_msg_id=query.message.message_id)
        ctx["mode"] = "cookie"
        await query.edit_message_text(
            "🍪 আপনার Facebook **Cookie** স্ট্রিংটি পাঠান:\n\n"
            "উদাহরণ: `c_user=1000...; xs=...; datr=...;`",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif data == "set_post":
        await wipe_previous_messages(context.bot, chat_id, user_id, keep_msg_id=query.message.message_id)
        ctx["mode"] = "post_id"
        await query.edit_message_text(
            "🔗 টার্গেট পোস্টের **সম্পূর্ণ Post Link (URL)** পাঠান:\n\n"
            "টিপস: সরাসরি পোস্টের পূর্ণ লিংক (যেমন `https://www.facebook.com/.../posts/...`) দিলে ফর্ম খুঁজে পেতে সবচেয়ে ভালো কাজ করে।",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif data == "set_comments":
        await wipe_previous_messages(context.bot, chat_id, user_id, keep_msg_id=query.message.message_id)
        ctx["mode"] = "comments"
        await query.edit_message_text(
            "💬 কমেন্ট লিস্ট পাঠান (প্রতি লাইনে একটি বা `|` দিয়ে আলাদা করে):",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif data == "set_delay":
        await wipe_previous_messages(context.bot, chat_id, user_id, keep_msg_id=query.message.message_id)
        ctx["mode"] = "delay"
        await query.edit_message_text(
            "⏱ মিনিমাম ও ম্যাক্সিমাম ডিলে (সেকেন্ডে) পাঠান:\n\nউদাহরণ: `25-60`",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif data == "check_cookie":
        await wipe_previous_messages(context.bot, chat_id, user_id, keep_msg_id=query.message.message_id)
        cfg = get_settings()
        if not cfg["cookie"]:
            await query.edit_message_text(
                "❌ আগে **Set Cookie** থেকে কুকি সেট করুন।",
                parse_mode="Markdown",
                reply_markup=nav_keyboard("home"),
            )
            return

        await query.edit_message_text("⏳ কুকি যাচাই করা হচ্ছে...")
        client = FBCommentClient(cfg["cookie"])
        ok, msg = await asyncio.to_thread(client.check_session)
        RUNTIME["last_status"] = msg
        icon = "✅" if ok else "❌"
        await query.edit_message_text(
            f"{icon} **Cookie Check Result:**\n`{msg}`",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif data == "clear_data_confirm":
        await wipe_previous_messages(context.bot, chat_id, user_id, keep_msg_id=query.message.message_id)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ হ্যাঁ, সব মুছে ফেলুন", callback_data="clear_data_yes")],
            [
                InlineKeyboardButton("⬅️ Back", callback_data="home"),
                InlineKeyboardButton("🏠 Home", callback_data="home"),
            ],
        ])
        await query.edit_message_text(
            "⚠️ আপনি কি ডাটাবেস থেকে সেভ করা **Cookie, Post URL এবং Comments** মুছে ফেলতে চান?",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    elif data == "clear_data_yes":
        RUNTIME["running"] = False
        RUNTIME["sent_count"] = 0
        RUNTIME["fail_count"] = 0
        RUNTIME["last_status"] = "Data cleared"
        update_setting("cookie", "")
        update_setting("post_id", "")
        update_setting("comments", [])
        await show_clean_home(context.bot, chat_id, user_id, message_to_edit=query.message)

    elif data == "start_worker":
        cfg = get_settings()
        if RUNTIME["running"]:
            await show_clean_home(context.bot, chat_id, user_id, message_to_edit=query.message)
            return

        if not cfg["cookie"] or not cfg["post_id"] or not cfg["comments"]:
            await query.edit_message_text(
                "❌ Start করার আগে **Cookie**, **Post URL/ID**, এবং **Comments** সেট করুন।",
                parse_mode="Markdown",
                reply_markup=nav_keyboard("home"),
            )
            return

        RUNTIME["running"] = True
        RUNTIME["last_status"] = "Starting..."
        RUNTIME["worker_task"] = asyncio.create_task(
            comment_worker_loop(context.application, chat_id, user_id)
        )
        await show_clean_home(context.bot, chat_id, user_id, message_to_edit=query.message)

    elif data == "stop_worker":
        RUNTIME["running"] = False
        RUNTIME["last_status"] = "Stopped by user"
        await show_clean_home(context.bot, chat_id, user_id, message_to_edit=query.message)


async def text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if not is_authorized(user_id):
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    ctx = get_user_ctx(user_id)
    mode = ctx.get("mode")
    if not mode:
        return

    text = update.message.text.strip()

    if mode == "cookie":
        update_setting("cookie", text)
        await show_clean_home(context.bot, chat_id, user_id)

    elif mode == "post_id":
        update_setting("post_id", text)
        await show_clean_home(context.bot, chat_id, user_id)

    elif mode == "comments":
        raw_items = re.split(r"[\n|]+", text)
        comments = [c.strip() for c in raw_items if c.strip()]
        if not comments:
            err = await context.bot.send_message(
                chat_id=chat_id,
                text="❌ অন্তত একটি বৈধ কমেন্ট দিন।",
                reply_markup=nav_keyboard("home"),
            )
            track_msg(user_id, err.message_id)
            return
        update_setting("comments", comments)
        await show_clean_home(context.bot, chat_id, user_id)

    elif mode == "delay":
        m = re.match(r"^(\d+)\s*[-, ]\s*(\d+)$", text)
        if not m:
            err = await context.bot.send_message(
                chat_id=chat_id,
                text="❌ সঠিক ফরম্যাটে দিন, যেমন: `25-60`",
                parse_mode="Markdown",
                reply_markup=nav_keyboard("home"),
            )
            track_msg(user_id, err.message_id)
            return
        min_d = max(5, int(m.group(1)))
        max_d = max(min_d + 5, int(m.group(2)))
        update_setting("delay", (min_d, max_d))
        await show_clean_home(context.bot, chat_id, user_id)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing!")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))

    logger.info("Starting Telegram FB Comment Bot (Verified Form Mode)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
