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
)

load_dotenv()

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ODDS_API_KEYS = [
    k.strip() for k in os.getenv("ODDS_API_KEYS", "").split(",") if k.strip()
]
MIN_PROFIT_PERCENT = float(os.getenv("MIN_PROFIT_PERCENT", "0.1"))
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "120"))
REGIONS = os.getenv("REGIONS", "eu,uk").strip()
BD_TIMEZONE_OFFSET = 6
DB_PATH = Path("arbitrage.db")

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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ================= DATABASE =================
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_cache (
            sport TEXT PRIMARY KEY,
            updated_at REAL NOT NULL,
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
    conn.commit()
    conn.close()


def get_cached_odds(sport: str, ttl: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT updated_at, raw_json FROM api_cache WHERE sport = ?", (sport,))
    row = cur.fetchone()
    conn.close()
    if not row or (time.time() - row["updated_at"]) > ttl:
        return None
    return json.loads(row["raw_json"])


def save_cached_odds(sport: str, data: list):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR REPLACE INTO api_cache (sport, updated_at, raw_json)
        VALUES (?, ?, ?)
        """,
        (sport, time.time(), json.dumps(data)),
    )
    conn.commit()
    conn.close()


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


def get_arbitrage_by_id(arb_id: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT data_json FROM arbitrage_matches WHERE arb_id = ?", (arb_id,))
    row = cur.fetchone()
    conn.close()
    return json.loads(row["data_json"]) if row else None


# ================= ODDS FEED =================
class OddsFeedService:
    def __init__(self):
        self.key_index = 0

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
                if resp.status_code in (401, 429):
                    logger.warning("API Key লিমিট শেষ বা ইনভ্যালিড (Status %s)", resp.status_code)
                    self._rotate_key()
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
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
            cached = get_cached_odds(category, CACHE_TTL_SECONDS)
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

        # 1) Match Winner / 1X2 মার্কেট
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
            legs_raw = [
                {"label": f"W1 ({home})", **best_h2h[home]},
            ]
            if has_draw:
                legs_raw.append({"label": "Draw (X)", **best_h2h["Draw"]})
            legs_raw.append({"label": f"W2 ({away})", **best_h2h[away]})

            inv_sum = sum(1 / leg["price"] for leg in legs_raw)
            if inv_sum < 1.0:
                profit_pct = round(((1 / inv_sum) - 1) * 100, 2)
                if profit_pct >= MIN_PROFIT_PERCENT:
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

        # 2) Over / Under (Totals) মার্কেট
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
                    if profit_pct >= MIN_PROFIT_PERCENT:
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


# ================= TELEGRAM BOT UI =================
def home_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Arbitrage ম্যাচ দেখতে এখানে চাপুন", callback_data="menu_sports")]
    ])


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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 **স্বাগতম Arbitrage Scanner বটে!**\n\n"
        "শুধুমাত্র নিশ্চিত **Arbitrage** সুযোগ থাকা ম্যাচগুলোই ফিল্টার করে আপনার সামনে আনা হবে—কোনো সাধারণ ম্যাচ আসবে না।\n\n"
        "শুরু করতে নিচের বাটনে চাপুন 👇"
    )
    await update.message.reply_text(text, reply_markup=home_keyboard(), parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "go_home":
        await query.edit_message_text(
            "👋 **Arbitrage Scanner**\n\nশুধুমাত্র লাইভ Arbitrage ম্যাচগুলো দেখতে নিচের বাটনে চাপুন 👇",
            reply_markup=home_keyboard(),
            parse_mode="Markdown",
        )
        return

    if data == "menu_sports":
        await query.edit_message_text(
            "📂 **একটি ক্যাটাগরি সিলেক্ট করুন:**",
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

        sport = match["sport"]
        lines = [
            "🎯 **ARBITRAGE বিস্তারিত রিপোর্ট**",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🏆 **লিগ:** {match['league']}",
            f"⚔️ **ম্যাচ:** `{match['home']}` **vs** `{match['away']}`",
            f"🕒 **খেলার সময় (BD):** {match['match_time_bd']}",
            f"📊 **মার্কেট:** {match['market_type']}",
            f"💰 **নিশ্চিত প্রফিট:** **+{match['profit_pct']}%**",
            "━━━━━━━━━━━━━━━━━━━━",
            "📌 **কোন ওয়েবসাইটে কী ধরবেন:**\n",
        ]

        sample_total = 1000
        for idx, leg in enumerate(match["legs"], start=1):
            stake_amount = round(sample_total * (leg["stake_pct"] / 100), 2)
            payout = round(stake_amount * leg["odds"], 2)
            lines.append(
                f"**{idx}. সাইট:** 🌐 `{leg['bookmaker']}`\n"
                f"   • **অপশন:** {leg['label']}\n"
                f"   • **অডস:** `{leg['odds']}`\n"
                f"   • **ভাগ:** {leg['stake_pct']}% (১০০০ টাকায় `{stake_amount}` টাকা ধরলে রিটার্ন `{payout}` টাকা)\n"
            )

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("💡 *অডস খুব দ্রুত বদলায়, তাই বেট ধরার আগে দুই সাইটেই লাইভ অডস মিলিয়ে নেবেন।*")

        back_buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔙 ম্যাচ তালিকায় ফিরুন", callback_data=f"time_{sport}_{hours}"),
                InlineKeyboardButton("🏠 Home", callback_data="go_home"),
            ]
        ])

        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=back_buttons,
            parse_mode="Markdown",
        )


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN সেট করা নেই! Environment Variable-এ BOT_TOKEN দিন।")

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("ArbitrageScanner এক-ফাইল বট চালু হয়েছে...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
