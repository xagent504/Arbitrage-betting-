import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ================= CONFIG & PERSISTENT STORAGE =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

DEFAULT_ADMIN_IDS = {8622779484}
ENV_ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
ADMIN_IDS = DEFAULT_ADMIN_IDS | ENV_ADMIN_IDS

ADMIN_WHATSAPP_LABEL = "Message Abhishap Noctis on WhatsApp"
ADMIN_WHATSAPP_URL = "https://wa.me/8801791110761"

ODDS_API_KEYS = [
    k.strip() for k in os.getenv("ODDS_API_KEYS", "").split(",") if k.strip()
]
REGIONS = os.getenv("REGIONS", "eu,uk").strip()
BD_TIMEZONE_OFFSET = 6

# আপডেট বা রিস্টার্ট দিলেও যেন ডাটা না হারায়, তাই আলাদা ডিরেক্টরিতে DB রাখা হয়েছে
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "arbitrage.db"

BASE_URL = "https://api.the-odds-api.com/v4"
SPORT_PREFIX_MAP = {
    "football": "soccer_",
    "basketball": "basketball_",
    "tennis": "tennis_",
}
SPORT_LABELS = {
    "football": "⚽ Football",
    "basketball": "🏀 Basketball",
    "tennis": "🎾 Tennis",
}

DEFAULT_WELCOME = (
    "👋 **স্বাগতম Arbitrage Scanner বটে!**\n\n"
    "শুধুমাত্র নিশ্চিত **Arbitrage** সুযোগ থাকা ম্যাচগুলোই ফিল্টার করে আপনার সামনে আনা হবে—কোনো সাধারণ ম্যাচ আসবে না।\n\n"
    "নিচের বাটনে চাপুন 👇"
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ================= DATABASE =================
def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            vip_until REAL DEFAULT 0,
            joined_at REAL NOT NULL,
            last_seen REAL NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_cache (
            sport TEXT PRIMARY KEY,
            updated_at REAL NOT NULL,
            event_count INTEGER DEFAULT 0,
            arb_count INTEGER DEFAULT 0,
            raw_json TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS arbitrage_matches (
            arb_id TEXT PRIMARY KEY,
            sport TEXT NOT NULL,
            data_json TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)

    defaults = {
        "min_profit_pct": os.getenv("MIN_PROFIT_PERCENT", "0.1"),
        "cache_ttl_sec": os.getenv("CACHE_TTL_SECONDS", "120"),
        "vip_channel_id": os.getenv("VIP_CHANNEL_ID", ""),
        "vip_min_profit_pct": os.getenv("VIP_MIN_PROFIT_PERCENT", "2.0"),
        "welcome_message": DEFAULT_WELCOME,
    }
    for k, v in defaults.items():
        cur.execute(
            "INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)",
            (k, str(v)),
        )

    conn.commit()
    conn.close()


def get_setting(key: str, default: str = "") -> str:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def register_user(user):
    if not user:
        return
    now = time.time()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (user_id, username, full_name, vip_until, joined_at, last_seen)
        VALUES (?, ?, ?, 0, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            full_name = excluded.full_name,
            last_seen = excluded.last_seen
        """,
        (user.id, user.username or "", user.full_name or "", now, now),
    )
    conn.commit()
    conn.close()


def get_all_users() -> list[dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users ORDER BY joined_at DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_total_users_count() -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM users")
    row = cur.fetchone()
    conn.close()
    return int(row["cnt"]) if row else 0


def is_user_vip(user_id: int) -> tuple[bool, float]:
    if user_id in ADMIN_IDS:
        return True, 0.0
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT vip_until FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    vip_until = float(row["vip_until"]) if row and row["vip_until"] else 0.0
    return vip_until > time.time(), vip_until


def grant_vip_days(user_id: int, days: int) -> float:
    now = time.time()
    _, current_until = is_user_vip(user_id)
    base = current_until if current_until > now else now
    new_until = base + (days * 86400)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (user_id, username, full_name, vip_until, joined_at, last_seen)
        VALUES (?, '', '', ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET vip_until = ?
        """,
        (user_id, new_until, now, now, new_until),
    )
    conn.commit()
    conn.close()
    return new_until


def revoke_vip(user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET vip_until = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_cached_odds(sport: str):
    ttl = int(float(get_setting("cache_ttl_sec", "120")))
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT updated_at, raw_json FROM api_cache WHERE sport = ?", (sport,))
    row = cur.fetchone()
    conn.close()
    if not row or (time.time() - row["updated_at"]) > ttl:
        return None
    return json.loads(row["raw_json"])


def save_cached_odds(sport: str, data: list, arb_count: int = None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT arb_count FROM api_cache WHERE sport = ?", (sport,))
    existing = cur.fetchone()
    final_arb = arb_count if arb_count is not None else (existing["arb_count"] if existing else 0)
    cur.execute(
        """
        INSERT OR REPLACE INTO api_cache (sport, updated_at, event_count, arb_count, raw_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (sport, time.time(), len(data), final_arb, json.dumps(data)),
    )
    conn.commit()
    conn.close()


def update_cache_arb_count(sport: str, arb_count: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE api_cache SET arb_count = ? WHERE sport = ?", (arb_count, sport))
    conn.commit()
    conn.close()


def clear_all_cache():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM api_cache")
    conn.commit()
    conn.close()


def get_cache_status_list() -> list[dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT sport, updated_at, event_count, arb_count FROM api_cache")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def save_arbitrage_list(sport: str, arb_list: list):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM arbitrage_matches WHERE sport = ?", (sport,))
    now = time.time()
    for item in arb_list:
        cur.execute(
            """
            INSERT OR REPLACE INTO arbitrage_matches (arb_id, sport, data_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (item["arb_id"], sport, json.dumps(item), now),
        )
    conn.commit()
    conn.close()
    update_cache_arb_count(sport, len(arb_list))


def get_arbitrage_by_id(arb_id: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT data_json FROM arbitrage_matches WHERE arb_id = ?", (arb_id,))
    row = cur.fetchone()
    conn.close()
    return json.loads(row["data_json"]) if row else None


# ================= ODDS FEED & CREDIT TRACKER =================
class OddsFeedService:
    def __init__(self):
        self.key_index = 0
        self.last_credits_remaining = None
        self.last_credits_used = None
        self.last_api_status = "Ready"

    def _get_current_key(self) -> str:
        if not ODDS_API_KEYS:
            raise ValueError("ODDS_API_KEYS সেট করা নেই! Environment Variable-এ API Key দিন।")
        return ODDS_API_KEYS[self.key_index % len(ODDS_API_KEYS)]

    def _rotate_key(self):
        if len(ODDS_API_KEYS) > 1:
            self.key_index = (self.key_index + 1) % len(ODDS_API_KEYS)
            logger.warning("API Key রোটেট করা হয়েছে: %s", self.key_index)

    def _request(self, endpoint: str, params: dict = None):
        params = params or {}
        attempts = max(1, len(ODDS_API_KEYS))
        for _ in range(attempts):
            api_key = self._get_current_key()
            req_params = {**params, "apiKey": api_key}
            try:
                resp = requests.get(f"{BASE_URL}{endpoint}", params=req_params, timeout=15)
                if "x-requests-remaining" in resp.headers:
                    self.last_credits_remaining = resp.headers.get("x-requests-remaining")
                if "x-requests-used" in resp.headers:
                    self.last_credits_used = resp.headers.get("x-requests-used")

                if resp.status_code in (401, 429):
                    self.last_api_status = f"Key limit/error ({resp.status_code})"
                    self._rotate_key()
                    continue
                resp.raise_for_status()
                self.last_api_status = "OK"
                return resp.json()
            except requests.RequestException as e:
                self.last_api_status = f"Error: {e}"
                logger.error("API রিকোয়েস্ট এরর (%s): %s", endpoint, e)
                self._rotate_key()
        return []

    def get_active_sport_keys(self, category: str) -> list[str]:
        prefix = SPORT_PREFIX_MAP.get(category)
        if not prefix:
            return []
        sports = self._request("/sports")
        if not isinstance(sports, list):
            return []
        active_keys = [
            s["key"]
            for s in sports
            if s.get("active") and s["key"].startswith(prefix) and not s.get("has_outrights")
        ]
        return active_keys[:10]

    def fetch_odds(self, category: str, force_refresh: bool = False) -> list[dict]:
        if not force_refresh:
            cached = get_cached_odds(category)
            if cached is not None:
                return cached

        sport_keys = self.get_active_sport_keys(category)
        all_events = []
        for sport_key in sport_keys:
            events = self._request(
                f"/sports/{sport_key}/odds",
                params={
                    "regions": REGIONS,
                    "markets": "h2h,totals",
                    "oddsFormat": "decimal",
                },
            )
            if isinstance(events, list):
                all_events.extend(events)

        if all_events:
            save_cached_odds(category, all_events)
        return all_events


odds_feed = OddsFeedService()


# ================= ARBITRAGE ANALYZER =================
def _parse_bd_time(commence_time_str: str):
    dt_utc = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
    bd_tz = timezone(timedelta(hours=BD_TIMEZONE_OFFSET))
    return dt_utc, dt_utc.astimezone(bd_tz)


def analyze_arbitrage(sport: str, events: list[dict], hours_limit: int) -> list[dict]:
    min_profit = float(get_setting("min_profit_pct", "0.1"))
    now_utc = datetime.now(timezone.utc)
    max_utc = now_utc + timedelta(hours=hours_limit)
    results = []

    prefix_code = {"football": "fb", "basketball": "bb", "tennis": "tn"}.get(sport, "sp")
    has_draw = sport == "football"

    for event in events:
        commence_str = event.get("commence_time")
        if not commence_str:
            continue

        dt_utc, dt_bd = _parse_bd_time(commence_str)
        if not (now_utc <= dt_utc <= max_utc):
            continue

        home = event.get("home_team", "Team 1")
        away = event.get("away_team", "Team 2")
        event_id = event.get("id", f"{home}_{away}")
        league = event.get("sport_title", sport.title())
        match_time_bd = dt_bd.strftime("%d %b, %I:%M %p")

        best_h2h = {}
        for bm in event.get("bookmakers", []):
            bm_title = bm.get("title", "Unknown")
            for market in bm.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                for out in market.get("outcomes", []):
                    name = out.get("name")
                    price = float(out.get("price", 0))
                    if price <= 1.0 or not name:
                        continue
                    if name not in best_h2h or price > best_h2h[name]["price"]:
                        best_h2h[name] = {"price": price, "bookmaker": bm_title}

        required_keys = [home, away, "Draw"] if has_draw else [home, away]
        if all(k in best_h2h for k in required_keys):
            legs_raw = [{"label": f"W1 ({home})", **best_h2h[home]}]
            if has_draw:
                legs_raw.append({"label": "Draw (X)", **best_h2h["Draw"]})
            legs_raw.append({"label": f"W2 ({away})", **best_h2h[away]})

            inv_sum = sum(1 / leg["price"] for leg in legs_raw)
            if inv_sum < 1.0:
                profit_pct = round(((1 / inv_sum) - 1) * 100, 2)
                if profit_pct >= min_profit:
                    results.append({
                        "arb_id": f"{prefix_code}_{event_id}_h2h",
                        "sport": sport,
                        "league": league,
                        "home": home,
                        "away": away,
                        "match_time_bd": match_time_bd,
                        "market_type": "Match Winner (1X2)" if has_draw else "Match Winner (W1 / W2)",
                        "profit_pct": profit_pct,
                        "legs": [
                            {
                                "label": leg["label"],
                                "bookmaker": leg["bookmaker"],
                                "odds": leg["price"],
                                "stake_pct": round((1 / leg["price"]) / inv_sum * 100, 2),
                            }
                            for leg in legs_raw
                        ],
                    })

        totals_by_point = {}
        for bm in event.get("bookmakers", []):
            bm_title = bm.get("title", "Unknown")
            for market in bm.get("markets", []):
                if market.get("key") != "totals":
                    continue
                for out in market.get("outcomes", []):
                    side = out.get("name")
                    point = out.get("point")
                    price = float(out.get("price", 0))
                    if point is None or price <= 1.0 or side not in ("Over", "Under"):
                        continue
                    totals_by_point.setdefault(point, {})
                    if side not in totals_by_point[point] or price > totals_by_point[point][side]["price"]:
                        totals_by_point[point][side] = {"price": price, "bookmaker": bm_title}

        for point, sides in totals_by_point.items():
            if "Over" in sides and "Under" in sides:
                ov = sides["Over"]
                un = sides["Under"]
                inv_sum = (1 / ov["price"]) + (1 / un["price"])
                if inv_sum < 1.0:
                    profit_pct = round(((1 / inv_sum) - 1) * 100, 2)
                    if profit_pct >= min_profit:
                        results.append({
                            "arb_id": f"{prefix_code}_{event_id}_tot_{point}",
                            "sport": sport,
                            "league": league,
                            "home": home,
                            "away": away,
                            "match_time_bd": match_time_bd,
                            "market_type": f"Over / Under ({point})",
                            "profit_pct": profit_pct,
                            "legs": [
                                {
                                    "label": f"Over {point}",
                                    "bookmaker": ov["bookmaker"],
                                    "odds": ov["price"],
                                    "stake_pct": round((1 / ov["price"]) / inv_sum * 100, 2),
                                },
                                {
                                    "label": f"Under {point}",
                                    "bookmaker": un["bookmaker"],
                                    "odds": un["price"],
                                    "stake_pct": round((1 / un["price"]) / inv_sum * 100, 2),
                                },
                            ],
                        })

    results.sort(key=lambda x: x["profit_pct"], reverse=True)
    save_arbitrage_list(sport, results)
    return results


# ================= FORMATTERS & KEYBOARDS =================
def format_welcome_text() -> str:
    welcome_msg = get_setting("welcome_message", DEFAULT_WELCOME)
    total_users = get_total_users_count()
    return (
        f"{welcome_msg}\n\n"
        f"👥 **মোট ইউজার সংখ্যা:** `{total_users}` জন\n"
        f"📞 **অ্যাডমিন সাপোর্ট:** [{ADMIN_WHATSAPP_LABEL}]({ADMIN_WHATSAPP_URL})"
    )


def home_keyboard(user_id: int):
    rows = [
        [InlineKeyboardButton("🔍 Arbitrage ম্যাচ দেখতে এখানে চাপুন", callback_data="menu_sports")],
        [InlineKeyboardButton("👑 VIP / সাবস্ক্রিপশন তথ্য", callback_data="vip_info")],
        [InlineKeyboardButton(f"💬 {ADMIN_WHATSAPP_LABEL}", url=ADMIN_WHATSAPP_URL)],
    ]
    if user_id in ADMIN_IDS:
        rows.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_home")])
    return InlineKeyboardMarkup(rows)


def sports_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚽ Football", callback_data="sport_football")],
        [InlineKeyboardButton("🏀 Basketball", callback_data="sport_basketball")],
        [InlineKeyboardButton("🎾 Tennis", callback_data="sport_tennis")],
        [InlineKeyboardButton("🏠 Home", callback_data="go_home")],
    ])


def time_keyboard(sport: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱ ৬ ঘণ্টার মধ্যে", callback_data=f"time_{sport}_6")],
        [InlineKeyboardButton("⏱ ১২ ঘণ্টার মধ্যে", callback_data=f"time_{sport}_12")],
        [InlineKeyboardButton("⏱ ২৪ ঘণ্টার মধ্যে", callback_data=f"time_{sport}_24")],
        [
            InlineKeyboardButton("🔙 Back", callback_data="menu_sports"),
            InlineKeyboardButton("🏠 Home", callback_data="go_home"),
        ],
    ])


def admin_keyboard():
    min_p = get_setting("min_profit_pct", "0.1")
    ttl = get_setting("cache_ttl_sec", "120")
    vip_min = get_setting("vip_min_profit_pct", "2.0")
    vip_ch = get_setting("vip_channel_id", "") or "Set হয়নি"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📈 Min Profit: {min_p}% (পরিবর্তন)", callback_data="adm_profit_menu")],
        [InlineKeyboardButton(f"⏱ Cache TTL: {ttl}s (পরিবর্তন)", callback_data="adm_ttl_menu")],
        [InlineKeyboardButton(f"👑 VIP সেটিংস ({vip_min}% | {vip_ch})", callback_data="adm_vip_menu")],
        [InlineKeyboardButton("👥 ইউজার লিস্ট ও VIP দিন", callback_data="adm_users_0")],
        [InlineKeyboardButton("📊 API ক্রেডিট ও ক্যাশ মনিটর", callback_data="adm_api_status")],
        [InlineKeyboardButton("📝 Welcome Message সেট করুন", callback_data="adm_set_welcome")],
        [InlineKeyboardButton("📢 সবাইকে Broadcast পাঠান", callback_data="adm_broadcast_start")],
        [InlineKeyboardButton(f"💬 {ADMIN_WHATSAPP_LABEL}", url=ADMIN_WHATSAPP_URL)],
        [InlineKeyboardButton("🏠 Home", callback_data="go_home")],
    ])


def build_detail_text(match: dict, budget: float) -> str:
    net_profit = round(budget * (match["profit_pct"] / 100), 2)
    lines = [
        "🎯 **ARBITRAGE বিস্তারিত রিপোর্ট**",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🏆 **লিগ:** {match['league']}",
        f"⚔️ **ম্যাচ:** `{match['home']}` **vs** `{match['away']}`",
        f"🕒 **খেলার সময় (BD):** {match['match_time_bd']}",
        f"📊 **মার্কেট:** {match['market_type']}",
        f"💰 **নিশ্চিত প্রফিট:** **+{match['profit_pct']}%** (নিট লাভ: `+{net_profit}`)",
        f"💵 **হিসাবের বাজেট:** `{budget}`",
        "━━━━━━━━━━━━━━━━━━━━",
        "📌 **কোন ওয়েবসাইটে কী ধরবেন:**\n",
    ]

    for idx, leg in enumerate(match["legs"], start=1):
        stake_amount = round(budget * (leg["stake_pct"] / 100), 2)
        payout = round(stake_amount * leg["odds"], 2)
        lines.append(
            f"**{idx}. সাইট:** 🌐 `{leg['bookmaker']}`\n"
            f"   • **অপশন:** {leg['label']}\n"
            f"   • **অডস:** `{leg['odds']}`\n"
            f"   • **ভাগ:** {leg['stake_pct']}% → ধরবেন `{stake_amount}` (রিটার্ন `{payout}`)\n"
        )

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 *অডস খুব দ্রুত বদলায়, তাই বেট ধরার আগে দুই সাইটেই লাইভ অডস মিলিয়ে নেবেন।*")
    return "\n".join(lines)


def detail_keyboard(arb_id: str, sport: str, hours: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💵 500", callback_data=f"calc|{arb_id}|{sport}|{hours}|500"),
            InlineKeyboardButton("💵 1000", callback_data=f"calc|{arb_id}|{sport}|{hours}|1000"),
            InlineKeyboardButton("💵 2000", callback_data=f"calc|{arb_id}|{sport}|{hours}|2000"),
            InlineKeyboardButton("💵 5000", callback_data=f"calc|{arb_id}|{sport}|{hours}|5000"),
        ],
        [InlineKeyboardButton("⌨️ নিজের বাজেট লিখুন", callback_data=f"custom_budget|{arb_id}|{sport}|{hours}")],
        [
            InlineKeyboardButton("🔙 ম্যাচ তালিকা", callback_data=f"time_{sport}_{hours}"),
            InlineKeyboardButton("🏠 Home", callback_data="go_home"),
        ],
    ])


# ================= COMMANDS & HANDLERS =================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    register_user(update.effective_user)
    await update.message.reply_text(
        format_welcome_text(),
        reply_markup=home_keyboard(update.effective_user.id),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ এই প্যানেল শুধুমাত্র অ্যাডমিনের জন্য।")
        return
    context.user_data.pop("awaiting_input", None)
    await update.message.reply_text(
        "⚙️ **Admin Panel**\nসব কিছু বাটনে চাপ দিয়ে অটোমেটিক নিয়ন্ত্রণ করুন:",
        reply_markup=admin_keyboard(),
        parse_mode="Markdown",
    )


async def text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    user_id = update.effective_user.id
    mode = context.user_data.get("awaiting_input")
    text_val = (update.message.text or "").strip()

    if not mode:
        return

    if mode.startswith("budget|"):
        _, arb_id, sport, hours_str = mode.split("|", 3)
        hours = int(hours_str)
        context.user_data.pop("awaiting_input", None)

        try:
            budget = float(text_val.replace(",", ""))
            if budget <= 0:
                raise ValueError()
        except ValueError:
            await update.message.reply_text(
                "⚠️ সঠিক সংখ্যা লিখুন (যেমন `1500` বা `3000`)।",
                parse_mode="Markdown",
            )
            return

        match = get_arbitrage_by_id(arb_id)
        if not match:
            await update.message.reply_text(
                "⚠️ এই ম্যাচটি আর নেই, নতুন করে স্ক্যান করুন।",
                reply_markup=sports_keyboard(),
            )
            return

        await update.message.reply_text(
            build_detail_text(match, budget),
            reply_markup=detail_keyboard(arb_id, sport, hours),
            parse_mode="Markdown",
        )
        return

    if user_id not in ADMIN_IDS:
        return

    if mode == "set_welcome":
        context.user_data.pop("awaiting_input", None)
        set_setting("welcome_message", update.message.text)
        await update.message.reply_text(
            "✅ **Welcome Message আপডেট হয়েছে!**",
            reply_markup=admin_keyboard(),
            parse_mode="Markdown",
        )

    elif mode == "set_vip_channel":
        context.user_data.pop("awaiting_input", None)
        set_setting("vip_channel_id", text_val)
        await update.message.reply_text(
            f"✅ **VIP Channel ID সেট হয়েছে:** `{text_val}`",
            reply_markup=admin_keyboard(),
            parse_mode="Markdown",
        )

    elif mode == "broadcast":
        context.user_data.pop("awaiting_input", None)
        users = get_all_users()
        sent = 0
        failed = 0
        status_msg = await update.message.reply_text(
            f"📢 `{len(users)}` জন ইউজারকে মেসেজ পাঠানো হচ্ছে...",
            parse_mode="Markdown",
        )
        for u in users:
            try:
                await context.bot.send_message(
                    chat_id=u["user_id"],
                    text=update.message.text,
                    parse_mode="Markdown",
                )
                sent += 1
            except Exception:
                failed += 1
        await status_msg.edit_text(
            f"✅ **Broadcast সম্পন্ন!**\n• সফল: `{sent}` জন\n• ব্যর্থ/ব্লকড: `{failed}` জন",
            reply_markup=admin_keyboard(),
            parse_mode="Markdown",
        )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_id = user.id
    register_user(user)
    data = query.data

    if data == "go_home":
        context.user_data.pop("awaiting_input", None)
        await query.edit_message_text(
            format_welcome_text(),
            reply_markup=home_keyboard(user_id),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    if data == "vip_info":
        is_vip, vip_until = is_user_vip(user_id)
        vip_min = get_setting("vip_min_profit_pct", "2.0")
        vip_ch = get_setting("vip_channel_id", "") or "শীঘ্রই যুক্ত হবে"
        if is_vip:
            until_str = datetime.fromtimestamp(
                vip_until, tz=timezone(timedelta(hours=BD_TIMEZONE_OFFSET))
            ).strftime("%d %b, %Y %I:%M %p")
            status_txt = (
                f"👑 আপনি একজন **VIP সদস্য**।\n• মেয়াদ শেষ: `{until_str}`"
                if user_id not in ADMIN_IDS
                else "👑 আপনি **Admin (Unlimited VIP)**।"
            )
        else:
            status_txt = "🆓 আপনি বর্তমানে **Standard** অ্যাক্সেসে আছেন।"

        text = (
            f"💎 **VIP সাবস্ক্রিপশন তথ্য**\n\n"
            f"{status_txt}\n"
            f"• VIP High-Profit Threshold: **+{vip_min}%+**\n"
            f"• VIP চ্যানেল: `{vip_ch}`\n"
            f"• যোগাযোগ: [{ADMIN_WHATSAPP_LABEL}]({ADMIN_WHATSAPP_URL})\n\n"
            f"👥 মোট বট ইউজার: `{get_total_users_count()}` জন"
        )
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"💬 {ADMIN_WHATSAPP_LABEL}", url=ADMIN_WHATSAPP_URL)],
                [InlineKeyboardButton("🏠 Home", callback_data="go_home")],
            ]),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    if data == "menu_sports":
        await query.edit_message_text(
            f"📂 **একটি স্পোর্টস সিলেক্ট করুন:**\n👥 মোট ইউজার: `{get_total_users_count()}` জন",
            reply_markup=sports_keyboard(),
            parse_mode="Markdown",
        )
        return

    if data.startswith("sport_"):
        sport = data.split("_", 1)[1]
        sport_name = SPORT_LABELS.get(sport, sport.title())
        await query.edit_message_text(
            f"আপনি সিলেক্ট করেছেন: **{sport_name}**\n\n🕒 **কত ঘণ্টার মধ্যে শুরু হওয়া ম্যাচ দেখতে চান?**",
            reply_markup=time_keyboard(sport),
            parse_mode="Markdown",
        )
        return

    if data.startswith("time_") or data.startswith("refresh_"):
        action, sport, hours_str = data.split("_")
        hours = int(hours_str)
        force = action == "refresh"
        sport_name = SPORT_LABELS.get(sport, sport.title())

        await query.edit_message_text(
            f"🔎 **{sport_name}**-এর আগামী **{hours} ঘণ্টার** মধ্যে শুধু Arbitrage ম্যাচ খোঁজা হচ্ছে... ⏳",
            parse_mode="Markdown",
        )

        try:
            events = odds_feed.fetch_odds(sport, force_refresh=force)
            arb_matches = analyze_arbitrage(sport, events, hours)
        except Exception as e:
            logger.error("Scan error: %s", e)
            await query.edit_message_text(
                f"⚠️ ডেটা আনতে সমস্যা হয়েছে:\n`{e}`",
                reply_markup=time_keyboard(sport),
                parse_mode="Markdown",
            )
            return

        if not arb_matches:
            await query.edit_message_text(
                f"❌ **{sport_name}**-এ আগামী **{hours} ঘণ্টার** মধ্যে এই মুহূর্তে কোনো Arbitrage ম্যাচ পাওয়া যায়নি।",
                reply_markup=time_keyboard(sport),
                parse_mode="Markdown",
            )
            return

        vip_ch_raw = get_setting("vip_channel_id", "").strip()
        vip_min = float(get_setting("vip_min_profit_pct", "2.0"))
        if vip_ch_raw and not force:
            high_pro = [m for m in arb_matches if m["profit_pct"] >= vip_min][:3]
            if high_pro:
                try:
                    ch_id = int(vip_ch_raw) if vip_ch_raw.lstrip("-").isdigit() else vip_ch_raw
                    ch_lines = [f"🚨 **VIP HIGH ARBITRAGE ({sport_name})**\n"]
                    for m in high_pro:
                        ch_lines.append(
                            f"🔥 `{m['home']} vs {m['away']}` | 🕒 `{m['match_time_bd']}` | **+{m['profit_pct']}%**"
                        )
                    await context.bot.send_message(
                        chat_id=ch_id, text="\n".join(ch_lines), parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.warning("VIP Channel send failed: %s", e)

        buttons = []
        for match in arb_matches[:25]:
            btn_text = (
                f"🔥 {match['home']} vs {match['away']} | "
                f"🕒 {match['match_time_bd']} (+{match['profit_pct']}%)"
            )
            buttons.append([
                InlineKeyboardButton(
                    btn_text,
                    callback_data=f"detail|{hours}|{match['arb_id']}",
                )
            ])

        buttons.append([
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{sport}_{hours}"),
            InlineKeyboardButton("🔙 Back", callback_data=f"sport_{sport}"),
            InlineKeyboardButton("🏠 Home", callback_data="go_home"),
        ])

        await query.edit_message_text(
            f"✅ **{sport_name} — আগামী {hours} ঘণ্টার Arbitrage ম্যাচ ({len(arb_matches)}টি)**\n\n"
            "👇 **কোন ওয়েবসাইটে কত অডসে Arbitrage হচ্ছে তা স্পষ্ট দেখতে ম্যাচের নামের ওপর ক্লিক করুন:**",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
        return

    if data.startswith("detail|"):
        _, hours_str, arb_id = data.split("|", 2)
        hours = int(hours_str)
        match = get_arbitrage_by_id(arb_id)
        if not match:
            await query.edit_message_text(
                "⚠️ এই ম্যাচটির অডস আপডেট হয়ে গেছে। অনুগ্রহ করে আবার স্ক্যান করুন।",
                reply_markup=sports_keyboard(),
            )
            return
        await query.edit_message_text(
            build_detail_text(match, 1000.0),
            reply_markup=detail_keyboard(arb_id, match["sport"], hours),
            parse_mode="Markdown",
        )
        return

    if data.startswith("calc|"):
        _, arb_id, sport, hours_str, budget_str = data.split("|", 4)
        hours = int(hours_str)
        budget = float(budget_str)
        match = get_arbitrage_by_id(arb_id)
        if not match:
            await query.edit_message_text("⚠️ ম্যাচটি আপডেট হয়ে গেছে।", reply_markup=sports_keyboard())
            return
        await query.edit_message_text(
            build_detail_text(match, budget),
            reply_markup=detail_keyboard(arb_id, sport, hours),
            parse_mode="Markdown",
        )
        return

    if data.startswith("custom_budget|"):
        _, arb_id, sport, hours_str = data.split("|", 3)
        context.user_data["awaiting_input"] = f"budget|{arb_id}|{sport}|{hours_str}"
        await query.message.reply_text(
            "💵 **আপনার বাজেট লিখুন** (যেমন `750` বা `3500`):",
            parse_mode="Markdown",
        )
        return

    # ================= ADMIN PANEL ACTIONS =================
    if data.startswith("adm_"):
        if user_id not in ADMIN_IDS:
            await query.edit_message_text("⛔ শুধুমাত্র অ্যাডমিন এই অংশ ব্যবহার করতে পারবেন।")
            return

        if data == "admin_home":
            context.user_data.pop("awaiting_input", None)
            await query.edit_message_text(
                "⚙️ **Admin Panel**\nসব কিছু বাটনে চাপ দিয়ে অটোমেটিক নিয়ন্ত্রণ করুন:",
                reply_markup=admin_keyboard(),
                parse_mode="Markdown",
            )
            return

        if data == "adm_profit_menu":
            cur_p = float(get_setting("min_profit_pct", "0.1"))
            await query.edit_message_text(
                f"📈 **Min Profit সেটিংস**\nবর্তমান মিনিমাম প্রফিট: **{cur_p}%**",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("➖ 0.2%", callback_data="adm_p_delta|-0.2"),
                        InlineKeyboardButton("➕ 0.2%", callback_data="adm_p_delta|0.2"),
                        InlineKeyboardButton("➕ 0.5%", callback_data="adm_p_delta|0.5"),
                    ],
                    [
                        InlineKeyboardButton("0.1%", callback_data="adm_p_set|0.1"),
                        InlineKeyboardButton("0.5%", callback_data="adm_p_set|0.5"),
                        InlineKeyboardButton("1.0%", callback_data="adm_p_set|1.0"),
                        InlineKeyboardButton("2.0%", callback_data="adm_p_set|2.0"),
                    ],
                    [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_home")],
                ]),
                parse_mode="Markdown",
            )
            return

        if data.startswith("adm_p_delta|"):
            delta = float(data.split("|")[1])
            new_p = max(0.05, round(float(get_setting("min_profit_pct", "0.1")) + delta, 2))
            set_setting("min_profit_pct", str(new_p))
            await query.edit_message_text(
                f"✅ Min Profit আপডেট হয়ে **{new_p}%** হয়েছে।",
                reply_markup=admin_keyboard(),
                parse_mode="Markdown",
            )
            return

        if data.startswith("adm_p_set|"):
            new_p = float(data.split("|")[1])
            set_setting("min_profit_pct", str(new_p))
            await query.edit_message_text(
                f"✅ Min Profit সেট হয়েছে **{new_p}%**।",
                reply_markup=admin_keyboard(),
                parse_mode="Markdown",
            )
            return

        if data == "adm_ttl_menu":
            cur_ttl = int(float(get_setting("cache_ttl_sec", "120")))
            await query.edit_message_text(
                f"⏱ **Cache TTL সেটিংস**\nবর্তমান ক্যাশ সময়: **{cur_ttl} সেকেন্ড**",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("30s", callback_data="adm_ttl_set|30"),
                        InlineKeyboardButton("60s", callback_data="adm_ttl_set|60"),
                        InlineKeyboardButton("120s", callback_data="adm_ttl_set|120"),
                        InlineKeyboardButton("300s", callback_data="adm_ttl_set|300"),
                    ],
                    [InlineKeyboardButton("🗑 সব ক্যাশ ক্লিয়ার করুন", callback_data="adm_clear_cache")],
                    [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_home")],
                ]),
                parse_mode="Markdown",
            )
            return

        if data.startswith("adm_ttl_set|"):
            val = int(data.split("|")[1])
            set_setting("cache_ttl_sec", str(val))
            await query.edit_message_text(
                f"✅ Cache TTL সেট হয়েছে **{val} সেকেন্ড**।",
                reply_markup=admin_keyboard(),
                parse_mode="Markdown",
            )
            return

        if data == "adm_clear_cache":
            clear_all_cache()
            await query.edit_message_text(
                "🗑 **সব API ক্যাশ ক্লিয়ার করা হয়েছে!** পরের স্ক্যানে নতুন লাইভ ডেটা আসবে।",
                reply_markup=admin_keyboard(),
                parse_mode="Markdown",
            )
            return

        if data == "adm_vip_menu":
            vip_min = float(get_setting("vip_min_profit_pct", "2.0"))
            vip_ch = get_setting("vip_channel_id", "") or "Set হয়নি"
            await query.edit_message_text(
                f"👑 **VIP চ্যানেল ও প্রফিট সেটিংস**\n• VIP Min Profit: **+{vip_min}%**\n• VIP Channel: `{vip_ch}`",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("1.0%", callback_data="adm_vmin_set|1.0"),
                        InlineKeyboardButton("2.0%", callback_data="adm_vmin_set|2.0"),
                        InlineKeyboardButton("3.0%", callback_data="adm_vmin_set|3.0"),
                    ],
                    [InlineKeyboardButton("📺 VIP Channel ID সেট করুন", callback_data="adm_set_vip_ch")],
                    [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_home")],
                ]),
                parse_mode="Markdown",
            )
            return

        if data.startswith("adm_vmin_set|"):
            v = float(data.split("|")[1])
            set_setting("vip_min_profit_pct", str(v))
            await query.edit_message_text(
                f"✅ VIP Min Profit সেট হয়েছে **+{v}%**।",
                reply_markup=admin_keyboard(),
                parse_mode="Markdown",
            )
            return

        if data == "adm_set_vip_ch":
            context.user_data["awaiting_input"] = "set_vip_channel"
            await query.message.reply_text(
                "📺 **VIP Channel/Group ID বা @username পাঠান** (যেমন `-1001234567890`):",
                parse_mode="Markdown",
            )
            return

        if data.startswith("adm_users_"):
            page = int(data.split("_")[2])
            users = get_all_users()
            per_page = 6
            total_pages = max(1, (len(users) + per_page - 1) // per_page)
            page = max(0, min(page, total_pages - 1))
            chunk = users[page * per_page : (page + 1) * per_page]

            lines = [f"👥 **মোট ইউজার: {len(users)} জন** (Page {page + 1}/{total_pages})\n"]
            kb_rows = []
            for u in chunk:
                uid = u["user_id"]
                is_v, _ = is_user_vip(uid)
                badge = "👑 VIP" if is_v else "🆓"
                uname = f"@{u['username']}" if u.get("username") else (u.get("full_name") or str(uid))
                lines.append(f"{badge} `{uid}` — {uname}")
                kb_rows.append([
                    InlineKeyboardButton(f"+7d {uid}", callback_data=f"adm_vip_add|{uid}|7|{page}"),
                    InlineKeyboardButton(f"+30d {uid}", callback_data=f"adm_vip_add|{uid}|30|{page}"),
                    InlineKeyboardButton(f"❌ {uid}", callback_data=f"adm_vip_rem|{uid}|{page}"),
                ])

            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"adm_users_{page - 1}"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"adm_users_{page + 1}"))
            if nav:
                kb_rows.append(nav)
            kb_rows.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_home")])

            await query.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(kb_rows),
                parse_mode="Markdown",
            )
            return

        if data.startswith("adm_vip_add|"):
            _, uid_str, days_str, page_str = data.split("|")
            uid = int(uid_str)
            days = int(days_str)
            grant_vip_days(uid, days)
            await query.answer(f"{uid} কে {days} দিন VIP দেওয়া হয়েছে!", show_alert=True)
            query.data = f"adm_users_{page_str}"
            await button_handler(update, context)
            return

        if data.startswith("adm_vip_rem|"):
            _, uid_str, page_str = data.split("|")
            uid = int(uid_str)
            revoke_vip(uid)
            await query.answer(f"{uid} এর VIP বাতিল করা হয়েছে!", show_alert=True)
            query.data = f"adm_users_{page_str}"
            await button_handler(update, context)
            return

        if data == "adm_api_status":
            caches = get_cache_status_list()
            bd_tz = timezone(timedelta(hours=BD_TIMEZONE_OFFSET))
            lines = [
                "📊 **API ক্রেডিট ও ক্যাশ মনিটর**",
                "━━━━━━━━━━━━━━━━━━━━",
                f"🔑 মোট API Key: `{len(ODDS_API_KEYS)}` টি (Active Index: `{odds_feed.key_index}`)",
                f"📉 Credits Used: `{odds_feed.last_credits_used or 'N/A'}`",
                f"🔋 Credits Remaining: `{odds_feed.last_credits_remaining or 'N/A'}`",
                f"📡 Status: `{odds_feed.last_api_status}`",
                "━━━━━━━━━━━━━━━━━━━━",
                "🗂 **স্পোর্টস ক্যাশ স্ট্যাটাস:**\n",
            ]
            if not caches:
                lines.append("• এখনো কোনো ক্যাশ ডেটা নেই।")
            else:
                for c in caches:
                    dt_str = datetime.fromtimestamp(c["updated_at"], tz=bd_tz).strftime("%I:%M:%S %p")
                    lines.append(
                        f"• **{c['sport'].title()}**: `{c['event_count']}` ম্যাচ | `{c['arb_count']}` Arbitrage | আপডেট: `{dt_str}`"
                    )

            await query.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑 Clear Cache", callback_data="adm_clear_cache")],
                    [InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_home")],
                ]),
                parse_mode="Markdown",
            )
            return

        if data == "adm_set_welcome":
            context.user_data["awaiting_input"] = "set_welcome"
            await query.message.reply_text(
                "📝 **নতুন Welcome Message লিখে পাঠান:**",
                parse_mode="Markdown",
            )
            return

        if data == "adm_broadcast_start":
            context.user_data["awaiting_input"] = "broadcast"
            await query.message.reply_text(
                f"📢 **সব ({get_total_users_count()} জন) ইউজারকে পাঠানোর জন্য মেসেজ লিখুন:**",
                parse_mode="Markdown",
            )
            return


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN সেট করা নেই! Environment Variable-এ BOT_TOKEN দিন।")

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))

    logger.info("ArbitrageScanner Pro Bot চালু হয়েছে...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
