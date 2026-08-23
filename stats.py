from __future__ import annotations

from datetime import datetime, timedelta
from collections import defaultdict

from utils import json_load, json_save
from constants import STATS_PATH


class StatsTracker:
    """
    Stores claimed-drop events locally so the dashboard can show
    weekly progress, drops per game, and watch-time saved.
    """

    def __init__(self) -> None:
        # each event: {"date": "YYYY-MM-DD", "game": str, "minutes": int}
        self._events: list[dict] = json_load(STATS_PATH, [])  # type: ignore[assignment]

    def record_claim(self, game_name: str, minutes: int) -> None:
        self._events.append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "game": game_name,
            "minutes": max(0, minutes),
        })
        json_save(STATS_PATH, self._events)

    def weekly_progress(self) -> list[tuple[str, int]]:
        # returns [(day_label, claim_count), ...] for the last 7 days, oldest first
        today = datetime.now().date()
        counts: dict[str, int] = defaultdict(int)
        for event in self._events:
            counts[event["date"]] += 1
        result: list[tuple[str, int]] = []
        for i in range(6, -1, -1):
            day = today - timedelta(days=i)
            key = day.strftime("%Y-%m-%d")
            result.append((day.strftime("%a"), counts.get(key, 0)))
        return result

    def drops_per_game(self, top_n: int = 8) -> list[tuple[str, int]]:
        counts: dict[str, int] = defaultdict(int)
        for event in self._events:
            counts[event["game"]] += 1
        return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:top_n]

    def total_hours_saved(self) -> float:
        # "hours saved" = time we didn't have to manually watch to earn these drops
        return sum(event["minutes"] for event in self._events) / 60

    def total_drops_claimed(self) -> int:
        return len(self._events)
