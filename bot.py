import os
import re
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

# ইন-মেমোরি বট স্টেট (প্রয়োজনে SQLite যুক্ত করা যাবে)
STATE = {
    "cookie": os.getenv("FB_COOKIE", "").strip(),
    "post_id": os.getenv("POST_ID", "").strip(),
    "comments": [
        c.strip()
        for c in os.getenv("COMMENTS", "Nice post!|Great update!|Awesome!").split("|")
        if c.strip()
    ],
    "min_delay": int(os.getenv("MIN_DELAY", "25")),
    "max_delay": int(os.getenv("MAX_DELAY", "60")),
    "running": False,
    "sent_count": 0,
    "fail_count": 0,
    "last_status": "Idle",
    "worker_task": None,
}

# ইউজার ইনপুট স্টেট ট্র্যাকিং
USER_INPUT_MODE: dict[int, str] = {}


def is_authorized(user_id: int) -> bool:
    if ADMIN_ID == 0:
        return True
    return user_id == ADMIN_ID


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


def home_keyboard() -> InlineKeyboardMarkup:
    status_btn = (
        InlineKeyboardButton("⏹ Stop Worker", callback_data="stop_worker")
        if STATE["running"]
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
        [InlineKeyboardButton("🔄 Refresh Status", callback_data="home")],
    ])


def nav_keyboard(back_to: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Back", callback_data=back_to),
            InlineKeyboardButton("🏠 Home", callback_data="home"),
        ]
    ])


def build_dashboard_text() -> str:
    cookie_status = "✅ Set" if STATE["cookie"] else "❌ Not Set"
    post_status = f"`{STATE['post_id']}`" if STATE["post_id"] else "❌ Not Set"
    run_status = "🟢 Running" if STATE["running"] else "🔴 Stopped"
    comments_preview = ", ".join(STATE["comments"][:3]) if STATE["comments"] else "None"

    return (
        "🤖 **FB Auto Comment Bot (Railway)**\n\n"
        f"• **Status:** {run_status}\n"
        f"• **Cookie:** {cookie_status}\n"
        f"• **Post ID:** {post_status}\n"
        f"• **Comments ({len(STATE['comments'])}):** `{comments_preview}`\n"
        f"• **Delay:** `{STATE['min_delay']}s - {STATE['max_delay']}s`\n"
        f"• **Sent / Failed:** `{STATE['sent_count']} / {STATE['fail_count']}`\n"
        f"• **Last Log:** `{STATE['last_status']}`"
    )


async def comment_worker_loop(app: Application, chat_id: int):
    client = FBCommentClient(STATE["cookie"])
    ok, fb_dtsg, jazoest, msg = await asyncio.to_thread(client.check_session)
    if not ok:
        STATE["running"] = False
        STATE["last_status"] = msg
        await app.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Worker বন্ধ হয়েছে: `{msg}`",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )
        return

    idx = 0
    consecutive_failures = 0

    while STATE["running"]:
        comments = STATE["comments"]
        if not comments:
            STATE["running"] = False
            STATE["last_status"] = "No comments configured."
            break

        text = comments[idx % len(comments)]
        idx += 1

        success, status_msg = await asyncio.to_thread(
            client.send_comment, STATE["post_id"], text, fb_dtsg, jazoest
        )

        if success:
            STATE["sent_count"] += 1
            STATE["last_status"] = f"OK: {text[:25]}"
            consecutive_failures = 0
        else:
            STATE["fail_count"] += 1
            STATE["last_status"] = status_msg
            consecutive_failures += 1

            if consecutive_failures >= 3:
                STATE["running"] = False
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ টানা ৩ বার ব্যর্থ হওয়ায় Worker নিরাপদে বন্ধ করা হয়েছে।\nকারণ: `{status_msg}`",
                    parse_mode="Markdown",
                    reply_markup=nav_keyboard("home"),
                )
                break

            ok, new_dtsg, new_jazoest, chk_msg = await asyncio.to_thread(client.check_session)
            if not ok:
                STATE["running"] = False
                STATE["last_status"] = chk_msg
                break
            fb_dtsg = new_dtsg or fb_dtsg
            jazoest = new_jazoest or jazoest

        delay = random.randint(STATE["min_delay"], STATE["max_delay"])
        for _ in range(delay):
            if not STATE["running"]:
                break
            await asyncio.sleep(1)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("⛔ আপনি এই বট ব্যবহারের জন্য অনুমোদিত নন।")
        return

    USER_INPUT_MODE.pop(user_id, None)
    await update.message.reply_text(
        build_dashboard_text(),
        parse_mode="Markdown",
        reply_markup=home_keyboard(),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if not is_authorized(user_id):
        await query.edit_message_text("⛔ অনুমোদিত নয়।")
        return

    data = query.data

    if data == "home":
        USER_INPUT_MODE.pop(user_id, None)
        await query.edit_message_text(
            build_dashboard_text(),
            parse_mode="Markdown",
            reply_markup=home_keyboard(),
        )

    elif data == "set_cookie":
        USER_INPUT_MODE[user_id] = "cookie"
        await query.edit_message_text(
            "🍪 আপনার Facebook **Cookie** স্ট্রিংটি পাঠান:\n\n"
            "উদাহরণ: `c_user=1000...; xs=...; datr=...;`",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif data == "set_post":
        USER_INPUT_MODE[user_id] = "post_id"
        await query.edit_message_text(
            "🔗 টার্গেট পোস্টের **Post URL** বা **Post ID** পাঠান:",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif data == "set_comments":
        USER_INPUT_MODE[user_id] = "comments"
        await query.edit_message_text(
            "💬 কমেন্ট লিস্ট পাঠান (প্রতি লাইনে একটি বা `|` দিয়ে আলাদা করে):\n\n"
            "উদাহরণ:\n`Nice post!`\n`Great update!`\n`Awesome!`",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif data == "set_delay":
        USER_INPUT_MODE[user_id] = "delay"
        await query.edit_message_text(
            "⏱ মিনিমাম ও ম্যাক্সিমাম ডিলে (সেকেন্ডে) পাঠান:\n\n"
            "উদাহরণ: `25-60`",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif data == "check_cookie":
        if not STATE["cookie"]:
            await query.edit_message_text(
                "❌ আগে **Set Cookie** থেকে কুকি সেট করুন।",
                parse_mode="Markdown",
                reply_markup=nav_keyboard("home"),
            )
            return

        await query.edit_message_text("⏳ কুকি যাচাই করা হচ্ছে...")
        client = FBCommentClient(STATE["cookie"])
        ok, _, _, msg = await asyncio.to_thread(client.check_session)
        STATE["last_status"] = msg
        icon = "✅" if ok else "❌"
        await query.edit_message_text(
            f"{icon} **Cookie Check Result:**\n`{msg}`",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif data == "start_worker":
        if STATE["running"]:
            await query.edit_message_text(
                "⚠️ Worker ইতিমধ্যে চলছে।",
                reply_markup=nav_keyboard("home"),
            )
            return

        if not STATE["cookie"] or not STATE["post_id"] or not STATE["comments"]:
            await query.edit_message_text(
                "❌ Start করার আগে **Cookie**, **Post ID**, এবং **Comments** সেট করুন।",
                parse_mode="Markdown",
                reply_markup=nav_keyboard("home"),
            )
            return

        STATE["running"] = True
        STATE["last_status"] = "Starting..."
        STATE["worker_task"] = asyncio.create_task(
            comment_worker_loop(context.application, query.message.chat_id)
        )
        await query.edit_message_text(
            build_dashboard_text(),
            parse_mode="Markdown",
            reply_markup=home_keyboard(),
        )

    elif data == "stop_worker":
        STATE["running"] = False
        STATE["last_status"] = "Stopped by user"
        await query.edit_message_text(
            build_dashboard_text(),
            parse_mode="Markdown",
            reply_markup=home_keyboard(),
        )


async def text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        return

    mode = USER_INPUT_MODE.get(user_id)
    if not mode:
        return

    text = update.message.text.strip()

    if mode == "cookie":
        STATE["cookie"] = text
        USER_INPUT_MODE.pop(user_id, None)
        await update.message.reply_text(
            "✅ **Cookie আপডেট হয়েছে!**",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif mode == "post_id":
        STATE["post_id"] = extract_post_id(text)
        USER_INPUT_MODE.pop(user_id, None)
        await update.message.reply_text(
            f"✅ **Post ID সেট হয়েছে:** `{STATE['post_id']}`",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif mode == "comments":
        raw_items = re.split(r"[\n|]+", text)
        comments = [c.strip() for c in raw_items if c.strip()]
        if not comments:
            await update.message.reply_text(
                "❌ অন্তত একটি বৈধ কমেন্ট দিন।",
                reply_markup=nav_keyboard("home"),
            )
            return
        STATE["comments"] = comments
        USER_INPUT_MODE.pop(user_id, None)
        await update.message.reply_text(
            f"✅ **{len(comments)}টি কমেন্ট সেভ হয়েছে!**",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )

    elif mode == "delay":
        m = re.match(r"^(\d+)\s*[-, ]\s*(\d+)$", text)
        if not m:
            await update.message.reply_text(
                "❌ সঠিক ফরম্যাটে দিন, যেমন: `25-60`",
                parse_mode="Markdown",
                reply_markup=nav_keyboard("home"),
            )
            return
        min_d = max(5, int(m.group(1)))
        max_d = max(min_d + 5, int(m.group(2)))
        STATE["min_delay"] = min_d
        STATE["max_delay"] = max_d
        USER_INPUT_MODE.pop(user_id, None)
        await update.message.reply_text(
            f"✅ **Delay সেট হয়েছে:** `{min_d}s - {max_d}s`",
            parse_mode="Markdown",
            reply_markup=nav_keyboard("home"),
        )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing!")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))

    logger.info("Starting Telegram FB Comment Bot...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
