from services.odds_feed import odds_feed


def get_football_events(force_refresh: bool = False) -> list[dict]:
    return odds_feed.fetch_odds_for_category("football", force_refresh=force_refresh)
  
