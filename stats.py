from __future__ import annotations

import json
from datetime import datetime, timedelta, date
from collections import defaultdict

from constants import STATS_PATH

RANGE_DAYS: dict[str, int] = {"day": 1, "week": 7, "month": 30, "3months": 90}


class StatsTracker:
    """
    Stores claimed-drop events locally so the dashboard can show
    weekly progress, drops per game, and watch-time saved.
    """

    def __init__(self) -> None:
        # each event: {"date": "YYYY-MM-DD", "game": str, "minutes": int}
        # NOTE: json_load/json_save (utils.py) are built for dict-shaped
        # settings files, not plain lists: json_load(path, []) silently
        # turns the default [] into {} via dict(defaults), and merge_json
        # calls .items() on the loaded list and crashes. That crash was
        # swallowed by the try/except around record_claim(), so no claim
        # was ever saved. Load/save the list ourselves instead.
        self._events: list[dict] = self._load()

    @staticmethod
    def _load() -> list[dict]:
        if not STATS_PATH.exists():
            return []
        try:
            with STATS_PATH.open("r", encoding="utf8") as file:
                data = json.load(file)
        except (json.JSONDecodeError, OSError):
            return []
        return data if isinstance(data, list) else []

    def _save(self) -> None:
        with STATS_PATH.open("w", encoding="utf8") as file:
            json.dump(self._events, file, indent=4)

    def record_claim(self, game_name: str, minutes: int) -> None:
        self._events.append({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "game": game_name,
            "minutes": max(0, minutes),
        })
        self._save()

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

    def _range_bounds(self, range_key: str) -> tuple[date, date]:
        today = datetime.now().date()
        if range_key in RANGE_DAYS:
            return today - timedelta(days=RANGE_DAYS[range_key] - 1), today
        # "all": start from the oldest recorded event (or today if there's none yet)
        if self._events:
            oldest = min(
                datetime.strptime(e["date"], "%Y-%m-%d").date() for e in self._events
            )
        else:
            oldest = today
        return oldest, today

    def stats_for_range(self, range_key: str) -> dict:
        """
        Returns totals + a chart-ready series for the requested period
        ("day"/"week"/"month"/"3months"/"all"). Long periods are bucketed
        by week instead of by day, so the chart doesn't turn into a wall
        of unreadable bars.
        """
        start, end = self._range_bounds(range_key)
        span_days = (end - start).days + 1
        bucket_days = 7 if span_days > 60 else 1

        drop_buckets: dict[date, int] = defaultdict(int)
        minute_buckets: dict[date, int] = defaultdict(int)
        game_counts: dict[str, int] = defaultdict(int)
        total_minutes = 0
        total_drops = 0
        for event in self._events:
            day = datetime.strptime(event["date"], "%Y-%m-%d").date()
            if not (start <= day <= end):
                continue
            bucket = start + timedelta(days=((day - start).days // bucket_days) * bucket_days)
            drop_buckets[bucket] += 1
            minute_buckets[bucket] += event["minutes"]
            game_counts[event["game"]] += 1
            total_minutes += event["minutes"]
            total_drops += 1

        series: list[dict] = []
        cursor = start
        while cursor <= end:
            series.append({
                "label": cursor.strftime("%d/%m"),
                "drops": drop_buckets.get(cursor, 0),
                "hours": round(minute_buckets.get(cursor, 0) / 60, 2),
            })
            cursor += timedelta(days=bucket_days)

        per_game = sorted(game_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
        return {
            "range": range_key,
            "bucketed_weekly": bucket_days == 7,
            "series": series,
            "per_game": [{"game": g, "count": c} for g, c in per_game],
            "total_drops": total_drops,
            "hours_saved": round(total_minutes / 60, 2),
        }
