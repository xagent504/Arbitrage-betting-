import os
import re
import json
import sqlite3
import asyncio
import random
import logging
from urllib.parse import urlparse

from playwright.async_api import async_playwright
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
logger = logging.getLogger("fb-playwright-bot")

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


def format_clean_error(exc: Exception) -> str:
    lines = [
        line.strip(" ║╔╗╚╝═─│┌┐└┘")
        for line in str(exc).splitlines()
        if line.strip(" ║╔╗╚╝═─│┌┐└┘")
    ]
    if not lines:
        return str(exc)[:120]
    if len(lines) >= 2 and lines[0].endswith(":"):
        return f"{lines[0]} {lines[1]}"[:140]
    return lines[0][:140]


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
        default_min = int(os.getenv("MIN_DELAY", "30"))
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


# -------------------- Cookie & URL Helpers --------------------
def parse_cookie_string(raw_cookie: str) -> dict:
    cleaned_raw = re.sub(r"[\r\n]+", ";", raw_cookie or "")
    cookies = {}
    for part in cleaned_raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = re.sub(r"[\x00-\x1f\x7f;,\s]+", "", k.strip())
        v = re.sub(r"[\x00-\x1f\x7f;]+", "", v.strip())
        if k and v:
            cookies[k] = v
    return cookies


def build_playwright_cookies(raw_cookie: str) -> list[dict]:
    parsed = parse_cookie_string(raw_cookie)
    pw_cookies = []
    for name, value in parsed.items():
        pw_cookies.append({
            "name": name,
            "value": value,
            "domain": ".facebook.com",
            "path": "/",
            "secure": True,
            "sameSite": "Lax",
        })
    return pw_cookies


def normalize_post_url(post_input: str) -> str:
    post_input = post_input.strip()
    if post_input.startswith("http://") or post_input.startswith("https://"):
        parsed = urlparse(post_input)
        host = parsed.netloc.lower()
        if "facebook.com" in host or "fb.watch" in host:
            return post_input
    if post_input.isdigit():
        return f"https://www.facebook.com/{post_input}"
    return f"https://www.facebook.com/{post_input.lstrip('/')}"


# -------------------- Playwright FB Client --------------------
class FBPlaywrightClient:
    def __init__(self, cookie_str: str):
        self.cookie_str = cookie_str
        self.parsed_dict = parse_cookie_string(cookie_str)
        self.pw_cookies = build_playwright_cookies(cookie_str)
        self.uid = self.parsed_dict.get("c_user", "Unknown")

    async def _new_context(self, playwright):
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        if self.pw_cookies:
            await context.add_cookies(self.pw_cookies)
        return browser, context

    async def _find_comment_box(self, page):
        selectors = [
            'div[contenteditable="true"][role="textbox"]',
            'div[role="textbox"][contenteditable="true"]',
            'div[contenteditable="true"][aria-label*="comment" i]',
            'div[contenteditable="true"][aria-label*="Write a comment" i]',
            'div[contenteditable="true"][aria-label*="মন্তব্য" i]',
            'textarea[name="comment_text"]',
            'textarea[placeholder*="comment" i]',
        ]
        for sel in selectors:
            loc = page.locator(sel)
            count = await loc.count()
            for i in range(count):
                item = loc.nth(i)
                try:
                    if not await item.is_visible():
                        continue
                    aria = (await item.get_attribute("aria-label") or "").lower()
                    placeholder = (await item.get_attribute("placeholder") or "").lower()
                    if any(bad in aria or bad in placeholder for bad in ("search", "খুঁজুন", "messenger", "chat")):
                        continue
                    await item.scroll_into_view_if_needed(timeout=4000)
                    return item
                except Exception:
                    continue
        return None

    async def _try_click_comment_trigger(self, page):
        # শুধু পোস্টের অ্যাকশন বাটন হিসেবে থাকা "Comment" বা "মন্তব্য" বাটন খোঁজা
        triggers = [
            page.get_by_role("button", name=re.compile(r"^(comment|মন্তব্য)$", re.I)),
            page.locator('div[aria-label*="Leave a comment" i][role="button"]'),
            page.locator('span[aria-label*="Leave a comment" i]'),
        ]
        for loc in triggers:
            try:
                if await loc.count() > 0:
                    btn = loc.first
                    if await btn.is_visible():
                        await btn.scroll_into_view_if_needed(timeout=4000)
                        await btn.click(timeout=5000)
                        await page.wait_for_timeout(1500)
                        return
            except Exception:
                continue

    async def check_session(self) -> tuple[bool, str]:
        if not self.pw_cookies or "c_user" not in self.parsed_dict:
            return False, "Missing c_user or invalid cookie format."

        browser = None
        try:
            async with async_playwright() as p:
                browser, context = await self._new_context(p)
                page = await context.new_page()
                await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=35000)
                await page.wait_for_timeout(3000)

                url = page.url.lower()
                if "login" in url or "checkpoint" in url:
                    return False, "Cookie expired, logged out, or checkpoint."

                cookies_now = await context.cookies("https://www.facebook.com")
                c_user = next((c["value"] for c in cookies_now if c["name"] == "c_user"), None)
                if not c_user:
                    return False, "Session not active (c_user missing)."

                return True, f"Valid (UID: {c_user})"
        except Exception as exc:
            return False, f"Browser error: {format_clean_error(exc)}"
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def send_comment(self, post_target: str, text: str) -> tuple[bool, str]:
        post_url = normalize_post_url(post_target)
        browser = None
        try:
            async with async_playwright() as p:
                browser, context = await self._new_context(p)
                page = await context.new_page()

                await page.goto(post_url, wait_until="domcontentloaded", timeout=40000)
                await page.wait_for_timeout(5000)

                url = page.url.lower()
                if "login" in url or "checkpoint" in url:
                    return False, "Login required or checkpoint hit."

                box = None
                for step in range(4):
                    box = await self._find_comment_box(page)
                    if box:
                        break
                    await self._try_click_comment_trigger(page)
                    box = await self._find_comment_box(page)
                    if box:
                        break
                    await page.mouse.wheel(0, 500 + step * 250)
                    await page.wait_for_timeout(1200)

                if not box:
                    page_title = await page.title()
                    return False, f"Comment box not found ({page_title[:30]})."

                await box.click(timeout=10000)
                await page.wait_for_timeout(500)

                await page.keyboard.type(text, delay=25)
                await page.wait_for_timeout(1000)

                await page.keyboard.press("Enter")
                await page.wait_for_timeout(3500)

                body_text = (await page.locator("body").inner_text()).lower()
                if "checkpoint" in page.url.lower():
                    return False, "Checkpoint triggered after comment."
                if "you can't use this feature right now" in body_text or "temporarily blocked" in body_text:
                    return False, "Commenting temporarily blocked by FB."

                snippet = text.strip()[:16]
                if snippet and snippet.lower() in body_text:
                    return True, f"Verified: {snippet}"

                return True, f"Submitted: {snippet}"
        except Exception as exc:
            return False, f"Playwright error: {format_clean_error(exc)}"
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass


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
            InlineKeyboardButton("🔗 Set Post URL", callback_data="set_post"),
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
    post_status = f"`{cfg['post_id'][:34]}`" if cfg["post_id"] else "❌ Not Set"
    run_status = "🟢 Running" if RUNTIME["running"] else "🔴 Stopped"
    comments_preview = ", ".join(cfg["comments"][:3]) if cfg["comments"] else "None"

    return (
        "🤖 **FB Auto Comment Bot (Playwright Mode)**\n\n"
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
    client = FBPlaywrightClient(cfg["cookie"])
    ok, msg = await client.check_session()

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
            RUNTIME["last_status"] = "Missing comments or post URL."
            break

        text = comments[idx % len(comments)]
        idx += 1

        success, status_msg = await client.send_comment(post_target, text)

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
            "🔗 টার্গেট পোস্টের **Post URL** পাঠান:\n\n"
            "উদাহরণ: `https://www.facebook.com/.../posts/...`",
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
            "⏱ মিনিমাম ও ম্যাক্সিমাম ডিলে (সেকেন্ডে) পাঠান:\n\nউদাহরণ: `30-60`",
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

        await query.edit_message_text("⏳ ব্রাউজার দিয়ে কুকি যাচাই করা হচ্ছে...")
        client = FBPlaywrightClient(cfg["cookie"])
        ok, msg = await client.check_session()
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
                "❌ Start করার আগে **Cookie**, **Post URL**, এবং **Comments** সেট করুন।",
                parse_mode="Markdown",
                reply_markup=nav_keyboard("home"),
            )
            return

        RUNTIME["running"] = True
        RUNTIME["last_status"] = "Starting browser..."
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
                text="❌ সঠিক ফরম্যাটে দিন, যেমন: `30-60`",
                parse_mode="Markdown",
                reply_markup=nav_keyboard("home"),
            )
            track_msg(user_id, err.message_id)
            return
        min_d = max(10, int(m.group(1)))
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

    logger.info("Starting Telegram FB Comment Bot (Playwright Mode)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
