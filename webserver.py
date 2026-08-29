from __future__ import annotations

import json
import time
import socket
import secrets
import logging
from pathlib import Path
from datetime import datetime, timedelta
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from aiohttp import web

from constants import PriorityMode, State
from utils import resource_path

if TYPE_CHECKING:
    from twitch import Twitch

logger = logging.getLogger("TwitchDrops")


def new_token() -> str:
    # used as the secret part of the share link, so it needs to be hard to guess:
    # secrets.token_hex is CSPRNG-backed, unlike the random-module nonce used elsewhere
    return secrets.token_hex(20)


def local_ip() -> str:
    # best-effort LAN IP, used to build a shareable link. Doesn't actually send anything;
    # a UDP socket's local address is resolved without any packet leaving the machine.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def public_ip() -> str | None:
    # best-effort public IP lookup, only used when the user opts into a public link. This is
    # just for building a shareable URL - it doesn't open any port or expose anything by
    # itself; the user's router still needs to forward the configured port for a public link
    # to actually work. A short timeout keeps this from ever hanging the UI.
    import aiohttp
    try:
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://api.ipify.org") as resp:
                if resp.status == 200:
                    text = (await resp.text()).strip()
                    if text:
                        return text
    except Exception:
        pass
    return None


PRIORITY_MODE_LABELS: dict[int, str] = {
    PriorityMode.PRIORITY_ONLY.value: "Priority list only",
    PriorityMode.PRIORITY_ONLY_CONTINUE.value: "Priority list only, then continue with the rest",
    PriorityMode.ENDING_SOONEST.value: "Ending soonest",
    PriorityMode.PRIORITY_ENDING_SOONEST.value: "Priority list first, then ending soonest",
    PriorityMode.LOW_AVBL_FIRST.value: "Low availability first",
    PriorityMode.PRIORITY_LOW_AVBL_FIRST.value: "Priority list first, then low availability",
}


class _RateLimiter:
    """
    Small fixed-window, per-IP rate limiter, applied as middleware.

    The whole app - the mining loop included - runs on a single asyncio event loop, so if
    this dashboard ends up reachable from the internet, a scanner or an impatient client
    hammering it could add latency to everything else the loop is doing. This keeps each
    visitor to a generous but bounded request rate, and evicts old entries so tracking many
    distinct IPs (random internet scans, if the port ends up exposed) can't grow memory
    unbounded.
    """

    WINDOW_SECONDS = 10.0
    MAX_REQUESTS = 40  # generous: the page polls every 4s, this allows many browser tabs too
    MAX_TRACKED_IPS = 500

    def __init__(self) -> None:
        # ip -> (window_start, count); OrderedDict so the oldest entry is evictable in O(1)
        self._hits: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def allow(self, ip: str) -> bool:
        now = time.monotonic()
        entry = self._hits.get(ip)
        if entry is None or now - entry[0] >= self.WINDOW_SECONDS:
            self._hits[ip] = (now, 1)
            self._hits.move_to_end(ip)
            if len(self._hits) > self.MAX_TRACKED_IPS:
                self._hits.popitem(last=False)
            return True
        window_start, count = entry
        if count >= self.MAX_REQUESTS:
            return False
        self._hits[ip] = (window_start, count + 1)
        self._hits.move_to_end(ip)
        return True


class WebDashboard:
    """
    Optional, local HTTP server exposing a small view/control dashboard, meant to be reached
    from other devices on the same network (or a remote one, through port forwarding/a tunnel,
    at the user's own risk). Disabled by default.

    Every route is namespaced under the current secret token, so the link itself is what
    grants viewing access - there's no separate login step for that. Control actions
    (pause/resume, changing the priority mode) are only exposed at all if the user opted into
    "view and control" mode, and can additionally be gated behind a password.
    """

    def __init__(self, twitch: Twitch) -> None:
        self._twitch: Twitch = twitch
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._limiter = _RateLimiter()
        # ip -> last-seen monotonic timestamp, used to estimate how many distinct visitors
        # currently have the dashboard open (an entry expires after VIEWER_TTL of silence,
        # i.e. after ~2 missed polls), without keeping any persistent connection open
        self._viewers: OrderedDict[str, float] = OrderedDict()
        self._icon_cache: dict[str, bytes] = {}

    @property
    def running(self) -> bool:
        return self._site is not None

    async def start(self) -> None:
        if self.running:
            return
        settings = self._twitch.settings
        if not settings.web_server_token:
            settings.web_server_token = new_token()
        token = settings.web_server_token
        # cap request body size (only the priority-mode POST has a body, and it's tiny) and
        # register the rate-limit middleware ahead of routing, so throttled requests never
        # reach the (slightly heavier) state-building code below
        app = web.Application(client_max_size=1024 * 8, middlewares=[self._rate_limit_middleware])
        routes = [
            web.get(f"/{token}", self._handle_index),
            web.get(f"/{token}/", self._handle_index),
            web.get(f"/{token}/favicon.ico", self._handle_favicon),
            web.get(f"/{token}/api/state", self._handle_state),
            web.get(f"/{token}/api/campaigns", self._handle_campaigns),
        ]
        if settings.web_server_allow_control:
            routes += [
                web.post(f"/{token}/api/pause", self._handle_pause),
                web.post(f"/{token}/api/pause_for", self._handle_pause_for),
                web.post(f"/{token}/api/resume", self._handle_resume),
                web.post(f"/{token}/api/priority_mode", self._handle_priority_mode),
                web.post(f"/{token}/api/priority/add", self._handle_priority_add),
                web.post(f"/{token}/api/priority/remove", self._handle_priority_remove),
                web.post(f"/{token}/api/priority/move", self._handle_priority_move),
                web.post(f"/{token}/api/exclude/add", self._handle_exclude_add),
                web.post(f"/{token}/api/exclude/remove", self._handle_exclude_remove),
                web.post(f"/{token}/api/reload", self._handle_reload),
            ]
        app.add_routes(routes)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", settings.web_server_port)
        try:
            await self._site.start()
            logger.info(f"Web dashboard started on port {settings.web_server_port}")
        except OSError:
            logger.exception("Failed to start the web dashboard (port already in use?)")
            await self.stop()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    # -- middleware --

    @web.middleware
    async def _rate_limit_middleware(self, request: web.Request, handler):
        ip = request.remote or "unknown"
        if not self._limiter.allow(ip):
            return web.json_response({"error": "rate limited"}, status=429)
        response = await handler(request)
        # defense-in-depth headers: this page is never meant to be embedded elsewhere,
        # never contains third-party scripts, and its API responses aren't meant to be
        # sniffed as anything other than what they declare themselves to be
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' https: data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
        )
        return response

    # -- viewer tracking --

    VIEWER_TTL = 9.0  # seconds; a bit over 2x the 4s poll interval used by the page

    def _touch_viewer(self, request: web.Request) -> None:
        ip = request.remote or "unknown"
        now = time.monotonic()
        self._viewers[ip] = now
        self._viewers.move_to_end(ip)
        # sweep from the oldest entry; the dict is ordered by last-touch time, so we can
        # stop as soon as we hit one that's still fresh instead of scanning everything
        while self._viewers:
            oldest_ip, last_seen = next(iter(self._viewers.items()))
            if now - last_seen > self.VIEWER_TTL:
                del self._viewers[oldest_ip]
            else:
                break

    def _viewer_count(self) -> int:
        return len(self._viewers)

    # -- auth helper --

    def _password_ok(self, request: web.Request) -> bool:
        password = self._twitch.settings.web_server_password
        if not password:
            return True
        supplied = request.headers.get("X-Dashboard-Password", "")
        return secrets.compare_digest(supplied, password)

    # -- campaigns / drops with images --

    def _campaigns_list(self) -> list[dict[str, Any]]:
        # mirrors the desktop Inventory tab (image + reward thumbnails + progress), capped
        # to keep the payload light: images are linked directly to Twitch's CDN, never
        # proxied or re-encoded by this server, so serving this costs us almost nothing
        out: list[dict[str, Any]] = []
        for campaign in self._twitch.inventory:
            if campaign.expired or not campaign.eligible:
                continue
            drops = []
            for drop in campaign.drops:
                reward_image = drop.benefits[0].image_url if drop.benefits else None
                drops.append({
                    "rewards": drop.rewards_text(),
                    "image_url": reward_image,
                    "progress": round(drop.progress, 4),
                    "claimed": drop.is_claimed,
                })
                if len(drops) >= 12:
                    break
            out.append({
                "game": campaign.game.name,
                "image_url": campaign.image_url,
                "name": campaign.name,
                "active": campaign.active,
                "progress": round(campaign.progress, 4),
                "claimed_drops": campaign.claimed_drops,
                "total_drops": campaign.total_drops,
                "starts_at": campaign.starts_at.isoformat(),
                "drops": drops,
            })
            if len(out) >= 30:
                break
        out.sort(key=lambda c: (not c["active"], -c["progress"]))
        return out

    # -- state snapshot --

    def _state_dict(self) -> dict[str, Any]:
        twitch = self._twitch
        settings = twitch.settings
        drop = twitch.gui.progress._drop
        current_drop: dict[str, Any] | None = None
        if drop is not None:
            campaign = drop.campaign
            reward_image = drop.benefits[0].image_url if drop.benefits else None
            current_drop = {
                "game": campaign.game.name,
                "game_image": campaign.image_url,
                "campaign": campaign.name,
                "rewards": drop.rewards_text(),
                "reward_image": reward_image,
                "drop_progress": round(drop.progress, 4),
                "campaign_progress": round(campaign.progress, 4),
                "claimed_drops": campaign.claimed_drops,
                "total_drops": campaign.total_drops,
                "drop_remaining_minutes": drop.remaining_minutes,
                "campaign_remaining_minutes": campaign.remaining_minutes,
            }
        watching = twitch.watching_channel.get_with_default(None)
        watching_channel: dict[str, Any] | None = None
        if watching is not None:
            watching_channel = {
                "name": watching.name,
                "game": watching.game.name if watching.game is not None else None,
                "viewers": watching.viewers,
            }
        # traffic-light style status, mirroring the desktop tray icon at a glance:
        # green when actively mining, amber when paused (intentional), red when idle
        # (running, unpaused, but nothing is currently being watched)
        if twitch.paused:
            status = "paused"
        elif watching is not None:
            status = "mining"
        else:
            status = "idle"
        priority_mode = settings.priority_mode
        priority_value = (
            priority_mode.value if isinstance(priority_mode, PriorityMode) else int(priority_mode)
        )
        # best-effort box art per game name, so the "drops per game" ranking can show art too
        game_images = {c.game.name: c.image_url for c in twitch.inventory}
        per_game = [
            {"game": name, "count": count, "image_url": game_images.get(name)}
            for name, count in twitch.stats.drops_per_game()
        ]
        available_games = sorted({c.game.name for c in twitch.inventory})
        return {
            "app": {"name": "DropStream", "version": self._version()},
            "control_enabled": settings.web_server_allow_control,
            "show_viewers": settings.web_server_show_viewers,
            "viewer_count": self._viewer_count() if settings.web_server_show_viewers else None,
            "password_required": bool(
                settings.web_server_allow_control and settings.web_server_password
            ),
            "status": status,
            "paused": twitch.paused,
            "resume_at": (
                twitch._resume_at.isoformat() if twitch._resume_at is not None else None
            ),
            "watching_channel": watching_channel,
            "current_drop": current_drop,
            "priority_mode": {
                "value": priority_value,
                "label": PRIORITY_MODE_LABELS.get(priority_value, "Unknown"),
            },
            "priority_list": list(settings.priority),
            "exclude_list": sorted(settings.exclude),
            "available_games": available_games,
            "stats": {
                "total_drops": twitch.stats.total_drops_claimed(),
                "hours_saved": round(twitch.stats.total_hours_saved(), 2),
                "weekly": twitch.stats.weekly_progress(),
                "per_game": per_game,
            },
        }

    @staticmethod
    def _version() -> str:
        from version import __version__
        return __version__

    # -- handlers --

    async def _handle_index(self, request: web.Request) -> web.Response:
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    # icon shown in the browser tab; reuses the same status-coded .ico files as the
    # desktop tray icon, so the tab tells you what's happening at a glance too
    _FAVICON_BY_STATUS = {
        "mining": "active.ico",
        "paused": "maint.ico",
        "idle": "idle.ico",
    }

    async def _handle_favicon(self, request: web.Request) -> web.Response:
        twitch = self._twitch
        if twitch.paused:
            status = "paused"
        elif twitch.watching_channel.get_with_default(None) is not None:
            status = "mining"
        else:
            status = "idle"
        filename = self._FAVICON_BY_STATUS.get(status, "idle.ico")
        data = self._icon_cache.get(filename)
        if data is None:
            try:
                data = Path(resource_path(f"icons/{filename}")).read_bytes()
            except OSError:
                data = b""
            self._icon_cache[filename] = data
        return web.Response(
            body=data, content_type="image/x-icon",
            headers={"Cache-Control": "no-cache"},  # tab icon must follow live status
        )

    async def _handle_state(self, request: web.Request) -> web.Response:
        self._touch_viewer(request)
        return web.json_response(self._state_dict())

    async def _handle_campaigns(self, request: web.Request) -> web.Response:
        return web.json_response({"campaigns": self._campaigns_list()})

    async def _handle_pause(self, request: web.Request) -> web.Response:
        if not self._password_ok(request):
            return web.json_response({"error": "wrong password"}, status=401)
        self._twitch.pause()
        return web.json_response(self._state_dict())

    async def _handle_resume(self, request: web.Request) -> web.Response:
        if not self._password_ok(request):
            return web.json_response({"error": "wrong password"}, status=401)
        self._twitch.resume()
        return web.json_response(self._state_dict())

    async def _handle_pause_for(self, request: web.Request) -> web.Response:
        if not self._password_ok(request):
            return web.json_response({"error": "wrong password"}, status=401)
        try:
            body = await request.json()
            minutes = float(body["minutes"])
        except (json.JSONDecodeError, KeyError, ValueError):
            return web.json_response({"error": "invalid duration"}, status=400)
        if not (0 < minutes <= 24 * 60):
            return web.json_response({"error": "duration out of range"}, status=400)
        self._twitch.pause_until(datetime.now() + timedelta(minutes=minutes))
        if self._twitch.gui is not None:
            self._twitch.gui.settings.sync_from_settings()
        return web.json_response(self._state_dict())

    async def _handle_priority_mode(self, request: web.Request) -> web.Response:
        if not self._password_ok(request):
            return web.json_response({"error": "wrong password"}, status=401)
        try:
            body = await request.json()
            mode_value = int(body["mode"])
            self._twitch.settings.priority_mode = PriorityMode(mode_value)
        except (json.JSONDecodeError, KeyError, ValueError):
            return web.json_response({"error": "invalid mode"}, status=400)
        # keep the desktop GUI's settings panel in sync with changes made here
        if self._twitch.gui is not None:
            self._twitch.gui.settings.sync_from_settings()
        return web.json_response(self._state_dict())

    async def _handle_priority_add(self, request: web.Request) -> web.Response:
        if not self._password_ok(request):
            return web.json_response({"error": "wrong password"}, status=401)
        try:
            body = await request.json()
            game_name = str(body["game"]).strip()
        except (json.JSONDecodeError, KeyError):
            return web.json_response({"error": "invalid game"}, status=400)
        if not game_name:
            return web.json_response({"error": "invalid game"}, status=400)
        settings = self._twitch.settings
        if game_name not in settings.priority:
            settings.priority.append(game_name)
            settings.alter()
        # keep the desktop GUI's settings panel in sync with changes made here
        if self._twitch.gui is not None:
            self._twitch.gui.settings.sync_from_settings()
        return web.json_response(self._state_dict())

    async def _handle_priority_remove(self, request: web.Request) -> web.Response:
        if not self._password_ok(request):
            return web.json_response({"error": "wrong password"}, status=401)
        try:
            body = await request.json()
            game_name = str(body["game"])
        except (json.JSONDecodeError, KeyError):
            return web.json_response({"error": "invalid game"}, status=400)
        settings = self._twitch.settings
        if game_name in settings.priority:
            settings.priority.remove(game_name)
            settings.alter()
        # keep the desktop GUI's settings panel in sync with changes made here
        if self._twitch.gui is not None:
            self._twitch.gui.settings.sync_from_settings()
        return web.json_response(self._state_dict())

    async def _handle_priority_move(self, request: web.Request) -> web.Response:
        if not self._password_ok(request):
            return web.json_response({"error": "wrong password"}, status=401)
        try:
            body = await request.json()
            game_name = str(body["game"])
            direction = int(body["direction"])  # -1 = up (earlier), +1 = down (later)
        except (json.JSONDecodeError, KeyError, ValueError):
            return web.json_response({"error": "invalid request"}, status=400)
        settings = self._twitch.settings
        priority = settings.priority
        if game_name in priority and direction in (-1, 1):
            idx = priority.index(game_name)
            new_idx = idx + direction
            if 0 <= new_idx < len(priority):
                priority[idx], priority[new_idx] = priority[new_idx], priority[idx]
                settings.alter()
        # keep the desktop GUI's settings panel in sync with changes made here
        if self._twitch.gui is not None:
            self._twitch.gui.settings.sync_from_settings()
        return web.json_response(self._state_dict())

    async def _handle_exclude_add(self, request: web.Request) -> web.Response:
        if not self._password_ok(request):
            return web.json_response({"error": "wrong password"}, status=401)
        try:
            body = await request.json()
            game_name = str(body["game"]).strip()
        except (json.JSONDecodeError, KeyError):
            return web.json_response({"error": "invalid game"}, status=400)
        if not game_name:
            return web.json_response({"error": "invalid game"}, status=400)
        settings = self._twitch.settings
        if game_name not in settings.exclude:
            settings.exclude.add(game_name)
            settings.alter()
        # keep the desktop GUI's settings panel in sync with changes made here
        if self._twitch.gui is not None:
            self._twitch.gui.settings.sync_from_settings()
        return web.json_response(self._state_dict())

    async def _handle_exclude_remove(self, request: web.Request) -> web.Response:
        if not self._password_ok(request):
            return web.json_response({"error": "wrong password"}, status=401)
        try:
            body = await request.json()
            game_name = str(body["game"])
        except (json.JSONDecodeError, KeyError):
            return web.json_response({"error": "invalid game"}, status=400)
        settings = self._twitch.settings
        if game_name in settings.exclude:
            settings.exclude.discard(game_name)
            settings.alter()
        # keep the desktop GUI's settings panel in sync with changes made here
        if self._twitch.gui is not None:
            self._twitch.gui.settings.sync_from_settings()
        return web.json_response(self._state_dict())

    async def _handle_reload(self, request: web.Request) -> web.Response:
        if not self._password_ok(request):
            return web.json_response({"error": "wrong password"}, status=401)
        self._twitch.change_state(State.INVENTORY_FETCH)
        return web.json_response(self._state_dict())


# Single-file dashboard: plain HTML/CSS/JS, no build step, no external requests (drop and
# box art images are linked straight to Twitch's CDN, never fetched or re-hosted by us).
# Kept intentionally simple (polling, not websockets) to keep the server dependency-free.
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" id="favicon" href="favicon.ico" type="image/x-icon">
<meta name="theme-color" id="theme-color-meta" content="#9147ff">
<title>DropStream</title>
<style>
  :root {
    --bg: #0e0e10; --card: #18181b; --card2: #202024; --border: #2f2f35; --fg: #efeff1;
    --dim: #adadb8; --accent: #9147ff; --green: #2ecc71; --amber: #e0a800; --red: #e05252;
    --input-bg: #303034;
  }
  html[data-theme="light"] {
    --bg: #f2f2f5; --card: #ffffff; --card2: #f5f5f8; --border: #dcdce2; --fg: #18181b;
    --dim: #5c5c66; --accent: #772ce8; --green: #1a9c4a; --amber: #b3790a; --red: #c93a3a;
    --input-bg: #eaeaef;
  }
  @media (prefers-color-scheme: light) {
    html:not([data-theme]) {
      --bg: #f2f2f5; --card: #ffffff; --card2: #f5f5f8; --border: #dcdce2; --fg: #18181b;
      --dim: #5c5c66; --accent: #772ce8; --green: #1a9c4a; --amber: #b3790a; --red: #c93a3a;
      --input-bg: #eaeaef;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 20px; background: var(--bg); color: var(--fg);
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
  }
  .wrap { max-width: 860px; margin: 0 auto; }
  .top-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
  h1 { font-size: 20px; margin: 0 0 4px; display: flex; align-items: center; gap: 8px; }
  .logo { display: inline-flex; }
  .logo img { width: 22px; height: 22px; display: block; }
  .sub { color: var(--dim); font-size: 13px; margin-bottom: 14px; }
  .top-controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .badge {
    display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px;
    background: var(--input-bg); color: var(--dim); margin-left: 8px; vertical-align: middle;
  }
  select#lang-select {
    background: var(--input-bg); color: var(--fg); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 8px; font-size: 12px;
  }
  .theme-switch { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .theme-switch button {
    background: var(--input-bg); color: var(--dim); border: none; padding: 6px 10px;
    font-size: 12px; line-height: 1.4; white-space: nowrap; cursor: pointer; border-radius: 0;
  }
  .theme-switch button.active { background: var(--accent); color: #fff; }
  .tab-bar { display: flex; gap: 4px; margin: 16px 0; border-bottom: 1px solid var(--border); }
  .tab-btn {
    background: none; border: none; color: var(--dim); padding: 8px 14px; font-size: 13px;
    cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -1px;
  }
  .tab-btn.active { color: var(--fg); border-bottom-color: var(--accent); }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px; margin-bottom: 14px;
  }
  .row { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  .label { color: var(--dim); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  .value { font-size: 15px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 8px; }
  .dot.mining { background: var(--green); }
  .dot.paused { background: var(--amber); }
  .dot.idle { background: var(--red); }
  .bar { background: var(--input-bg); border-radius: 6px; overflow: hidden; height: 10px; margin-top: 6px; }
  .bar > div { background: var(--accent); height: 100%; transition: width .3s; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  button.action {
    background: var(--accent); color: #fff; border: none; border-radius: 6px;
    padding: 10px 16px; font-size: 13px; line-height: 1.4; white-space: nowrap; cursor: pointer;
  }
  button.action:hover { opacity: .9; }
  button.action:disabled { opacity: .4; cursor: not-allowed; }
  button.action.secondary { background: var(--input-bg); color: var(--fg); }
  select, input[type=password], input[type=text] {
    background: var(--input-bg); color: var(--fg); border: 1px solid var(--border);
    border-radius: 6px; padding: 8px; font-size: 13px; width: 100%;
  }
  ul.chips { list-style: none; padding: 0; margin: 8px 0 0; display: flex; flex-wrap: wrap; gap: 6px; }
  ul.chips li { background: var(--input-bg); padding: 4px 10px; border-radius: 999px; font-size: 12px; }
  .muted { color: var(--dim); font-size: 13px; }
  .err { color: var(--red); font-size: 13px; margin-top: 8px; display: none; }
  .inline { display: flex; gap: 8px; }
  .inline input { flex: 1; }
  .drop-current { display: flex; gap: 12px; align-items: flex-start; }
  .drop-current img { width: 56px; height: 56px; border-radius: 8px; object-fit: cover; background: var(--input-bg); }
  .time-row { display: flex; gap: 16px; margin-top: 6px; flex-wrap: wrap; }
  .time-chip { font-size: 12px; color: var(--dim); }
  .time-chip b { color: var(--fg); font-weight: 600; }
  .rank-list { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
  .rank-item { display: flex; align-items: center; gap: 10px; }
  .rank-num {
    width: 22px; height: 22px; border-radius: 50%; background: var(--input-bg); color: var(--dim);
    font-size: 11px; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  }
  .rank-item img { width: 32px; height: 32px; border-radius: 6px; object-fit: cover; background: var(--input-bg); flex-shrink: 0; }
  .rank-name { flex: 1; font-size: 13px; }
  .rank-count { font-size: 12px; color: var(--dim); }
  .campaign-list { display: flex; flex-direction: column; gap: 12px; margin-top: 8px; }
  .campaign-card { display: flex; gap: 12px; padding: 10px; background: var(--card2); border-radius: 8px; }
  .campaign-card img.boxart { width: 48px; height: 64px; border-radius: 6px; object-fit: cover; background: var(--input-bg); flex-shrink: 0; }
  .campaign-body { flex: 1; min-width: 0; }
  .campaign-title { font-size: 14px; margin-bottom: 2px; }
  .campaign-game { font-size: 12px; color: var(--dim); margin-bottom: 6px; }
  .drop-thumbs { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .drop-thumb { position: relative; width: 34px; height: 34px; }
  .drop-thumb img {
    width: 34px; height: 34px; border-radius: 6px; object-fit: cover; background: var(--input-bg);
    display: block;
  }
  .drop-thumb.claimed img { opacity: .45; }
  .drop-thumb .check {
    position: absolute; top: -4px; right: -4px; width: 14px; height: 14px; border-radius: 50%;
    background: var(--green); color: #08240f; font-size: 10px; display: flex;
    align-items: center; justify-content: center; font-weight: bold;
  }
  .edit-list { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  .edit-item {
    display: flex; align-items: center; gap: 8px; background: var(--card2); border-radius: 6px;
    padding: 6px 8px;
  }
  .edit-item .name { flex: 1; font-size: 13px; }
  .icon-btn {
    background: var(--input-bg); color: var(--dim); border: none; border-radius: 5px;
    width: 26px; height: 26px; font-size: 12px; cursor: pointer; flex-shrink: 0;
  }
  .icon-btn:hover { color: var(--fg); }
  .icon-btn:disabled { opacity: .3; cursor: not-allowed; }
  .add-row { display: flex; gap: 8px; margin-top: 10px; }
  .add-row input { flex: 1; }
  .notice { background: var(--card2); border-radius: 8px; padding: 12px; font-size: 13px; color: var(--dim); }
  .campaign-toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
  .campaign-toolbar input[type=text] { flex: 1; }
  .campaign-toolbar select { width: auto; flex: 0 0 auto; }
</style>
</head>
<body>
<div class="wrap">
  <div class="top-row">
    <div>
      <h1><span class="logo"><img id="app-logo" src="favicon.ico" alt=""></span> DropStream <span class="badge" id="mode-badge">-</span><span class="badge" id="viewer-badge" style="display:none"></span></h1>
      <div class="sub" data-i18n="subtitle"></div>
    </div>
    <div class="top-controls">
      <div class="theme-switch">
        <button id="theme-light" data-i18n="theme_light"></button>
        <button id="theme-dark" data-i18n="theme_dark"></button>
        <button id="theme-auto" data-i18n="theme_auto"></button>
      </div>
      <select id="lang-select"></select>
    </div>
  </div>

  <div class="card" id="password-card" style="display:none">
    <div class="label" data-i18n="password_title"></div>
    <div class="inline" style="margin-top:8px">
      <input type="password" id="password-input">
      <button class="action" id="unlock-btn" data-i18n="unlock"></button>
    </div>
    <div class="err" id="password-err" data-i18n="wrong_password"></div>
  </div>

  <div class="tab-bar">
    <button class="tab-btn active" data-tab="dashboard" data-i18n="tab_dashboard"></button>
    <button class="tab-btn" data-tab="campaigns" data-i18n="tab_campaigns"></button>
    <button class="tab-btn" data-tab="control" data-i18n="tab_control"></button>
  </div>

  <div class="tab-panel active" id="tab-dashboard">
    <div class="card">
      <div class="row">
        <div><span class="dot" id="status-dot"></span><span class="value" id="status-text">...</span></div>
        <button class="action" id="toggle-btn"></button>
      </div>
      <div class="row" style="margin-top:8px">
        <select id="pause-timer-select">
          <option value="15">Pause 15 min</option>
          <option value="30">Pause 30 min</option>
          <option value="60">Pause 1 h</option>
          <option value="180">Pause 3 h</option>
          <option value="480">Pause 8 h</option>
        </select>
        <button class="action secondary" id="pause-timer-btn">Set timer</button>
      </div>
      <div class="muted" id="resume-at-info" style="margin-top:6px"></div>
    </div>

    <div class="card" id="drop-card" style="display:none">
      <div class="label" data-i18n="currently_mining"></div>
      <div class="drop-current" style="margin-top:8px">
        <img id="drop-image" src="" alt="">
        <div style="flex:1">
          <div class="value" id="drop-game" style="font-size:18px"></div>
          <div class="muted" id="drop-rewards"></div>
        </div>
      </div>
      <div class="label" style="margin-top:12px"><span data-i18n="drop_progress"></span> <span id="drop-pct"></span></div>
      <div class="bar"><div id="drop-bar" style="width:0%"></div></div>
      <div class="label" style="margin-top:12px"><span data-i18n="campaign_progress"></span> <span id="campaign-pct"></span></div>
      <div class="bar"><div id="campaign-bar" style="width:0%"></div></div>
      <div class="time-row">
        <div class="time-chip" id="drop-remaining"></div>
        <div class="time-chip" id="campaign-remaining"></div>
      </div>
    </div>

    <div class="card" id="channel-card" style="display:none">
      <div class="label" data-i18n="watching"></div>
      <div class="value" id="channel-name"></div>
    </div>

    <div class="grid">
      <div class="card">
        <div class="label" data-i18n="total_drops"></div>
        <div class="value" id="stat-total" style="font-size:22px">-</div>
      </div>
      <div class="card">
        <div class="label" data-i18n="hours_saved"></div>
        <div class="value" id="stat-hours" style="font-size:22px">-</div>
      </div>
    </div>

    <div class="card">
      <div class="label" data-i18n="drops_per_game_title"></div>
      <ul class="rank-list" id="rank-list"></ul>
    </div>
  </div>

  <div class="tab-panel" id="tab-campaigns">
    <div class="card">
      <div class="campaign-toolbar">
        <input type="text" id="campaign-search">
        <select id="campaign-sort">
          <option value="default" data-i18n="sort_default"></option>
          <option value="recent" data-i18n="sort_recent"></option>
          <option value="progress" data-i18n="sort_progress"></option>
        </select>
      </div>
      <div class="label" data-i18n="campaigns_title"></div>
      <div class="campaign-list" id="campaign-list"></div>
      <div class="muted" id="no-campaigns" data-i18n="no_campaigns" style="display:none"></div>
    </div>
  </div>

  <div class="tab-panel" id="tab-control">
    <div class="card" id="control-locked" style="display:none">
      <div class="notice" data-i18n="locked_notice"></div>
    </div>
    <div id="control-body">
      <div class="card">
        <div class="label" data-i18n="priority_mode"></div>
        <select id="priority-mode"></select>
      </div>
      <div class="card">
        <div class="label" data-i18n="priority_list"></div>
        <ul class="edit-list" id="priority-edit-list"></ul>
        <div class="muted" id="priority-empty" data-i18n="empty_priority" style="display:none"></div>
        <div class="add-row">
          <input type="text" id="priority-input" list="game-options">
          <button class="action" id="priority-add-btn" data-i18n="add"></button>
        </div>
      </div>
      <div class="card">
        <div class="label" data-i18n="exclude_list"></div>
        <ul class="edit-list" id="exclude-edit-list"></ul>
        <div class="muted" id="exclude-empty" data-i18n="empty_exclude" style="display:none"></div>
        <div class="add-row">
          <input type="text" id="exclude-input" list="game-options">
          <button class="action" id="exclude-add-btn" data-i18n="add"></button>
        </div>
      </div>
      <div class="card">
        <button class="action secondary" id="reload-btn" data-i18n="reload" style="width:100%"></button>
      </div>
    </div>
  </div>

  <datalist id="game-options"></datalist>

  <div class="err" id="error-box" data-i18n="connection_lost"></div>
</div>

<script>
const I18N = {
  "en": {
    "subtitle": "Remote dashboard for this instance.",
    "mode_view": "View only",
    "mode_control": "View & control",
    "mining": "Mining",
    "paused": "Paused",
    "pause": "Pause",
    "resume": "Resume",
    "password_title": "Control password",
    "unlock": "Unlock",
    "wrong_password": "Incorrect password.",
    "currently_mining": "Currently mining",
    "drop_progress": "Drop progress",
    "campaign_progress": "Campaign progress",
    "watching": "Watching",
    "viewers": "viewers",
    "total_drops": "Total drops claimed",
    "hours_saved": "Watch hours saved",
    "priority_mode": "Priority mode",
    "priority_list": "Priority list",
    "drops_per_game_title": "Drops per game",
    "campaigns_title": "Drop campaigns",
    "campaign_search_placeholder": "Search campaigns or games...",
    "sort_default": "Default",
    "sort_recent": "Most recent",
    "sort_progress": "Progress",
    "claimed": "Claimed",
    "no_campaigns": "No campaigns to show yet.",
    "connection_lost": "Connection lost - retrying...",
    "modes": [
      "Priority list only",
      "Priority list only, then continue with the rest",
      "Ending soonest",
      "Priority list first, then ending soonest",
      "Low availability first",
      "Priority list first, then low availability"
    ],
    "tab_dashboard": "Dashboard",
    "tab_campaigns": "Campaigns",
    "tab_control": "Control",
    "theme_light": "Light",
    "theme_dark": "Dark",
    "theme_auto": "Auto",
    "idle": "Idle",
    "remaining": "remaining",
    "drop_remaining": "Drop time left",
    "campaign_remaining": "Campaign time left",
    "reload": "Reload",
    "reload_done": "Reloaded",
    "add": "Add",
    "locked_notice": "This instance is view-only; control is disabled.",
    "priority_placeholder": "Game name",
    "exclude_placeholder": "Game name",
    "move_up": "Move up",
    "move_down": "Move down",
    "remove": "Remove",
    "empty_priority": "Priority list is empty.",
    "empty_exclude": "Exclude list is empty.",
    "exclude_list": "Exclude list"
  },
  "fr": {
    "subtitle": "Tableau de bord distant pour cette instance.",
    "mode_view": "Consultation uniquement",
    "mode_control": "Consultation et contrôle",
    "mining": "En cours",
    "paused": "En pause",
    "pause": "Pause",
    "resume": "Reprendre",
    "password_title": "Mot de passe de contrôle",
    "unlock": "Déverrouiller",
    "wrong_password": "Mot de passe incorrect.",
    "currently_mining": "En cours de minage",
    "drop_progress": "Progression du drop",
    "campaign_progress": "Progression de la campagne",
    "watching": "Chaîne regardée",
    "viewers": "spectateurs",
    "total_drops": "Total de drops récupérés",
    "hours_saved": "Heures de visionnage économisées",
    "priority_mode": "Mode de priorité",
    "priority_list": "Liste de priorité",
    "drops_per_game_title": "Drops par jeu",
    "campaigns_title": "Campagnes de drops",
    "campaign_search_placeholder": "Rechercher une campagne ou un jeu...",
    "sort_default": "Par défaut",
    "sort_recent": "Plus récent",
    "sort_progress": "Progression",
    "claimed": "Récupéré",
    "no_campaigns": "Aucune campagne à afficher pour le moment.",
    "connection_lost": "Connexion perdue, nouvelle tentative...",
    "modes": [
      "Liste de priorité uniquement",
      "Liste de priorité uniquement, puis continuer avec le reste",
      "Se termine le plus tôt",
      "Liste de priorité d'abord, puis se termine le plus tôt",
      "Faible disponibilité en premier",
      "Liste de priorité d'abord, puis faible disponibilité"
    ],
    "tab_dashboard": "Tableau de bord",
    "tab_campaigns": "Campagnes",
    "tab_control": "Contrôle",
    "theme_light": "Clair",
    "theme_dark": "Sombre",
    "theme_auto": "Auto",
    "idle": "Inactif",
    "remaining": "restant",
    "drop_remaining": "Temps restant pour le drop",
    "campaign_remaining": "Temps restant pour la campagne",
    "reload": "Recharger",
    "reload_done": "Rechargé",
    "add": "Ajouter",
    "locked_notice": "Cette instance est en consultation uniquement ; le contrôle est désactivé.",
    "priority_placeholder": "Nom du jeu",
    "exclude_placeholder": "Nom du jeu",
    "move_up": "Monter",
    "move_down": "Descendre",
    "remove": "Retirer",
    "empty_priority": "La liste de priorité est vide.",
    "empty_exclude": "La liste d'exclusion est vide.",
    "exclude_list": "Liste d'exclusion"
  },
  "de": {
    "subtitle": "Fernsteuerungs-Dashboard für diese Instanz.",
    "mode_view": "Nur ansehen",
    "mode_control": "Ansehen & steuern",
    "mining": "Aktiv",
    "paused": "Pausiert",
    "pause": "Pause",
    "resume": "Fortsetzen",
    "password_title": "Steuerungspasswort",
    "unlock": "Entsperren",
    "wrong_password": "Falsches Passwort.",
    "currently_mining": "Aktuell aktiv",
    "drop_progress": "Drop-Fortschritt",
    "campaign_progress": "Kampagnen-Fortschritt",
    "watching": "Angesehener Kanal",
    "viewers": "Zuschauer",
    "total_drops": "Insgesamt erhaltene Drops",
    "hours_saved": "Gesparte Zuschauzeit",
    "priority_mode": "Prioritätsmodus",
    "priority_list": "Prioritätsliste",
    "drops_per_game_title": "Drops pro Spiel",
    "campaigns_title": "Drop-Kampagnen",
    "campaign_search_placeholder": "Kampagnen oder Spiele suchen...",
    "sort_default": "Standard",
    "sort_recent": "Neueste",
    "sort_progress": "Fortschritt",
    "claimed": "Erhalten",
    "no_campaigns": "Noch keine Kampagnen vorhanden.",
    "connection_lost": "Verbindung verloren, erneuter Versuch...",
    "modes": [
      "Nur Prioritätsliste",
      "Nur Prioritätsliste, dann mit dem Rest fortfahren",
      "Endet am frühesten",
      "Zuerst Prioritätsliste, dann am frühesten endend",
      "Zuerst geringe Verfügbarkeit",
      "Zuerst Prioritätsliste, dann geringe Verfügbarkeit"
    ],
    "tab_dashboard": "Übersicht",
    "tab_campaigns": "Kampagnen",
    "tab_control": "Steuerung",
    "theme_light": "Hell",
    "theme_dark": "Dunkel",
    "theme_auto": "Automatisch",
    "idle": "Inaktiv",
    "remaining": "verbleibend",
    "drop_remaining": "Verbleibende Zeit für Drop",
    "campaign_remaining": "Verbleibende Zeit für Kampagne",
    "reload": "Neu laden",
    "reload_done": "Neu geladen",
    "add": "Hinzufügen",
    "locked_notice": "Diese Instanz ist nur zum Ansehen; Steuerung ist deaktiviert.",
    "priority_placeholder": "Spielname",
    "exclude_placeholder": "Spielname",
    "move_up": "Nach oben",
    "move_down": "Nach unten",
    "remove": "Entfernen",
    "empty_priority": "Prioritätsliste ist leer.",
    "empty_exclude": "Ausschlussliste ist leer.",
    "exclude_list": "Ausschlussliste"
  },
  "es": {
    "subtitle": "Panel remoto para esta instancia.",
    "mode_view": "Solo ver",
    "mode_control": "Ver y controlar",
    "mining": "Minando",
    "paused": "En pausa",
    "pause": "Pausar",
    "resume": "Reanudar",
    "password_title": "Contraseña de control",
    "unlock": "Desbloquear",
    "wrong_password": "Contraseña incorrecta.",
    "currently_mining": "Minando actualmente",
    "drop_progress": "Progreso del drop",
    "campaign_progress": "Progreso de la campaña",
    "watching": "Canal en visión",
    "viewers": "espectadores",
    "total_drops": "Total de drops obtenidos",
    "hours_saved": "Horas de visionado ahorradas",
    "priority_mode": "Modo de prioridad",
    "priority_list": "Lista de prioridad",
    "drops_per_game_title": "Drops por juego",
    "campaigns_title": "Campañas de drops",
    "campaign_search_placeholder": "Buscar campañas o juegos...",
    "sort_default": "Predeterminado",
    "sort_recent": "Más reciente",
    "sort_progress": "Progreso",
    "claimed": "Obtenido",
    "no_campaigns": "Aún no hay campañas que mostrar.",
    "connection_lost": "Conexión perdida, reintentando...",
    "modes": [
      "Solo lista de prioridad",
      "Solo lista de prioridad, luego continuar con el resto",
      "Finaliza antes",
      "Lista de prioridad primero, luego finaliza antes",
      "Baja disponibilidad primero",
      "Lista de prioridad primero, luego baja disponibilidad"
    ],
    "tab_dashboard": "Panel",
    "tab_campaigns": "Campañas",
    "tab_control": "Control",
    "theme_light": "Claro",
    "theme_dark": "Oscuro",
    "theme_auto": "Automático",
    "idle": "Inactivo",
    "remaining": "restante",
    "drop_remaining": "Tiempo restante del drop",
    "campaign_remaining": "Tiempo restante de la campaña",
    "reload": "Recargar",
    "reload_done": "Recargado",
    "add": "Añadir",
    "locked_notice": "Esta instancia es solo de visualización; el control está desactivado.",
    "priority_placeholder": "Nombre del juego",
    "exclude_placeholder": "Nombre del juego",
    "move_up": "Subir",
    "move_down": "Bajar",
    "remove": "Quitar",
    "empty_priority": "La lista de prioridad está vacía.",
    "empty_exclude": "La lista de exclusión está vacía.",
    "exclude_list": "Lista de exclusión"
  },
  "it": {
    "subtitle": "Pannello remoto per questa istanza.",
    "mode_view": "Solo visualizzazione",
    "mode_control": "Visualizzazione e controllo",
    "mining": "In corso",
    "paused": "In pausa",
    "pause": "Pausa",
    "resume": "Riprendi",
    "password_title": "Password di controllo",
    "unlock": "Sblocca",
    "wrong_password": "Password errata.",
    "currently_mining": "Attualmente in corso",
    "drop_progress": "Progresso del drop",
    "campaign_progress": "Progresso della campagna",
    "watching": "Canale seguito",
    "viewers": "spettatori",
    "total_drops": "Totale drop ottenuti",
    "hours_saved": "Ore di visione risparmiate",
    "priority_mode": "Modalità priorità",
    "priority_list": "Lista priorità",
    "drops_per_game_title": "Drop per gioco",
    "campaigns_title": "Campagne drop",
    "campaign_search_placeholder": "Cerca campagne o giochi...",
    "sort_default": "Predefinito",
    "sort_recent": "Più recente",
    "sort_progress": "Progresso",
    "claimed": "Ottenuto",
    "no_campaigns": "Nessuna campagna da mostrare per ora.",
    "connection_lost": "Connessione persa, nuovo tentativo...",
    "modes": [
      "Solo lista priorità",
      "Solo lista priorità, poi continua con il resto",
      "Termina prima",
      "Lista priorità prima, poi termina prima",
      "Bassa disponibilità prima",
      "Lista priorità prima, poi bassa disponibilità"
    ],
    "tab_dashboard": "Pannello",
    "tab_campaigns": "Campagne",
    "tab_control": "Controllo",
    "theme_light": "Chiaro",
    "theme_dark": "Scuro",
    "theme_auto": "Automatico",
    "idle": "Inattivo",
    "remaining": "rimanente",
    "drop_remaining": "Tempo rimanente per il drop",
    "campaign_remaining": "Tempo rimanente per la campagna",
    "reload": "Ricarica",
    "reload_done": "Ricaricato",
    "add": "Aggiungi",
    "locked_notice": "Questa istanza è di sola visualizzazione; il controllo è disattivato.",
    "priority_placeholder": "Nome del gioco",
    "exclude_placeholder": "Nome del gioco",
    "move_up": "Sposta su",
    "move_down": "Sposta giù",
    "remove": "Rimuovi",
    "empty_priority": "La lista di priorità è vuota.",
    "empty_exclude": "La lista di esclusione è vuota.",
    "exclude_list": "Lista di esclusione"
  },
  "pt": {
    "subtitle": "Painel remoto para esta instância.",
    "mode_view": "Apenas visualizar",
    "mode_control": "Visualizar e controlar",
    "mining": "A minerar",
    "paused": "Em pausa",
    "pause": "Pausar",
    "resume": "Retomar",
    "password_title": "Palavra-passe de controlo",
    "unlock": "Desbloquear",
    "wrong_password": "Palavra-passe incorreta.",
    "currently_mining": "A minerar atualmente",
    "drop_progress": "Progresso do drop",
    "campaign_progress": "Progresso da campanha",
    "watching": "A assistir",
    "viewers": "espetadores",
    "total_drops": "Total de drops obtidos",
    "hours_saved": "Horas de visualização poupadas",
    "priority_mode": "Modo de prioridade",
    "priority_list": "Lista de prioridade",
    "drops_per_game_title": "Drops por jogo",
    "campaigns_title": "Campanhas de drops",
    "campaign_search_placeholder": "Pesquisar campanhas ou jogos...",
    "sort_default": "Padrão",
    "sort_recent": "Mais recente",
    "sort_progress": "Progresso",
    "claimed": "Obtido",
    "no_campaigns": "Ainda não há campanhas para mostrar.",
    "connection_lost": "Ligação perdida, a tentar novamente...",
    "modes": [
      "Apenas lista de prioridade",
      "Apenas lista de prioridade, depois continuar com o resto",
      "Termina mais cedo",
      "Lista de prioridade primeiro, depois termina mais cedo",
      "Baixa disponibilidade primeiro",
      "Lista de prioridade primeiro, depois baixa disponibilidade"
    ],
    "tab_dashboard": "Painel",
    "tab_campaigns": "Campanhas",
    "tab_control": "Controlo",
    "theme_light": "Claro",
    "theme_dark": "Escuro",
    "theme_auto": "Automático",
    "idle": "Inativo",
    "remaining": "restante",
    "drop_remaining": "Tempo restante do drop",
    "campaign_remaining": "Tempo restante da campanha",
    "reload": "Recarregar",
    "reload_done": "Recarregado",
    "add": "Adicionar",
    "locked_notice": "Esta instância é apenas de visualização; o controlo está desativado.",
    "priority_placeholder": "Nome do jogo",
    "exclude_placeholder": "Nome do jogo",
    "move_up": "Mover para cima",
    "move_down": "Mover para baixo",
    "remove": "Remover",
    "empty_priority": "A lista de prioridade está vazia.",
    "empty_exclude": "A lista de exclusão está vazia.",
    "exclude_list": "Lista de exclusão"
  },
  "nl": {
    "subtitle": "Extern dashboard voor deze instantie.",
    "mode_view": "Alleen bekijken",
    "mode_control": "Bekijken & besturen",
    "mining": "Actief",
    "paused": "Gepauzeerd",
    "pause": "Pauzeren",
    "resume": "Hervatten",
    "password_title": "Besturingswachtwoord",
    "unlock": "Ontgrendelen",
    "wrong_password": "Onjuist wachtwoord.",
    "currently_mining": "Nu actief",
    "drop_progress": "Drop-voortgang",
    "campaign_progress": "Campagnevoortgang",
    "watching": "Bekeken kanaal",
    "viewers": "kijkers",
    "total_drops": "Totaal aantal drops",
    "hours_saved": "Bespaarde kijkuren",
    "priority_mode": "Prioriteitsmodus",
    "priority_list": "Prioriteitslijst",
    "drops_per_game_title": "Drops per spel",
    "campaigns_title": "Drop-campagnes",
    "campaign_search_placeholder": "Campagnes of games zoeken...",
    "sort_default": "Standaard",
    "sort_recent": "Meest recent",
    "sort_progress": "Voortgang",
    "claimed": "Verkregen",
    "no_campaigns": "Nog geen campagnes om te tonen.",
    "connection_lost": "Verbinding verbroken, opnieuw proberen...",
    "modes": [
      "Alleen prioriteitslijst",
      "Alleen prioriteitslijst, daarna de rest",
      "Eindigt eerst",
      "Eerst prioriteitslijst, dan eindigt eerst",
      "Eerst lage beschikbaarheid",
      "Eerst prioriteitslijst, dan lage beschikbaarheid"
    ],
    "tab_dashboard": "Dashboard",
    "tab_campaigns": "Campagnes",
    "tab_control": "Besturing",
    "theme_light": "Licht",
    "theme_dark": "Donker",
    "theme_auto": "Automatisch",
    "idle": "Inactief",
    "remaining": "resterend",
    "drop_remaining": "Resterende tijd voor drop",
    "campaign_remaining": "Resterende tijd voor campagne",
    "reload": "Herladen",
    "reload_done": "Herladen",
    "add": "Toevoegen",
    "locked_notice": "Deze instantie is alleen-lezen; besturing is uitgeschakeld.",
    "priority_placeholder": "Spelnaam",
    "exclude_placeholder": "Spelnaam",
    "move_up": "Omhoog",
    "move_down": "Omlaag",
    "remove": "Verwijderen",
    "empty_priority": "Prioriteitslijst is leeg.",
    "empty_exclude": "Uitsluitingslijst is leeg.",
    "exclude_list": "Uitsluitingslijst"
  },
  "da": {
    "subtitle": "Fjernpanel for denne instans.",
    "mode_view": "Kun visning",
    "mode_control": "Visning & styring",
    "mining": "Aktiv",
    "paused": "Pause",
    "pause": "Pause",
    "resume": "Genoptag",
    "password_title": "Styringsadgangskode",
    "unlock": "Lås op",
    "wrong_password": "Forkert adgangskode.",
    "currently_mining": "Aktiv nu",
    "drop_progress": "Drop-fremgang",
    "campaign_progress": "Kampagnefremgang",
    "watching": "Ser på kanal",
    "viewers": "seere",
    "total_drops": "Antal opnåede drops",
    "hours_saved": "Sparede seetimer",
    "priority_mode": "Prioritetstilstand",
    "priority_list": "Prioritetsliste",
    "drops_per_game_title": "Drops pr. spil",
    "campaigns_title": "Drop-kampagner",
    "campaign_search_placeholder": "Søg kampagner eller spil...",
    "sort_default": "Standard",
    "sort_recent": "Nyeste",
    "sort_progress": "Fremskridt",
    "claimed": "Opnået",
    "no_campaigns": "Ingen kampagner at vise endnu.",
    "connection_lost": "Forbindelse mistet, prøver igen...",
    "modes": [
      "Kun prioritetsliste",
      "Kun prioritetsliste, derefter resten",
      "Slutter først",
      "Prioritetsliste først, derefter slutter først",
      "Lav tilgængelighed først",
      "Prioritetsliste først, derefter lav tilgængelighed"
    ],
    "tab_dashboard": "Oversigt",
    "tab_campaigns": "Kampagner",
    "tab_control": "Styring",
    "theme_light": "Lys",
    "theme_dark": "Mørk",
    "theme_auto": "Automatisk",
    "idle": "Inaktiv",
    "remaining": "tilbage",
    "drop_remaining": "Resterende tid for drop",
    "campaign_remaining": "Resterende tid for kampagne",
    "reload": "Genindlæs",
    "reload_done": "Genindlæst",
    "add": "Tilføj",
    "locked_notice": "Denne instans er kun til visning; styring er deaktiveret.",
    "priority_placeholder": "Spilnavn",
    "exclude_placeholder": "Spilnavn",
    "move_up": "Flyt op",
    "move_down": "Flyt ned",
    "remove": "Fjern",
    "empty_priority": "Prioritetslisten er tom.",
    "empty_exclude": "Udelukkelseslisten er tom.",
    "exclude_list": "Udelukkelsesliste"
  },
  "no": {
    "subtitle": "Fjernpanel for denne forekomsten.",
    "mode_view": "Kun visning",
    "mode_control": "Visning & styring",
    "mining": "Aktiv",
    "paused": "Pause",
    "pause": "Pause",
    "resume": "Gjenoppta",
    "password_title": "Styringspassord",
    "unlock": "Lås opp",
    "wrong_password": "Feil passord.",
    "currently_mining": "Aktiv nå",
    "drop_progress": "Drop-fremgang",
    "campaign_progress": "Kampanjefremgang",
    "watching": "Ser på kanal",
    "viewers": "seere",
    "total_drops": "Antall oppnådde drops",
    "hours_saved": "Sparte seertimer",
    "priority_mode": "Prioritetsmodus",
    "priority_list": "Prioritetsliste",
    "drops_per_game_title": "Drops per spill",
    "campaigns_title": "Drop-kampanjer",
    "campaign_search_placeholder": "Søk kampanjer eller spill...",
    "sort_default": "Standard",
    "sort_recent": "Nyeste",
    "sort_progress": "Fremdrift",
    "claimed": "Oppnådd",
    "no_campaigns": "Ingen kampanjer å vise ennå.",
    "connection_lost": "Mistet forbindelse, prøver igjen...",
    "modes": [
      "Kun prioritetsliste",
      "Kun prioritetsliste, deretter resten",
      "Slutter først",
      "Prioritetsliste først, deretter slutter først",
      "Lav tilgjengelighet først",
      "Prioritetsliste først, deretter lav tilgjengelighet"
    ],
    "tab_dashboard": "Oversikt",
    "tab_campaigns": "Kampanjer",
    "tab_control": "Styring",
    "theme_light": "Lys",
    "theme_dark": "Mørk",
    "theme_auto": "Automatisk",
    "idle": "Inaktiv",
    "remaining": "gjenstår",
    "drop_remaining": "Gjenstående tid for drop",
    "campaign_remaining": "Gjenstående tid for kampanje",
    "reload": "Last inn på nytt",
    "reload_done": "Lastet inn på nytt",
    "add": "Legg til",
    "locked_notice": "Denne forekomsten er kun for visning; styring er deaktivert.",
    "priority_placeholder": "Spillnavn",
    "exclude_placeholder": "Spillnavn",
    "move_up": "Flytt opp",
    "move_down": "Flytt ned",
    "remove": "Fjern",
    "empty_priority": "Prioritetslisten er tom.",
    "empty_exclude": "Ekskluderingslisten er tom.",
    "exclude_list": "Ekskluderingsliste"
  },
  "pl": {
    "subtitle": "Zdalny panel dla tej instancji.",
    "mode_view": "Tylko podgląd",
    "mode_control": "Podgląd i sterowanie",
    "mining": "Zdobywanie",
    "paused": "Wstrzymano",
    "pause": "Wstrzymaj",
    "resume": "Wznów",
    "password_title": "Hasło sterowania",
    "unlock": "Odblokuj",
    "wrong_password": "Nieprawidłowe hasło.",
    "currently_mining": "Aktualnie zdobywane",
    "drop_progress": "Postęp dropa",
    "campaign_progress": "Postęp kampanii",
    "watching": "Oglądany kanał",
    "viewers": "widzów",
    "total_drops": "Łączna liczba zdobytych dropów",
    "hours_saved": "Zaoszczędzone godziny oglądania",
    "priority_mode": "Tryb priorytetu",
    "priority_list": "Lista priorytetowa",
    "drops_per_game_title": "Dropy wg gry",
    "campaigns_title": "Kampanie dropów",
    "campaign_search_placeholder": "Szukaj kampanii lub gier...",
    "sort_default": "Domyślne",
    "sort_recent": "Najnowsze",
    "sort_progress": "Postęp",
    "claimed": "Zdobyto",
    "no_campaigns": "Brak kampanii do wyświetlenia.",
    "connection_lost": "Utracono połączenie, ponawianie...",
    "modes": [
      "Tylko lista priorytetowa",
      "Tylko lista priorytetowa, następnie reszta",
      "Kończy się najwcześniej",
      "Najpierw lista priorytetowa, potem kończy się najwcześniej",
      "Najpierw niska dostępność",
      "Najpierw lista priorytetowa, potem niska dostępność"
    ],
    "tab_dashboard": "Panel",
    "tab_campaigns": "Kampanie",
    "tab_control": "Sterowanie",
    "theme_light": "Jasny",
    "theme_dark": "Ciemny",
    "theme_auto": "Automatyczny",
    "idle": "Bezczynny",
    "remaining": "pozostało",
    "drop_remaining": "Pozostały czas dropa",
    "campaign_remaining": "Pozostały czas kampanii",
    "reload": "Przeładuj",
    "reload_done": "Przeładowano",
    "add": "Dodaj",
    "locked_notice": "Ta instancja jest tylko do podglądu; sterowanie jest wyłączone.",
    "priority_placeholder": "Nazwa gry",
    "exclude_placeholder": "Nazwa gry",
    "move_up": "Przesuń w górę",
    "move_down": "Przesuń w dół",
    "remove": "Usuń",
    "empty_priority": "Lista priorytetowa jest pusta.",
    "empty_exclude": "Lista wykluczeń jest pusta.",
    "exclude_list": "Lista wykluczeń"
  },
  "cs": {
    "subtitle": "Vzdálený panel pro tuto instanci.",
    "mode_view": "Pouze zobrazení",
    "mode_control": "Zobrazení a ovládání",
    "mining": "Těžba",
    "paused": "Pozastaveno",
    "pause": "Pozastavit",
    "resume": "Pokračovat",
    "password_title": "Heslo pro ovládání",
    "unlock": "Odemknout",
    "wrong_password": "Nesprávné heslo.",
    "currently_mining": "Právě těženo",
    "drop_progress": "Postup dropu",
    "campaign_progress": "Postup kampaně",
    "watching": "Sledovaný kanál",
    "viewers": "diváků",
    "total_drops": "Celkem získaných dropů",
    "hours_saved": "Ušetřené hodiny sledování",
    "priority_mode": "Režim priority",
    "priority_list": "Seznam priorit",
    "drops_per_game_title": "Dropy podle hry",
    "campaigns_title": "Kampaně dropů",
    "campaign_search_placeholder": "Hledat kampaně nebo hry...",
    "sort_default": "Výchozí",
    "sort_recent": "Nejnovější",
    "sort_progress": "Postup",
    "claimed": "Získáno",
    "no_campaigns": "Zatím žádné kampaně k zobrazení.",
    "connection_lost": "Spojení ztraceno, zkouším znovu...",
    "modes": [
      "Pouze seznam priorit",
      "Pouze seznam priorit, poté zbytek",
      "Končí nejdříve",
      "Nejprve seznam priorit, poté končí nejdříve",
      "Nejprve nízká dostupnost",
      "Nejprve seznam priorit, poté nízká dostupnost"
    ],
    "tab_dashboard": "Přehled",
    "tab_campaigns": "Kampaně",
    "tab_control": "Ovládání",
    "theme_light": "Světlý",
    "theme_dark": "Tmavý",
    "theme_auto": "Automaticky",
    "idle": "Nečinné",
    "remaining": "zbývá",
    "drop_remaining": "Zbývající čas dropu",
    "campaign_remaining": "Zbývající čas kampaně",
    "reload": "Obnovit",
    "reload_done": "Obnoveno",
    "add": "Přidat",
    "locked_notice": "Tato instance je pouze pro zobrazení; ovládání je vypnuto.",
    "priority_placeholder": "Název hry",
    "exclude_placeholder": "Název hry",
    "move_up": "Posunout nahoru",
    "move_down": "Posunout dolů",
    "remove": "Odebrat",
    "empty_priority": "Seznam priorit je prázdný.",
    "empty_exclude": "Seznam vyloučení je prázdný.",
    "exclude_list": "Seznam vyloučení"
  },
  "ro": {
    "subtitle": "Panou de la distanță pentru această instanță.",
    "mode_view": "Doar vizualizare",
    "mode_control": "Vizualizare și control",
    "mining": "Activ",
    "paused": "Pauzat",
    "pause": "Pauză",
    "resume": "Reluare",
    "password_title": "Parolă de control",
    "unlock": "Deblochează",
    "wrong_password": "Parolă incorectă.",
    "currently_mining": "În curs de minare",
    "drop_progress": "Progres drop",
    "campaign_progress": "Progres campanie",
    "watching": "Canal urmărit",
    "viewers": "spectatori",
    "total_drops": "Total drop-uri obținute",
    "hours_saved": "Ore de vizionare economisite",
    "priority_mode": "Mod de prioritate",
    "priority_list": "Listă de prioritate",
    "drops_per_game_title": "Drop-uri pe joc",
    "campaigns_title": "Campanii de drop-uri",
    "campaign_search_placeholder": "Caută campanii sau jocuri...",
    "sort_default": "Implicit",
    "sort_recent": "Cele mai recente",
    "sort_progress": "Progres",
    "claimed": "Obținut",
    "no_campaigns": "Nicio campanie de afișat momentan.",
    "connection_lost": "Conexiune pierdută, se reîncearcă...",
    "modes": [
      "Doar lista de prioritate",
      "Doar lista de prioritate, apoi restul",
      "Se termină cel mai devreme",
      "Lista de prioritate mai întâi, apoi cel mai devreme",
      "Disponibilitate scăzută mai întâi",
      "Lista de prioritate mai întâi, apoi disponibilitate scăzută"
    ],
    "tab_dashboard": "Panou",
    "tab_campaigns": "Campanii",
    "tab_control": "Control",
    "theme_light": "Luminos",
    "theme_dark": "Întunecat",
    "theme_auto": "Automat",
    "idle": "Inactiv",
    "remaining": "rămas",
    "drop_remaining": "Timp rămas pentru drop",
    "campaign_remaining": "Timp rămas pentru campanie",
    "reload": "Reîncarcă",
    "reload_done": "Reîncărcat",
    "add": "Adaugă",
    "locked_notice": "Această instanță este doar pentru vizualizare; controlul este dezactivat.",
    "priority_placeholder": "Numele jocului",
    "exclude_placeholder": "Numele jocului",
    "move_up": "Mută în sus",
    "move_down": "Mută în jos",
    "remove": "Elimină",
    "empty_priority": "Lista de prioritate este goală.",
    "empty_exclude": "Lista de excludere este goală.",
    "exclude_list": "Listă de excludere"
  },
  "hu": {
    "subtitle": "Távoli irányítópult ehhez a példányhoz.",
    "mode_view": "Csak megtekintés",
    "mode_control": "Megtekintés és vezérlés",
    "mining": "Bányászás",
    "paused": "Szüneteltetve",
    "pause": "Szünet",
    "resume": "Folytatás",
    "password_title": "Vezérlési jelszó",
    "unlock": "Feloldás",
    "wrong_password": "Hibás jelszó.",
    "currently_mining": "Jelenleg bányászva",
    "drop_progress": "Drop folyamata",
    "campaign_progress": "Kampány folyamata",
    "watching": "Nézett csatorna",
    "viewers": "néző",
    "total_drops": "Összes megszerzett drop",
    "hours_saved": "Megtakarított nézési órák",
    "priority_mode": "Prioritási mód",
    "priority_list": "Prioritási lista",
    "drops_per_game_title": "Dropok játékonként",
    "campaigns_title": "Drop kampányok",
    "campaign_search_placeholder": "Kampányok vagy játékok keresése...",
    "sort_default": "Alapértelmezett",
    "sort_recent": "Legújabb",
    "sort_progress": "Előrehaladás",
    "claimed": "Megszerezve",
    "no_campaigns": "Még nincs megjeleníthető kampány.",
    "connection_lost": "Kapcsolat megszakadt, újrapróbálkozás...",
    "modes": [
      "Csak prioritási lista",
      "Csak prioritási lista, majd a többi",
      "Leghamarabb véget érő",
      "Először prioritási lista, majd leghamarabb véget érő",
      "Először alacsony elérhetőség",
      "Először prioritási lista, majd alacsony elérhetőség"
    ],
    "tab_dashboard": "Áttekintés",
    "tab_campaigns": "Kampányok",
    "tab_control": "Vezérlés",
    "theme_light": "Világos",
    "theme_dark": "Sötét",
    "theme_auto": "Automatikus",
    "idle": "Inaktív",
    "remaining": "hátra van",
    "drop_remaining": "Drop hátralévő ideje",
    "campaign_remaining": "Kampány hátralévő ideje",
    "reload": "Újratöltés",
    "reload_done": "Újratöltve",
    "add": "Hozzáadás",
    "locked_notice": "Ez a példány csak megtekinthető; a vezérlés le van tiltva.",
    "priority_placeholder": "Játék neve",
    "exclude_placeholder": "Játék neve",
    "move_up": "Felfelé",
    "move_down": "Lefelé",
    "remove": "Eltávolítás",
    "empty_priority": "A prioritási lista üres.",
    "empty_exclude": "A kizárási lista üres.",
    "exclude_list": "Kizárási lista"
  },
  "tr": {
    "subtitle": "Bu örnek için uzaktan panel.",
    "mode_view": "Yalnızca görüntüleme",
    "mode_control": "Görüntüleme ve kontrol",
    "mining": "Kazılıyor",
    "paused": "Duraklatıldı",
    "pause": "Duraklat",
    "resume": "Devam ettir",
    "password_title": "Kontrol parolası",
    "unlock": "Kilidi aç",
    "wrong_password": "Yanlış parola.",
    "currently_mining": "Şu anda kazılıyor",
    "drop_progress": "Drop ilerlemesi",
    "campaign_progress": "Kampanya ilerlemesi",
    "watching": "İzlenen kanal",
    "viewers": "izleyici",
    "total_drops": "Toplam kazanılan drop",
    "hours_saved": "Kazanılan izleme saati",
    "priority_mode": "Öncelik modu",
    "priority_list": "Öncelik listesi",
    "drops_per_game_title": "Oyuna göre droplar",
    "campaigns_title": "Drop kampanyaları",
    "campaign_search_placeholder": "Kampanya veya oyun ara...",
    "sort_default": "Varsayılan",
    "sort_recent": "En yeni",
    "sort_progress": "İlerleme",
    "claimed": "Kazanıldı",
    "no_campaigns": "Henüz gösterilecek kampanya yok.",
    "connection_lost": "Bağlantı kesildi, yeniden deneniyor...",
    "modes": [
      "Yalnızca öncelik listesi",
      "Yalnızca öncelik listesi, sonra geri kalanı",
      "En erken biten",
      "Önce öncelik listesi, sonra en erken biten",
      "Önce düşük erişilebilirlik",
      "Önce öncelik listesi, sonra düşük erişilebilirlik"
    ],
    "tab_dashboard": "Panel",
    "tab_campaigns": "Kampanyalar",
    "tab_control": "Kontrol",
    "theme_light": "Açık",
    "theme_dark": "Koyu",
    "theme_auto": "Otomatik",
    "idle": "Boşta",
    "remaining": "kaldı",
    "drop_remaining": "Drop için kalan süre",
    "campaign_remaining": "Kampanya için kalan süre",
    "reload": "Yeniden yükle",
    "reload_done": "Yeniden yüklendi",
    "add": "Ekle",
    "locked_notice": "Bu örnek yalnızca görüntüleme içindir; kontrol devre dışı.",
    "priority_placeholder": "Oyun adı",
    "exclude_placeholder": "Oyun adı",
    "move_up": "Yukarı taşı",
    "move_down": "Aşağı taşı",
    "remove": "Kaldır",
    "empty_priority": "Öncelik listesi boş.",
    "empty_exclude": "Hariç tutma listesi boş.",
    "exclude_list": "Hariç tutma listesi"
  },
  "ru": {
    "subtitle": "Панель удалённого доступа для этого экземпляра.",
    "mode_view": "Только просмотр",
    "mode_control": "Просмотр и управление",
    "mining": "Добыча",
    "paused": "На паузе",
    "pause": "Пауза",
    "resume": "Возобновить",
    "password_title": "Пароль управления",
    "unlock": "Разблокировать",
    "wrong_password": "Неверный пароль.",
    "currently_mining": "Сейчас добывается",
    "drop_progress": "Прогресс дропа",
    "campaign_progress": "Прогресс кампании",
    "watching": "Просматриваемый канал",
    "viewers": "зрителей",
    "total_drops": "Всего получено дропов",
    "hours_saved": "Сэкономлено часов просмотра",
    "priority_mode": "Режим приоритета",
    "priority_list": "Список приоритета",
    "drops_per_game_title": "Дропы по играм",
    "campaigns_title": "Кампании дропов",
    "campaign_search_placeholder": "Поиск кампаний или игр...",
    "sort_default": "По умолчанию",
    "sort_recent": "Недавние",
    "sort_progress": "Прогресс",
    "claimed": "Получено",
    "no_campaigns": "Пока нет кампаний для отображения.",
    "connection_lost": "Соединение потеряно, повтор попытки...",
    "modes": [
      "Только список приоритета",
      "Только список приоритета, затем остальное",
      "Заканчивается раньше всех",
      "Сначала список приоритета, затем заканчивается раньше всех",
      "Сначала низкая доступность",
      "Сначала список приоритета, затем низкая доступность"
    ],
    "tab_dashboard": "Обзор",
    "tab_campaigns": "Кампании",
    "tab_control": "Управление",
    "theme_light": "Светлая",
    "theme_dark": "Тёмная",
    "theme_auto": "Авто",
    "idle": "Бездействие",
    "remaining": "осталось",
    "drop_remaining": "Осталось времени на дроп",
    "campaign_remaining": "Осталось времени на кампанию",
    "reload": "Перезагрузить",
    "reload_done": "Перезагружено",
    "add": "Добавить",
    "locked_notice": "Этот экземпляр доступен только для просмотра; управление отключено.",
    "priority_placeholder": "Название игры",
    "exclude_placeholder": "Название игры",
    "move_up": "Переместить вверх",
    "move_down": "Переместить вниз",
    "remove": "Удалить",
    "empty_priority": "Список приоритета пуст.",
    "empty_exclude": "Список исключений пуст.",
    "exclude_list": "Список исключений"
  },
  "uk": {
    "subtitle": "Панель віддаленого доступу для цього екземпляра.",
    "mode_view": "Лише перегляд",
    "mode_control": "Перегляд і керування",
    "mining": "Видобуток",
    "paused": "На паузі",
    "pause": "Пауза",
    "resume": "Відновити",
    "password_title": "Пароль керування",
    "unlock": "Розблокувати",
    "wrong_password": "Невірний пароль.",
    "currently_mining": "Зараз видобувається",
    "drop_progress": "Прогрес дропу",
    "campaign_progress": "Прогрес кампанії",
    "watching": "Переглянутий канал",
    "viewers": "глядачів",
    "total_drops": "Всього отримано дропів",
    "hours_saved": "Заощаджено годин перегляду",
    "priority_mode": "Режим пріоритету",
    "priority_list": "Список пріоритету",
    "drops_per_game_title": "Дропи за іграми",
    "campaigns_title": "Кампанії дропів",
    "campaign_search_placeholder": "Пошук кампаній або ігор...",
    "sort_default": "За замовчуванням",
    "sort_recent": "Найновіші",
    "sort_progress": "Прогрес",
    "claimed": "Отримано",
    "no_campaigns": "Поки немає кампаній для показу.",
    "connection_lost": "З'єднання втрачено, повторна спроба...",
    "modes": [
      "Лише список пріоритету",
      "Лише список пріоритету, потім решта",
      "Закінчується найшвидше",
      "Спочатку список пріоритету, потім закінчується найшвидше",
      "Спочатку низька доступність",
      "Спочатку список пріоритету, потім низька доступність"
    ],
    "tab_dashboard": "Огляд",
    "tab_campaigns": "Кампанії",
    "tab_control": "Керування",
    "theme_light": "Світла",
    "theme_dark": "Темна",
    "theme_auto": "Авто",
    "idle": "Бездіяльність",
    "remaining": "залишилось",
    "drop_remaining": "Залишилось часу на дроп",
    "campaign_remaining": "Залишилось часу на кампанію",
    "reload": "Перезавантажити",
    "reload_done": "Перезавантажено",
    "add": "Додати",
    "locked_notice": "Цей екземпляр доступний лише для перегляду; керування вимкнено.",
    "priority_placeholder": "Назва гри",
    "exclude_placeholder": "Назва гри",
    "move_up": "Перемістити вгору",
    "move_down": "Перемістити вниз",
    "remove": "Видалити",
    "empty_priority": "Список пріоритету порожній.",
    "empty_exclude": "Список виключень порожній.",
    "exclude_list": "Список виключень"
  },
  "ar": {
    "subtitle": "لوحة تحكم عن بُعد لهذا التطبيق.",
    "mode_view": "عرض فقط",
    "mode_control": "عرض وتحكم",
    "mining": "قيد التعدين",
    "paused": "متوقف مؤقتًا",
    "pause": "إيقاف مؤقت",
    "resume": "استئناف",
    "password_title": "كلمة مرور التحكم",
    "unlock": "فتح",
    "wrong_password": "كلمة مرور غير صحيحة.",
    "currently_mining": "قيد التعدين حاليًا",
    "drop_progress": "تقدم الدروب",
    "campaign_progress": "تقدم الحملة",
    "watching": "القناة المشاهدة",
    "viewers": "مشاهد",
    "total_drops": "إجمالي الدروبات المستلمة",
    "hours_saved": "ساعات المشاهدة الموفرة",
    "priority_mode": "وضع الأولوية",
    "priority_list": "قائمة الأولوية",
    "drops_per_game_title": "الدروبات حسب اللعبة",
    "campaigns_title": "حملات الدروب",
    "campaign_search_placeholder": "البحث عن الحملات أو الألعاب...",
    "sort_default": "افتراضي",
    "sort_recent": "الأحدث",
    "sort_progress": "التقدم",
    "claimed": "تم الاستلام",
    "no_campaigns": "لا توجد حملات لعرضها بعد.",
    "connection_lost": "انقطع الاتصال، جارٍ إعادة المحاولة...",
    "modes": [
      "قائمة الأولوية فقط",
      "قائمة الأولوية فقط، ثم الباقي",
      "الأقرب انتهاءً",
      "قائمة الأولوية أولاً، ثم الأقرب انتهاءً",
      "التوفر المنخفض أولاً",
      "قائمة الأولوية أولاً، ثم التوفر المنخفض"
    ],
    "tab_dashboard": "لوحة القيادة",
    "tab_campaigns": "الحملات",
    "tab_control": "التحكم",
    "theme_light": "فاتح",
    "theme_dark": "داكن",
    "theme_auto": "تلقائي",
    "idle": "خامل",
    "remaining": "متبقٍ",
    "drop_remaining": "الوقت المتبقي للدروب",
    "campaign_remaining": "الوقت المتبقي للحملة",
    "reload": "إعادة تحميل",
    "reload_done": "تمت إعادة التحميل",
    "add": "إضافة",
    "locked_notice": "هذا التطبيق للعرض فقط؛ التحكم معطّل.",
    "priority_placeholder": "اسم اللعبة",
    "exclude_placeholder": "اسم اللعبة",
    "move_up": "نقل لأعلى",
    "move_down": "نقل لأسفل",
    "remove": "إزالة",
    "empty_priority": "قائمة الأولوية فارغة.",
    "empty_exclude": "قائمة الاستبعاد فارغة.",
    "exclude_list": "قائمة الاستبعاد"
  },
  "ja": {
    "subtitle": "このインスタンスのリモートダッシュボード。",
    "mode_view": "閲覧のみ",
    "mode_control": "閲覧と操作",
    "mining": "マイニング中",
    "paused": "一時停止中",
    "pause": "一時停止",
    "resume": "再開",
    "password_title": "操作用パスワード",
    "unlock": "ロック解除",
    "wrong_password": "パスワードが違います。",
    "currently_mining": "現在マイニング中",
    "drop_progress": "ドロップの進捗",
    "campaign_progress": "キャンペーンの進捗",
    "watching": "視聴中のチャンネル",
    "viewers": "視聴者",
    "total_drops": "獲得したドロップ合計",
    "hours_saved": "節約した視聴時間",
    "priority_mode": "優先モード",
    "priority_list": "優先リスト",
    "drops_per_game_title": "ゲーム別ドロップ",
    "campaigns_title": "ドロップキャンペーン",
    "campaign_search_placeholder": "キャンペーンやゲームを検索...",
    "sort_default": "デフォルト",
    "sort_recent": "最新",
    "sort_progress": "進捗",
    "claimed": "獲得済み",
    "no_campaigns": "表示するキャンペーンはまだありません。",
    "connection_lost": "接続が切断されました。再試行中...",
    "modes": [
      "優先リストのみ",
      "優先リストのみ、その後残りを続行",
      "終了が最も早い順",
      "優先リストを優先し、その後終了が早い順",
      "在庫が少ない順を優先",
      "優先リストを優先し、その後在庫が少ない順"
    ],
    "tab_dashboard": "ダッシュボード",
    "tab_campaigns": "キャンペーン",
    "tab_control": "操作",
    "theme_light": "ライト",
    "theme_dark": "ダーク",
    "theme_auto": "自動",
    "idle": "待機中",
    "remaining": "残り",
    "drop_remaining": "ドロップの残り時間",
    "campaign_remaining": "キャンペーンの残り時間",
    "reload": "再読み込み",
    "reload_done": "再読み込みしました",
    "add": "追加",
    "locked_notice": "このインスタンスは閲覧専用です。操作は無効になっています。",
    "priority_placeholder": "ゲーム名",
    "exclude_placeholder": "ゲーム名",
    "move_up": "上へ移動",
    "move_down": "下へ移動",
    "remove": "削除",
    "empty_priority": "優先リストは空です。",
    "empty_exclude": "除外リストは空です。",
    "exclude_list": "除外リスト"
  },
  "zh-CN": {
    "subtitle": "此实例的远程控制面板。",
    "mode_view": "仅查看",
    "mode_control": "查看并控制",
    "mining": "正在挖取",
    "paused": "已暂停",
    "pause": "暂停",
    "resume": "继续",
    "password_title": "控制密码",
    "unlock": "解锁",
    "wrong_password": "密码错误。",
    "currently_mining": "当前正在挖取",
    "drop_progress": "掉落进度",
    "campaign_progress": "活动进度",
    "watching": "正在观看的频道",
    "viewers": "观众",
    "total_drops": "已获得掉落总数",
    "hours_saved": "节省的观看时长",
    "priority_mode": "优先模式",
    "priority_list": "优先列表",
    "drops_per_game_title": "各游戏掉落数",
    "campaigns_title": "掉落活动",
    "campaign_search_placeholder": "搜索活动或游戏...",
    "sort_default": "默认",
    "sort_recent": "最新",
    "sort_progress": "进度",
    "claimed": "已获得",
    "no_campaigns": "暂无活动可显示。",
    "connection_lost": "连接已断开，正在重试...",
    "modes": [
      "仅优先列表",
      "仅优先列表，然后继续其余的",
      "最早结束优先",
      "优先列表优先，然后最早结束优先",
      "低可用性优先",
      "优先列表优先，然后低可用性优先"
    ],
    "tab_dashboard": "仪表盘",
    "tab_campaigns": "活动",
    "tab_control": "控制",
    "theme_light": "浅色",
    "theme_dark": "深色",
    "theme_auto": "自动",
    "idle": "闲置",
    "remaining": "剩余",
    "drop_remaining": "掉落剩余时间",
    "campaign_remaining": "活动剩余时间",
    "reload": "重新加载",
    "reload_done": "已重新加载",
    "add": "添加",
    "locked_notice": "此实例仅供查看；控制功能已禁用。",
    "priority_placeholder": "游戏名称",
    "exclude_placeholder": "游戏名称",
    "move_up": "上移",
    "move_down": "下移",
    "remove": "移除",
    "empty_priority": "优先列表为空。",
    "empty_exclude": "排除列表为空。",
    "exclude_list": "排除列表"
  },
  "zh-TW": {
    "subtitle": "此實例的遠端控制面板。",
    "mode_view": "僅檢視",
    "mode_control": "檢視並控制",
    "mining": "挖取中",
    "paused": "已暫停",
    "pause": "暫停",
    "resume": "繼續",
    "password_title": "控制密碼",
    "unlock": "解鎖",
    "wrong_password": "密碼錯誤。",
    "currently_mining": "目前挖取中",
    "drop_progress": "掉落進度",
    "campaign_progress": "活動進度",
    "watching": "正在觀看的頻道",
    "viewers": "觀眾",
    "total_drops": "已獲得掉落總數",
    "hours_saved": "節省的觀看時數",
    "priority_mode": "優先模式",
    "priority_list": "優先清單",
    "drops_per_game_title": "各遊戲掉落數",
    "campaigns_title": "掉落活動",
    "campaign_search_placeholder": "搜尋活動或遊戲...",
    "sort_default": "預設",
    "sort_recent": "最新",
    "sort_progress": "進度",
    "claimed": "已獲得",
    "no_campaigns": "目前沒有活動可顯示。",
    "connection_lost": "連線已中斷，正在重試...",
    "modes": [
      "僅優先清單",
      "僅優先清單，然後繼續其餘的",
      "最早結束優先",
      "優先清單優先，然後最早結束優先",
      "低可用性優先",
      "優先清單優先，然後低可用性優先"
    ],
    "tab_dashboard": "儀表板",
    "tab_campaigns": "活動",
    "tab_control": "控制",
    "theme_light": "淺色",
    "theme_dark": "深色",
    "theme_auto": "自動",
    "idle": "閒置",
    "remaining": "剩餘",
    "drop_remaining": "掉落剩餘時間",
    "campaign_remaining": "活動剩餘時間",
    "reload": "重新載入",
    "reload_done": "已重新載入",
    "add": "新增",
    "locked_notice": "此實例僅供檢視；控制功能已停用。",
    "priority_placeholder": "遊戲名稱",
    "exclude_placeholder": "遊戲名稱",
    "move_up": "上移",
    "move_down": "下移",
    "remove": "移除",
    "empty_priority": "優先清單是空的。",
    "empty_exclude": "排除清單是空的。",
    "exclude_list": "排除清單"
  },
  "id": {
    "subtitle": "Dasbor jarak jauh untuk instans ini.",
    "mode_view": "Hanya lihat",
    "mode_control": "Lihat dan kendalikan",
    "mining": "Menambang",
    "paused": "Dijeda",
    "pause": "Jeda",
    "resume": "Lanjutkan",
    "password_title": "Kata sandi kendali",
    "unlock": "Buka kunci",
    "wrong_password": "Kata sandi salah.",
    "currently_mining": "Sedang ditambang",
    "drop_progress": "Progres drop",
    "campaign_progress": "Progres kampanye",
    "watching": "Saluran ditonton",
    "viewers": "penonton",
    "total_drops": "Total drop diperoleh",
    "hours_saved": "Jam tontonan yang dihemat",
    "priority_mode": "Mode prioritas",
    "priority_list": "Daftar prioritas",
    "drops_per_game_title": "Drop per game",
    "campaigns_title": "Kampanye drop",
    "campaign_search_placeholder": "Cari kampanye atau game...",
    "sort_default": "Default",
    "sort_recent": "Terbaru",
    "sort_progress": "Progres",
    "claimed": "Diperoleh",
    "no_campaigns": "Belum ada kampanye untuk ditampilkan.",
    "connection_lost": "Koneksi terputus, mencoba lagi...",
    "modes": [
      "Hanya daftar prioritas",
      "Hanya daftar prioritas, lalu lanjutkan sisanya",
      "Berakhir tercepat",
      "Daftar prioritas dulu, lalu berakhir tercepat",
      "Ketersediaan rendah dulu",
      "Daftar prioritas dulu, lalu ketersediaan rendah"
    ],
    "tab_dashboard": "Dasbor",
    "tab_campaigns": "Kampanye",
    "tab_control": "Kontrol",
    "theme_light": "Terang",
    "theme_dark": "Gelap",
    "theme_auto": "Otomatis",
    "idle": "Menganggur",
    "remaining": "tersisa",
    "drop_remaining": "Waktu tersisa untuk drop",
    "campaign_remaining": "Waktu tersisa untuk kampanye",
    "reload": "Muat ulang",
    "reload_done": "Dimuat ulang",
    "add": "Tambah",
    "locked_notice": "Instans ini hanya untuk melihat; kontrol dinonaktifkan.",
    "priority_placeholder": "Nama game",
    "exclude_placeholder": "Nama game",
    "move_up": "Naikkan",
    "move_down": "Turunkan",
    "remove": "Hapus",
    "empty_priority": "Daftar prioritas kosong.",
    "empty_exclude": "Daftar pengecualian kosong.",
    "exclude_list": "Daftar pengecualian"
  }
};
const LANG_NAMES = {
  en: "English", fr: "Français", de: "Deutsch", es: "Español", it: "Italiano", pt: "Português",
  nl: "Nederlands", da: "Dansk", no: "Norsk", pl: "Polski", cs: "Čeština", ro: "Română", hu: "Magyar",
  tr: "Türkçe", ru: "Русский", uk: "Українська", ar: "العربية", ja: "日本語",
  "zh-CN": "简体中文", "zh-TW": "繁體中文", id: "Indonesian",
};

function detectLang() {
  const saved = localStorage.getItem("dropstream_lang");
  if (saved && I18N[saved]) return saved;
  const nav = (navigator.language || "en");
  if (I18N[nav]) return nav;
  const short = nav.split("-")[0];
  if (short === "zh") return nav.toLowerCase().includes("tw") || nav.toLowerCase().includes("hant") ? "zh-TW" : "zh-CN";
  if (I18N[short]) return short;
  return "en";
}
let currentLang = detectLang();
let allCampaigns = [];

function t(key) {
  return (I18N[currentLang] && I18N[currentLang][key]) || I18N.en[key] || key;
}

function applyStaticTranslations() {
  document.documentElement.lang = currentLang;
  document.documentElement.dir = currentLang === "ar" ? "rtl" : "ltr";
  document.querySelectorAll("[data-i18n]").forEach(el => {
    el.textContent = t(el.getAttribute("data-i18n"));
  });
  document.getElementById("password-input").placeholder = t("password_title");
  document.getElementById("priority-input").placeholder = t("priority_placeholder");
  document.getElementById("exclude-input").placeholder = t("exclude_placeholder");
  document.getElementById("campaign-search").placeholder = t("campaign_search_placeholder");
  const modeSelect = document.getElementById("priority-mode");
  const modeLabels = t("modes");
  [0, 3, 1, 4, 2, 5].forEach((value, i) => {
    const opt = modeSelect.querySelector(`option[value="${value}"]`);
    if (opt) opt.textContent = modeLabels[i];
  });
}

const langSelect = document.getElementById("lang-select");
for (const code of Object.keys(I18N)) {
  const opt = document.createElement("option");
  opt.value = code;
  opt.textContent = LANG_NAMES[code] || code;
  langSelect.appendChild(opt);
}
langSelect.value = currentLang;
langSelect.addEventListener("change", () => {
  currentLang = langSelect.value;
  localStorage.setItem("dropstream_lang", currentLang);
  applyStaticTranslations();
  refresh();
});

// -- theme --
function applyTheme(mode) {
  if (mode === "auto") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", mode);
  }
  ["light", "dark", "auto"].forEach(m => {
    document.getElementById("theme-" + m).classList.toggle("active", m === mode);
  });
  localStorage.setItem("dropstream_theme", mode);
}
let currentTheme = localStorage.getItem("dropstream_theme") || "auto";
applyTheme(currentTheme);
["light", "dark", "auto"].forEach(m => {
  document.getElementById("theme-" + m).addEventListener("click", () => applyTheme(m));
});

// -- tabs --
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
  });
});

const base = location.pathname.endsWith("/") ? location.pathname.slice(0, -1) : location.pathname;
const PRIORITY_MODES = [0, 3, 1, 4, 2, 5];
const modeSelect = document.getElementById("priority-mode");
for (const value of PRIORITY_MODES) {
  const opt = document.createElement("option");
  opt.value = value;
  opt.textContent = value;
  modeSelect.appendChild(opt);
}
let applyingMode = false;
let password = "";
let lastPaused = false;

function pct(x) { return (x * 100).toFixed(1) + "%"; }
function fmtMinutes(mins) {
  if (mins == null || mins < 0) return "";
  const h = Math.floor(mins / 60), m = Math.round(mins % 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function setControlsEnabled(enabled) {
  document.getElementById("toggle-btn").disabled = !enabled;
  modeSelect.disabled = !enabled;
  document.getElementById("priority-add-btn").disabled = !enabled;
  document.getElementById("exclude-add-btn").disabled = !enabled;
  document.getElementById("reload-btn").disabled = !enabled;
}

async function post(path, body) {
  const headers = { "Content-Type": "application/json" };
  if (password) headers["X-Dashboard-Password"] = password;
  const res = await fetch(base + path, { method: "POST", headers, body: body ? JSON.stringify(body) : "{}" });
  if (res.status === 401) {
    password = "";
    document.getElementById("password-err").style.display = "block";
  }
  return res;
}

function renderRankList(perGame) {
  const list = document.getElementById("rank-list");
  list.innerHTML = "";
  perGame.forEach((entry, i) => {
    const li = document.createElement("li");
    li.className = "rank-item";
    const num = document.createElement("div");
    num.className = "rank-num";
    num.textContent = (i + 1).toString();
    li.appendChild(num);
    if (entry.image_url) {
      const img = document.createElement("img");
      img.src = entry.image_url;
      img.alt = "";
      li.appendChild(img);
    }
    const name = document.createElement("div");
    name.className = "rank-name";
    name.textContent = entry.game;
    li.appendChild(name);
    const count = document.createElement("div");
    count.className = "rank-count";
    count.textContent = entry.count;
    li.appendChild(count);
    list.appendChild(li);
  });
}

function renderCampaigns(campaigns) {
  const list = document.getElementById("campaign-list");
  const empty = document.getElementById("no-campaigns");
  list.innerHTML = "";
  if (!campaigns.length) {
    empty.style.display = "block";
    return;
  }
  empty.style.display = "none";
  for (const c of campaigns) {
    const card = document.createElement("div");
    card.className = "campaign-card";
    const img = document.createElement("img");
    img.className = "boxart";
    img.src = c.image_url || "";
    img.alt = "";
    card.appendChild(img);
    const body = document.createElement("div");
    body.className = "campaign-body";
    const title = document.createElement("div");
    title.className = "campaign-title";
    title.textContent = c.name;
    body.appendChild(title);
    const game = document.createElement("div");
    game.className = "campaign-game";
    game.textContent = c.game + " - " + c.claimed_drops + "/" + c.total_drops;
    body.appendChild(game);
    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("div");
    fill.style.width = pct(c.progress);
    bar.appendChild(fill);
    body.appendChild(bar);
    const thumbs = document.createElement("div");
    thumbs.className = "drop-thumbs";
    for (const d of c.drops) {
      const thumb = document.createElement("div");
      thumb.className = "drop-thumb" + (d.claimed ? " claimed" : "");
      thumb.title = d.rewards + (d.claimed ? " (" + t("claimed") + ")" : " " + pct(d.progress));
      const dimg = document.createElement("img");
      dimg.src = d.image_url || "";
      dimg.alt = "";
      thumb.appendChild(dimg);
      if (d.claimed) {
        const check = document.createElement("div");
        check.className = "check";
        check.textContent = "✓";
        thumb.appendChild(check);
      }
      thumbs.appendChild(thumb);
    }
    body.appendChild(thumbs);
    card.appendChild(body);
    list.appendChild(card);
  }
}

function renderEditList(listEl, emptyEl, games, kind, controlEnabled) {
  listEl.innerHTML = "";
  if (!games.length) {
    emptyEl.style.display = "block";
  } else {
    emptyEl.style.display = "none";
  }
  games.forEach((game, i) => {
    const li = document.createElement("li");
    li.className = "edit-item";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = game;
    li.appendChild(name);
    if (kind === "priority") {
      const up = document.createElement("button");
      up.className = "icon-btn";
      up.textContent = "▲";
      up.title = t("move_up");
      up.disabled = !controlEnabled || i === 0;
      up.addEventListener("click", async () => {
        await post("/api/priority/move", { game, direction: -1 });
        refresh();
      });
      li.appendChild(up);
      const down = document.createElement("button");
      down.className = "icon-btn";
      down.textContent = "▼";
      down.title = t("move_down");
      down.disabled = !controlEnabled || i === games.length - 1;
      down.addEventListener("click", async () => {
        await post("/api/priority/move", { game, direction: 1 });
        refresh();
      });
      li.appendChild(down);
    }
    const remove = document.createElement("button");
    remove.className = "icon-btn";
    remove.textContent = "×";
    remove.title = t("remove");
    remove.disabled = !controlEnabled;
    remove.addEventListener("click", async () => {
      await post(`/api/${kind}/remove`, { game });
      refresh();
    });
    li.appendChild(remove);
    listEl.appendChild(li);
  });
}

async function refreshCampaigns() {
  try {
    const res = await fetch(base + "/api/campaigns");
    if (!res.ok) return;
    const data = await res.json();
    allCampaigns = data.campaigns;
    applyCampaignFilters();
  } catch (e) { /* keep last known list on failure */ }
}

function applyCampaignFilters() {
  let list = allCampaigns;
  const query = document.getElementById("campaign-search").value.trim().toLowerCase();
  if (query) {
    list = list.filter(c =>
      c.name.toLowerCase().includes(query) || c.game.toLowerCase().includes(query)
    );
  }
  const sort = document.getElementById("campaign-sort").value;
  list = list.slice();
  if (sort === "recent") {
    list.sort((a, b) => new Date(b.starts_at) - new Date(a.starts_at));
  } else if (sort === "progress") {
    list.sort((a, b) => b.progress - a.progress);
  } else {
    list.sort((a, b) => (a.active === b.active ? 0 : a.active ? -1 : 1) || b.progress - a.progress);
  }
  renderCampaigns(list);
}

async function refresh() {
  try {
    const res = await fetch(base + "/api/state");
    if (!res.ok) throw new Error("bad response");
    const s = await res.json();
    document.getElementById("error-box").style.display = "none";

    const badge = document.getElementById("mode-badge");
    badge.textContent = s.control_enabled ? t("mode_control") : t("mode_view");
    const viewerBadge = document.getElementById("viewer-badge");
    if (s.show_viewers && s.viewer_count != null) {
      viewerBadge.style.display = "inline-block";
      viewerBadge.textContent = "\\ud83d\\udc41 " + s.viewer_count;
    } else {
      viewerBadge.style.display = "none";
    }
    const needsPassword = s.password_required && !password;
    document.getElementById("password-card").style.display = needsPassword ? "block" : "none";
    const controlsReady = s.control_enabled && !needsPassword;
    setControlsEnabled(controlsReady);
    document.getElementById("control-locked").style.display = s.control_enabled ? "none" : "block";
    document.getElementById("control-body").style.display = s.control_enabled ? "block" : "none";

    const dot = document.getElementById("status-dot");
    const text = document.getElementById("status-text");
    dot.className = "dot " + s.status;
    text.textContent = s.status === "paused" ? t("paused") : (s.status === "mining" ? t("mining") : t("idle"));
    lastPaused = s.paused;
    const toggleBtn = document.getElementById("toggle-btn");
    toggleBtn.textContent = s.paused ? t("resume") : t("pause");
    toggleBtn.className = "action" + (s.paused ? "" : " secondary");

    // tab icon, header icon, and accent color all follow the live status, cache-busted
    // so the browser actually refetches the icon instead of reusing the page-load one
    document.getElementById("favicon").href = base + "/favicon.ico?s=" + s.status;
    document.getElementById("app-logo").src = base + "/favicon.ico?s=" + s.status;
    const statusColor = { mining: "#2ecc71", paused: "#e0a800", idle: "#e05252" }[s.status] || "#9147ff";
    document.getElementById("theme-color-meta").content = statusColor;

    document.getElementById("resume-at-info").textContent = s.resume_at
      ? "Resumes automatically at " + new Date(s.resume_at).toLocaleTimeString()
      : "";

    const dropCard = document.getElementById("drop-card");
    if (s.current_drop) {
      dropCard.style.display = "block";
      document.getElementById("drop-image").src = s.current_drop.reward_image || s.current_drop.game_image || "";
      document.getElementById("drop-game").textContent = s.current_drop.game;
      document.getElementById("drop-rewards").textContent = s.current_drop.rewards;
      document.getElementById("drop-pct").textContent = pct(s.current_drop.drop_progress);
      document.getElementById("drop-bar").style.width = pct(s.current_drop.drop_progress);
      document.getElementById("campaign-pct").textContent =
        pct(s.current_drop.campaign_progress) + " (" + s.current_drop.claimed_drops +
        "/" + s.current_drop.total_drops + ")";
      document.getElementById("campaign-bar").style.width = pct(s.current_drop.campaign_progress);
      const dr = fmtMinutes(s.current_drop.drop_remaining_minutes);
      const cr = fmtMinutes(s.current_drop.campaign_remaining_minutes);
      document.getElementById("drop-remaining").innerHTML =
        dr ? t("drop_remaining") + ": <b>" + dr + "</b> " + t("remaining") : "";
      document.getElementById("campaign-remaining").innerHTML =
        cr ? t("campaign_remaining") + ": <b>" + cr + "</b> " + t("remaining") : "";
      const pctInt = Math.round(s.current_drop.drop_progress * 100);
      document.title = pctInt + "% - " + s.current_drop.rewards + " - DropStream";
    } else {
      dropCard.style.display = "none";
      document.title = "DropStream";
    }

    const channelCard = document.getElementById("channel-card");
    if (s.watching_channel) {
      channelCard.style.display = "block";
      let label = s.watching_channel.name;
      if (s.watching_channel.game) label += " - " + s.watching_channel.game;
      if (s.watching_channel.viewers != null) label += " (" + s.watching_channel.viewers + " " + t("viewers") + ")";
      document.getElementById("channel-name").textContent = label;
    } else {
      channelCard.style.display = "none";
    }

    document.getElementById("stat-total").textContent = s.stats.total_drops;
    document.getElementById("stat-hours").textContent = s.stats.hours_saved;

    if (!applyingMode) modeSelect.value = s.priority_mode.value;

    renderRankList(s.stats.per_game);
    renderEditList(
      document.getElementById("priority-edit-list"),
      document.getElementById("priority-empty"),
      s.priority_list, "priority", controlsReady
    );
    renderEditList(
      document.getElementById("exclude-edit-list"),
      document.getElementById("exclude-empty"),
      s.exclude_list, "exclude", controlsReady
    );

    const options = document.getElementById("game-options");
    options.innerHTML = "";
    for (const game of (s.available_games || [])) {
      const opt = document.createElement("option");
      opt.value = game;
      options.appendChild(opt);
    }
  } catch (e) {
    document.getElementById("error-box").style.display = "block";
  }
}

document.getElementById("unlock-btn").addEventListener("click", () => {
  password = document.getElementById("password-input").value;
  document.getElementById("password-err").style.display = "none";
  refresh();
});

document.getElementById("toggle-btn").addEventListener("click", async () => {
  await post(lastPaused ? "/api/resume" : "/api/pause");
  refresh();
});
document.getElementById("pause-timer-btn").addEventListener("click", async () => {
  const minutes = parseFloat(document.getElementById("pause-timer-select").value);
  await post("/api/pause_for", { minutes });
  refresh();
});
modeSelect.addEventListener("change", async () => {
  applyingMode = true;
  await post("/api/priority_mode", { mode: parseInt(modeSelect.value, 10) });
  applyingMode = false;
  refresh();
});
document.getElementById("priority-add-btn").addEventListener("click", async () => {
  const input = document.getElementById("priority-input");
  const game = input.value.trim();
  if (!game) return;
  await post("/api/priority/add", { game });
  input.value = "";
  refresh();
});
document.getElementById("exclude-add-btn").addEventListener("click", async () => {
  const input = document.getElementById("exclude-input");
  const game = input.value.trim();
  if (!game) return;
  await post("/api/exclude/add", { game });
  input.value = "";
  refresh();
});
document.getElementById("reload-btn").addEventListener("click", async () => {
  await post("/api/reload");
  refresh();
});
document.getElementById("campaign-search").addEventListener("input", applyCampaignFilters);
document.getElementById("campaign-sort").addEventListener("change", applyCampaignFilters);

applyStaticTranslations();
refresh();
refreshCampaigns();

// Battery/CPU optimization: a background tab has no reason to poll the API every few
// seconds, so intervals are cleared while hidden and a single catch-up refresh runs the
// moment the tab becomes visible again, instead of keeping timers ticking uselessly.
let refreshTimer = setInterval(refresh, 4000);
let campaignsTimer = setInterval(refreshCampaigns, 15000);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    clearInterval(refreshTimer);
    clearInterval(campaignsTimer);
    refreshTimer = null;
    campaignsTimer = null;
  } else if (!refreshTimer) {
    refresh();
    refreshCampaigns();
    refreshTimer = setInterval(refresh, 4000);
    campaignsTimer = setInterval(refreshCampaigns, 15000);
  }
});
</script>
</body>
</html>
"""
