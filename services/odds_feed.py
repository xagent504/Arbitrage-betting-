import logging
import requests
from config import ODDS_API_KEYS, REGIONS, CACHE_TTL_SECONDS
from database.database import get_cached_odds, save_cached_odds

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"

SPORT_PREFIX_MAP = {
    "football": "soccer_",
    "basketball": "basketball_",
    "tennis": "tennis_",
}


class OddsFeedService:
    def __init__(self):
        self.key_index = 0

    def _get_current_key(self) -> str:
        if not ODDS_API_KEYS:
            raise ValueError("ODDS_API_KEYS সেট করা নেই! .env বা Environment Variable চেক করুন।")
        return ODDS_API_KEYS[self.key_index % len(ODDS_API_KEYS)]

    def _rotate_key(self):
        if len(ODDS_API_KEYS) > 1:
            self.key_index = (self.key_index + 1) % len(ODDS_API_KEYS)
            logger.warning("API Key রোটেট করা হয়েছে (নতুন ইনডেক্স: %s)", self.key_index)

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
        # বড় লিগগুলো আগে রাখার জন্য সর্বোচ্চ ১০টি সক্রিয় টুর্নামেন্ট নেওয়া হচ্ছে (API ক্রেডিট সাশ্রয়ের জন্য)
        return active_keys[:10]

    def fetch_odds_for_category(self, category: str, force_refresh: bool = False) -> list[dict]:
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
  
