import os
import re
import json
import sqlite3
import asyncio
import random
import logging
from urllib.parse import urlparse

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

# Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or 0)
DB_PATH = os.getenv("DB_PATH", "bot_data.db").strip()

# রানটাইম স্টেট (Worker status ও মেসেজ ট্র্যাকিং)
RUNTIME = {
    "running": False,
    "sent_count": 0,
    "fail_count": 0,
    "last_status": "Idle",
    "worker_task": None,
}

# ইউজারের বর্তমান ইনপুট মোড ও ডিলিট করার জন্য মেসেজ ID ট্র্যাকার
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


# -------------------- Chat Cleanup Helper --------------------
def get_user_ctx(user_id: int) -> dict:
    if user_id not in USER_SESSION:
        USER_SESSION[user_id] = {
            "mode": None,
            "temp_msg_ids": [],
            "dashboard_msg_id": None,
        }
    return USER_SESSION[user_id]


def track_temp_msg(user_id: int, message_id: int):
    ctx = get_user_ctx(user_id)
    if message_id not in ctx["temp_msg_ids"]:
        ctx["temp_msg_ids"].append(message_id)


async def clear_temp_messages(bot, chat_id: int, user_id: int):
    """পেছনের সব অপ্রয়োজনীয় প্রম্পট ও ইউজার মেসেজ ডিলিট করে এবং ইনপুট মোড রিসেট করে।"""
    ctx = get_user_ctx(user_id)
    ctx["mode"] = None
    msg_ids = list(ctx["temp_msg_ids"])
    ctx["temp_msg_ids"].clear()

    for mid in msg_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass


# -------------------- Facebook Client --------------------
def parse_cookie_string(raw_cookie: str) -> dict:
    cookies = {}
    for part in raw_cookie.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def extract_post_id(post_input: str) -> str:
    post_input = post_input.strip()
    if post_input.isdigit():
        return post_input

    patterns = [
        r"/posts/(\d+)",
        r"story_fbid=(\d+)",
        r"fbid=(\d+)",
        r"/videos/(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, post_input)
        if m:
            return m.group(1)

    parsed = urlparse(post_input)
    if parsed.path:
        digits = re.findall(r"\d+", parsed.path)
        if digits:
            return digits[-1]
    return post_input


class FBCommentClient:
    def __init__(self, cookie_str: str):
        self.session = requests.Session()
        self.session.cookies.update(parse_cookie_string(cookie_str))
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
            "Connection": "keep-alive",
        })

    def check_session(self) -> tuple[bool, str | None, str | None, str]:
        try:
            resp = self.session.get("https://mbasic.facebook.com/", timeout=20)
            html = resp.text

            if "login_form" in html or "checkpoint" in resp.url or "c_user" not in self.session.cookies:
                return False, None, None, "Cookie invalid, expired, or checkpoint."

            dtsg_match = re.search(r'name="fb_dtsg"\s+value="([^"]+)"', html)
            if not dtsg_match:
                dtsg_match = re.search(r'"DTSGInitialData",\[\],\{"token":"([^"]+)"', html)

            jazoest_match = re.search(r'name="jazoest"\s+value="([^"]+)"', html)

            fb_dtsg = dtsg_match.group(1) if dtsg_match else None
            jazoest = jazoest_match.group(1) if jazoest_match else None
            uid = self.session.cookies.get("c_user", "Unknown")

            return True, fb_dtsg, jazoest, f"Valid (UID: {uid})"
        except Exception as exc:
            return False, None, None, f"Error: {exc}"

    def send_comment(self, post_id: str, text: str, fb_dtsg: str | None, jazoest: str | None) -> tuple[bool, str]:
        target_url = "https://mbasic.facebook.com/a/comment.php?fs=8&actionsource=1&refid=18"
        payload = {
            "fb_dtsg": fb_dtsg or "",
            "jazoest": jazoest or "",
            "comment_text": text,
            "post_id": post_id,
        }
        headers = {
            "Referer": f"https://mbasic.facebook.com/{post_id}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            resp = self.session.post(target_url, data=payload, headers=headers, timeout=20)
            if resp.status_code in (200, 302):
                if "checkpoint" in resp.url or "blocked" in resp.text.lower():
                    return False, "Blocked or checkpoint triggered."
                return True, f"Sent (HTTP {resp.status_code})"
            return False, f"Failed (HTTP {resp.status_code})"
        except Exception as exc:
            return False, f"Request error: {exc}"


# -------------------- UI Keyboards & Text --------------------
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
            InlineKeyboardButton("🔗 Set Post ID", callback_data="set_post"),
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
    post_status = f"`{cfg['post_id']}`" if cfg["post_id"] else "❌ Not Set"
    run_status = "🟢 Running" if RUNTIME["running"] else "🔴 Stopped"
    comments_preview = ", ".join(cfg["comments"][:3]) if cfg["comments"] else "None"

    return (
        "🤖 **FB Auto Comment Bot (SQLite + Clean UI)**\n\n"
        f"• **Status:** {run_status}\n"
        f"• **Cookie:** {cookie_status}\n"
        f"• **Post ID:** {post_status}\n"
        f"• **Comments ({len(cfg['comments'])}):** `{comments_preview}`\n"
        f"• **Delay:** `{cfg['min_delay']}s - {cfg['max_delay']}s`\n"
        f"• **Sent / Failed:** `{RUNTIME['sent_count']} / {RUNTIME['fail_count']}`\n"
        f"• **Last Log:** `{RUNTIME['last_status']}`"
    )


async def show_clean_home(bot, chat_id: int, user_id: int, message_to_edit=None):
    """পেছনের সব মেসেজ ও স্টেট মুছে দিয়ে পরিষ্কার হোম ড্যাশবোর্ড দেখায়।"""
    await clear_temp_messages(bot, chat_id, user_id)
    ctx = get_user_ctx(user_id)
    text = build_dashboard_text()
    kb = home_keyboard()

    if message_to_edit:
        try:
            await message_to_edit.edit_text(text, parse_mode="Markdown", reply_markup=kb)
            ctx["dashboard_msg_id"] = message_to_edit.message_id
            return
        except Exception:
            pass

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


# -------------------- Background Worker --------------------
async def comment_worker_loop(app: Application, chat_id: int, user_id: int):
    cfg = get_settings()
    client = FBCommentClient(cfg["cookie"])
    ok, fb_dtsg, jazoest, msg = await asyncio.to_thread(client.check_session)

    if not ok:
        RUNTIME["running"] = False
        RUNTIME["last_status"] = msg
        alert = await app.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Worker বন্ধ হয়েছে: `{msg}`",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )
        track_temp_msg(user_id, alert.message_id)
        return

    idx = 0
    consecutive_failures = 0

    while RUNTIME["running"]:
        cfg = get_settings()
        comments = cfg["comments"]
        post_id = cfg["post_id"]

        if not comments or not post_id:
            RUNTIME["running"] = False
            RUNTIME["last_status"] = "Missing comments or post_id."
            break

        text = comments[idx % len(comments)]
        idx += 1

        success, status_msg = await asyncio.to_thread(
            client.send_comment, post_id, text, fb_dtsg, jazoest
        )

        if success:
            RUNTIME["sent_count"] += 1
            RUNTIME["last_status"] = f"OK: {text[:25]}"
            consecutive_failures = 0
        else:
            RUNTIME["fail_count"] += 1
            RUNTIME["last_status"] = status_msg
            consecutive_failures += 1

            if consecutive_failures >= 3:
                RUNTIME["running"] = False
                alert = await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ টানা ৩ বার ব্যর্থ হওয়ায় Worker নিরাপদে বন্ধ হয়েছে।\nকারণ: `{status_msg}`",
                    parse_mode="Markdown",
                    reply_markup=nav_keyboard("home"),
                )
                track_temp_msg(user_id, alert.message_id)
                break

            ok, new_dtsg, new_jazoest, chk_msg = await asyncio.to_thread(client.check_session)
            if not ok:
                RUNTIME["running"] = False
                RUNTIME["last_status"] = chk_msg
                break
            fb_dtsg = new_dtsg or fb_dtsg
            jazoest = new_jazoest or jazoest

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

    # /start কমান্ড মেসেজ ও আগের ড্যাশবোর্ড মুছে নতুন পরিষ্কার হোম দেখায়
    try:
        await update.message.delete()
    except Exception:
        pass

    ctx = get_user_ctx(user_id)
    if ctx["dashboard_msg_id"]:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=ctx["dashboard_msg_id"])
        except Exception:
            pass
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

    if data == "home":
        await show_clean_home(context.bot, chat_id, user_id, message_to_edit=query.message)

    elif data == "set_cookie":
        await clear_temp_messages(context.bot, chat_id, user_id)
        ctx["mode"] = "cookie"
        await query.edit_message_text(
            "🍪 আপনার Facebook **Cookie** স্ট্রিংটি পাঠান:\n\n"
            "উদাহরণ: `c_user=1000...; xs=...; datr=...;`",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif data == "set_post":
        await clear_temp_messages(context.bot, chat_id, user_id)
        ctx["mode"] = "post_id"
        await query.edit_message_text(
            "🔗 টার্গেট পোস্টের **Post URL** বা **Post ID** পাঠান:",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif data == "set_comments":
        await clear_temp_messages(context.bot, chat_id, user_id)
        ctx["mode"] = "comments"
        await query.edit_message_text(
            "💬 কমেন্ট লিস্ট পাঠান (প্রতি লাইনে একটি বা `|` দিয়ে আলাদা করে):\n\n"
            "উদাহরণ:\n`Nice post!`\n`Great update!`\n`Awesome!`",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif data == "set_delay":
        await clear_temp_messages(context.bot, chat_id, user_id)
        ctx["mode"] = "delay"
        await query.edit_message_text(
            "⏱ মিনিমাম ও ম্যাক্সিমাম ডিলে (সেকেন্ডে) পাঠান:\n\n"
            "উদাহরণ: `25-60`",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif data == "check_cookie":
        await clear_temp_messages(context.bot, chat_id, user_id)
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
        ok, _, _, msg = await asyncio.to_thread(client.check_session)
        RUNTIME["last_status"] = msg
        icon = "✅" if ok else "❌"
        await query.edit_message_text(
            f"{icon} **Cookie Check Result:**\n`{msg}`",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif data == "clear_data_confirm":
        await clear_temp_messages(context.bot, chat_id, user_id)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ হ্যাঁ, সব মুছে ফেলুন", callback_data="clear_data_yes")],
            [
                InlineKeyboardButton("⬅️ Back", callback_data="home"),
                InlineKeyboardButton("🏠 Home", callback_data="home"),
            ],
        ])
        await query.edit_message_text(
            "⚠️ আপনি কি ডাটাবেস থেকে সেভ করা **Cookie, Post ID এবং Comments** মুছে ফেলতে চান?",
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
        await clear_temp_messages(context.bot, chat_id, user_id)
        cfg = get_settings()
        if RUNTIME["running"]:
            await show_clean_home(context.bot, chat_id, user_id, message_to_edit=query.message)
            return

        if not cfg["cookie"] or not cfg["post_id"] or not cfg["comments"]:
            await query.edit_message_text(
                "❌ Start করার আগে **Cookie**, **Post ID**, এবং **Comments** সেট করুন।",
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

    # ইউজারের পাঠানো মেসেজটি সাথে সাথে ডিলিট করে চ্যাট পরিষ্কার রাখা
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
        clean_id = extract_post_id(text)
        update_setting("post_id", clean_id)
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
            track_temp_msg(user_id, err.message_id)
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
            track_temp_msg(user_id, err.message_id)
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

    logger.info("Starting Telegram FB Comment Bot with SQLite & Clean UI...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
