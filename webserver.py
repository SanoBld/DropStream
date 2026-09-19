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
from logbuffer import console as log_buffer

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
            web.get(f"/{token}/api/stats", self._handle_stats),
            # always registered (unlike the control routes below), since it's gated by
            # the web_server_show_logs setting checked live inside the handler instead -
            # so toggling that setting takes effect immediately, without a server restart
            web.get(f"/{token}/api/logs", self._handle_logs),
        ]
        if settings.web_server_allow_control:
            routes += [
                web.post(f"/{token}/api/pause", self._handle_pause),
                web.post(f"/{token}/api/pause_for", self._handle_pause_for),
                web.post(f"/{token}/api/resume", self._handle_resume),
                web.post(f"/{token}/api/priority_mode", self._handle_priority_mode),
                web.post(f"/{token}/api/mine_unlinked", self._handle_mine_unlinked),
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
                try:
                    reward_image = drop.benefits[0].image_url if drop.benefits else None
                    drops.append({
                        "rewards": drop.rewards_text(),
                        "image_url": reward_image,
                        "progress": round(drop.progress, 4),
                        "claimed": drop.is_claimed,
                        # detail fields, used by the drop-detail popup opened by clicking
                        # a thumbnail on the Campaigns tab - kept separate from the fields
                        # above so the always-sent summary payload stays small
                        "benefits": [
                            {"name": b.name, "image_url": b.image_url} for b in drop.benefits
                        ],
                        "required_minutes": drop.required_minutes,
                        "current_minutes": drop.current_minutes,
                        "remaining_minutes": drop.remaining_minutes,
                    })
                except Exception:
                    # one malformed drop shouldn't take down the whole /api/campaigns response
                    logger.exception("Failed to build a campaign drop entry for the remote dashboard")
                    continue
                if len(drops) >= 12:
                    break
            acl = campaign.allowed_channels
            out.append({
                "game": campaign.game.name,
                "image_url": campaign.image_url,
                "name": campaign.name,
                "active": campaign.active,
                "progress": round(campaign.progress, 4),
                "claimed_drops": campaign.claimed_drops,
                "total_drops": campaign.total_drops,
                "starts_at": campaign.starts_at.isoformat(),
                "ends_at": campaign.ends_at.isoformat(),
                "linked": campaign.linked,
                "link_url": campaign.link_url,
                "allowed_channels": [ch.name for ch in acl],  # empty = all channels allowed
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
            # small preview of the campaign's other drops, shown next to the current one
            # on the Dashboard tab so visitors can see what's coming up without switching
            # to the Campaigns tab; capped since a campaign can have many drops
            other_drops = []
            for other in campaign.drops:
                if other.id == drop.id:
                    continue
                try:
                    other_drops.append({
                        "rewards": other.rewards_text(),
                        "image_url": other.benefits[0].image_url if other.benefits else None,
                        "claimed": other.is_claimed,
                        "progress": round(other.progress, 4),
                        "benefits": [
                            {"name": b.name, "image_url": b.image_url} for b in other.benefits
                        ],
                        "required_minutes": other.required_minutes,
                        "current_minutes": other.current_minutes,
                        "remaining_minutes": other.remaining_minutes,
                    })
                except Exception:
                    # one malformed drop shouldn't take down the whole dashboard state
                    logger.exception("Failed to build other_drops entry for the remote dashboard")
                    continue
                if len(other_drops) >= 8:
                    break
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
                "other_drops": other_drops,
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
        # prefer the box art saved at claim time (works for finished/no-longer-active
        # campaigns too), falling back to the current inventory for anything older
        game_images = dict(twitch.stats._game_images())
        game_images.update({c.game.name: c.image_url for c in twitch.inventory})
        per_game = [
            {"game": name, "count": count, "image_url": game_images.get(name)}
            for name, count in twitch.stats.drops_per_game()
        ]
        available_games = sorted({c.game.name for c in twitch.inventory})
        return {
            "app": {"name": "DropStream", "version": self._version()},
            "control_enabled": settings.web_server_allow_control,
            "logs_enabled": getattr(settings, "web_server_show_logs", False),
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
            "mine_unlinked_campaigns": settings.mine_unlinked_campaigns,
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
        # no-store: this page changes with every DropStream update, and browsers (mobile
        # ones especially) are eager to cache a plain HTML GET with no cache headers -
        # without this, visitors can keep seeing an old, broken version of the page after
        # an update until they clear their cache, even though the server is fully up to date
        return web.Response(
            text=DASHBOARD_HTML,
            content_type="text/html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

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
        try:
            return web.json_response(self._state_dict())
        except Exception as exc:
            # surface the real error instead of aiohttp's generic HTML 500 page, which
            # the frontend's res.json() can't parse and would otherwise fail silently,
            # leaving the whole dashboard stuck on its last (or default) values forever
            logger.exception("Failed to build the remote dashboard's state")
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    async def _handle_campaigns(self, request: web.Request) -> web.Response:
        try:
            return web.json_response({"campaigns": self._campaigns_list()})
        except Exception as exc:
            logger.exception("Failed to build the remote dashboard's campaigns list")
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    async def _handle_stats(self, request: web.Request) -> web.Response:
        range_key = request.query.get("range", "week")
        if range_key not in ("day", "week", "month", "3months", "all"):
            range_key = "week"
        try:
            return web.json_response(self._twitch.stats.stats_for_range(range_key))
        except Exception as exc:
            logger.exception("Failed to build the remote dashboard's stats")
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    async def _handle_logs(self, request: web.Request) -> web.Response:
        if not getattr(self._twitch.settings, "web_server_show_logs", False):
            return web.json_response({"error": "logs are disabled"}, status=403)
        return web.json_response({"lines": log_buffer.get_lines(limit=500)})

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

    async def _handle_mine_unlinked(self, request: web.Request) -> web.Response:
        if not self._password_ok(request):
            return web.json_response({"error": "wrong password"}, status=401)
        try:
            body = await request.json()
            self._twitch.settings.mine_unlinked_campaigns = bool(body["enabled"])
        except (json.JSONDecodeError, KeyError):
            return web.json_response({"error": "invalid value"}, status=400)
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
        self._twitch.force_reload()
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
.faq-item { border-top: 1px solid var(--border, #2a2a30); padding: 10px 0; }
.faq-item:first-of-type { margin-top: 8px; }
.faq-item summary { cursor: pointer; font-weight: 600; }
.faq-a { margin-top: 6px; line-height: 1.5; }
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
  @media (max-width: 600px) {
    body { padding: 10px; }
    .top-row { flex-direction: column; }
    .grid { grid-template-columns: 1fr; }
    .campaign-toolbar { flex-direction: column; }
    .campaign-toolbar select { width: 100%; }
    .campaign-card { flex-wrap: wrap; }
    .tab-bar { overflow-x: auto; -webkit-overflow-scrolling: touch; }
    .tab-btn { padding: 8px 10px; font-size: 12px; }
  }
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
  .rank-name {
    flex: 1; font-size: 13px; min-width: 0; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }
  .rank-count { font-size: 12px; color: var(--dim); flex-shrink: 0; }
  .stats-filter {
    display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px;
  }
  .stats-filter button {
    background: var(--input-bg); color: var(--fg); border: 1px solid var(--border);
    border-radius: 999px; padding: 6px 14px; font-size: 12px; cursor: pointer; white-space: nowrap;
  }
  .stats-filter button.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .stats-card-title {
    display: flex; justify-content: space-between; align-items: baseline; gap: 8px; flex-wrap: wrap;
  }
  .chart-wrap { position: relative; width: 100%; height: 220px; }
  .chart-wrap canvas { position: absolute; inset: 0; width: 100%; height: 100%; }
  .stats-empty {
    display: none; text-align: center; color: var(--dim); font-size: 13px; padding: 30px 0;
  }
  .switch { position: relative; display: inline-block; width: 40px; height: 22px; flex-shrink: 0; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .switch span {
    position: absolute; inset: 0; background: var(--input-bg); border: 1px solid var(--border);
    border-radius: 999px; cursor: pointer; transition: background .2s;
  }
  .switch span::before {
    content: ""; position: absolute; width: 16px; height: 16px; left: 2px; top: 2px;
    background: var(--fg); border-radius: 50%; transition: transform .2s;
  }
  .switch input:checked + span { background: var(--accent); border-color: var(--accent); }
  .switch input:checked + span::before { transform: translateX(18px); background: #fff; }
  .switch input:disabled + span { opacity: .5; cursor: not-allowed; }
  .link-hover { cursor: pointer; text-decoration: none; color: inherit; }
  .link-hover:hover { text-decoration: underline; }
  .drop-thumb-pct {
    position: absolute; bottom: 2px; right: 2px; font-size: 10px; font-weight: 600;
    background: rgba(0,0,0,.65); color: #fff; padding: 1px 4px; border-radius: 4px;
  }
  .drop-thumb { position: relative; }
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
  .drop-thumb { cursor: pointer; }
  .other-drops-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .other-drop-thumb {
    position: relative; width: 28px; height: 28px; cursor: pointer; flex-shrink: 0;
  }
  .other-drop-thumb img {
    width: 28px; height: 28px; border-radius: 6px; object-fit: cover; background: var(--input-bg);
    display: block;
  }
  .other-drop-thumb.claimed img { opacity: .45; }
  .logs-box {
    background: var(--card2); border-radius: 8px; padding: 10px; font-family: monospace;
    font-size: 11px; line-height: 1.5; white-space: pre-wrap; word-break: break-word;
    max-height: 65vh; overflow-y: auto; margin: 0;
  }
  .modal-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,.6); display: flex;
    align-items: center; justify-content: center; z-index: 50; padding: 16px;
  }
  .modal {
    position: relative; background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px; max-width: 360px; width: 100%; text-align: center;
  }
  .modal-close {
    position: absolute; top: 8px; right: 8px; width: 26px; height: 26px;
  }
  .modal img { width: 80px; height: 80px; border-radius: 8px; object-fit: cover; background: var(--input-bg); }
  .drop-modal-benefits {
    display: flex; flex-wrap: wrap; gap: 6px; margin-top: 14px; justify-content: center;
  }
  .drop-modal-benefits img {
    width: 40px; height: 40px; border-radius: 6px; object-fit: cover; background: var(--input-bg);
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
  .campaign-progress-row { display: flex; align-items: center; gap: 8px; cursor: pointer; }
  .campaign-progress-row:hover .campaign-pct { text-decoration: underline; }
  .campaign-progress-row .bar { flex: 1; }
  .campaign-pct { font-size: 12px; color: var(--dim); white-space: nowrap; }
  .campaign-details { display: none; margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border); font-size: 12px; }
  .campaign-details.open { display: block; }
  .campaign-details .row { align-items: flex-start; margin-bottom: 6px; }
  .link-status { text-decoration: none; font-weight: 600; }
  .link-status.linked { color: var(--green); }
  .link-status.not-linked { color: var(--red); cursor: pointer; }
  .link-status.not-linked:hover { text-decoration: underline; }
  .campaign-title-row { display: flex; align-items: center; gap: 6px; }
  .unlinked-dot {
    width: 8px; height: 8px; border-radius: 50%; background: var(--red); flex-shrink: 0;
  }
  .expand-arrow {
    margin-left: auto; border: none; background: transparent; color: var(--dim); cursor: pointer;
    font-size: 12px; padding: 2px 4px; transition: transform .15s ease; flex-shrink: 0;
  }
  .expand-arrow.open { transform: rotate(180deg); }
  .link-status.not-linked-hint { font-size: 11px; color: var(--dim); margin-left: 6px; }

/* links use the accent color (purple) instead of the browser default blue */
a, a:visited { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* Light animations. Only opacity/transform are animated (GPU-composited, no layout work),
   every animation runs once and stops (no infinite loops, no JS timers or rAF), so there is
   no measurable CPU/RAM/battery cost. Disabled entirely for users who prefer reduced motion. */
@keyframes ds-rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@keyframes ds-fade { from { opacity: 0; } to { opacity: 1; } }
.tab-panel.active > .card { animation: ds-rise .28s ease-out both; }
.tab-panel.active > .card:nth-child(2) { animation-delay: .04s; }
.tab-panel.active > .card:nth-child(3) { animation-delay: .08s; }
.tab-panel.active > .card:nth-child(4) { animation-delay: .12s; }
.tab-panel.active > .card:nth-child(n+5) { animation-delay: .16s; }
.faq-item[open] .faq-a { animation: ds-fade .25s ease-out; }
.tab-btn, .theme-switch button, .stats-filter button, a { transition: color .15s, background-color .15s, border-color .15s; }
.card { transition: border-color .2s; }
.card:hover { border-color: var(--accent); }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
}
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
    <button class="tab-btn" data-tab="stats" data-i18n="tab_stats"></button>
    <button class="tab-btn" data-tab="control" id="control-tab-btn" data-i18n="tab_control"></button>
    <button class="tab-btn" data-tab="logs" id="logs-tab-btn" data-i18n="tab_logs" style="display:none"></button>
    <button class="tab-btn" data-tab="help" data-i18n="tab_help"></button>
  </div>

  <div class="tab-panel active" id="tab-dashboard">
    <div class="card" id="pause-card">
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
      <div class="other-drops-row" id="other-drops-row"></div>
    </div>

    <div class="card" id="channel-card" style="display:none">
      <div class="label" data-i18n="watching"></div>
      <a class="value link-hover" id="channel-name" target="_blank" rel="noopener"></a>
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
        <select id="campaign-link-filter">
          <option value="all" data-i18n="filter_all"></option>
          <option value="linked" data-i18n="filter_linked_only"></option>
          <option value="not_linked" data-i18n="filter_not_linked_only"></option>
        </select>
      </div>
      <div class="label" data-i18n="campaigns_title"></div>
      <div class="campaign-list" id="campaign-list"></div>
      <div class="muted" id="no-campaigns" data-i18n="no_campaigns" style="display:none"></div>
    </div>
  </div>

  <div class="tab-panel" id="tab-stats">
    <div class="stats-filter" id="stats-filter">
      <button data-range="day" data-i18n="range_day"></button>
      <button data-range="week" data-i18n="range_week"></button>
      <button data-range="month" data-i18n="range_month"></button>
      <button data-range="3months" data-i18n="range_3months"></button>
      <button data-range="all" data-i18n="range_all"></button>
    </div>

    <div class="grid">
      <div class="card">
        <div class="label" data-i18n="total_drops"></div>
        <div class="value" id="stat-range-total" style="font-size:22px">-</div>
      </div>
      <div class="card">
        <div class="label" data-i18n="hours_saved"></div>
        <div class="value" id="stat-range-hours" style="font-size:22px">-</div>
      </div>
    </div>

    <div class="card">
      <div class="stats-card-title">
        <div class="label" id="chart-drops-title" data-i18n="stats_drops_title"></div>
      </div>
      <div class="chart-wrap"><canvas id="chart-weekly"></canvas></div>
      <div class="stats-empty" id="chart-drops-empty" data-i18n="stats_no_data"></div>
    </div>
    <div class="card">
      <div class="label" id="chart-hours-title" data-i18n="stats_hours_title"></div>
      <div class="chart-wrap"><canvas id="chart-hours"></canvas></div>
      <div class="stats-empty" id="chart-hours-empty" data-i18n="stats_no_data"></div>
    </div>
    <div class="card">
      <div class="label" data-i18n="drops_per_game_title"></div>
      <ul class="rank-list" id="rank-list-stats"></ul>
      <div class="stats-empty" id="rank-list-empty" data-i18n="stats_no_data"></div>
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
        <div class="row">
          <div class="label" data-i18n="mine_unlinked"></div>
          <label class="switch">
            <input type="checkbox" id="mine-unlinked-toggle">
            <span></span>
          </label>
        </div>
        <div class="muted" data-i18n="mine_unlinked_hint"></div>
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

  <div class="tab-panel" id="tab-logs">
    <div class="card">
      <div class="label" data-i18n="logs_title"></div>
      <div class="muted" data-i18n="logs_hint" style="margin-bottom:8px"></div>
      <pre id="logs-box" class="logs-box"></pre>
    </div>
  </div>

  <div class="tab-panel" id="tab-help">
    <div class="card">
      <div class="label" data-i18n="help_about_title"></div>
      <div class="muted" style="margin-top:6px">
        <span data-i18n="help_about_body"></span>
        <a href="https://github.com/DevilXD/TwitchDropsMiner" target="_blank" rel="noopener">DevilXD/TwitchDropsMiner</a>
      </div>
      <div class="muted" style="margin-top:10px" data-i18n="help_version_label"></div>
      <div class="value" id="help-version"></div>
    </div>
    <div class="card">
      <div class="label" data-i18n="help_how_title"></div>
      <div class="muted" style="margin-top:6px" data-i18n="help_how_body"></div>
    </div>
    <div class="card">
      <div class="label" data-i18n="faq_title"></div>
      <details class="faq-item">
        <summary data-i18n="faq_q1"></summary>
        <div class="muted faq-a" data-i18n="faq_a1"></div>
      </details>
      <details class="faq-item">
        <summary data-i18n="faq_q2"></summary>
        <div class="muted faq-a" data-i18n="faq_a2"></div>
      </details>
      <details class="faq-item">
        <summary data-i18n="faq_q3"></summary>
        <div class="muted faq-a" data-i18n="faq_a3"></div>
      </details>
      <details class="faq-item">
        <summary data-i18n="faq_q4"></summary>
        <div class="muted faq-a" data-i18n="faq_a4"></div>
      </details>
      <details class="faq-item">
        <summary data-i18n="faq_q5"></summary>
        <div class="muted faq-a" data-i18n="faq_a5"></div>
      </details>
      <details class="faq-item">
        <summary data-i18n="faq_q6"></summary>
        <div class="muted faq-a" data-i18n="faq_a6"></div>
      </details>
      <details class="faq-item">
        <summary data-i18n="faq_q7"></summary>
        <div class="muted faq-a" data-i18n="faq_a7"></div>
      </details>
      <details class="faq-item">
        <summary data-i18n="faq_q8"></summary>
        <div class="muted faq-a" data-i18n="faq_a8"></div>
      </details>
    </div>
    <div class="card">
      <div class="label" data-i18n="help_links_title"></div>
      <div class="muted" style="margin-top:6px">
        <a href="https://www.twitch.tv/drops/inventory" target="_blank" rel="noopener" data-i18n="help_link_inventory"></a><br>
        <a href="https://www.twitch.tv/drops/campaigns" target="_blank" rel="noopener" data-i18n="help_link_campaigns"></a><br>
        <a href="https://github.com/SanoBld/DropStream" target="_blank" rel="noopener" data-i18n="help_link_repo"></a>
      </div>
    </div>
  </div>

  <div class="modal-overlay" id="drop-modal-overlay" style="display:none">
    <div class="modal">
      <button class="icon-btn modal-close" id="drop-modal-close">×</button>
      <img id="drop-modal-image" src="" alt="">
      <div class="value" id="drop-modal-title" style="margin-top:8px"></div>
      <div class="muted" id="drop-modal-status" style="margin-top:4px"></div>
      <div class="bar" style="margin-top:10px"><div id="drop-modal-bar" style="width:0%"></div></div>
      <div class="muted" id="drop-modal-minutes" style="margin-top:4px"></div>
      <div id="drop-modal-benefits" class="drop-modal-benefits"></div>
    </div>
  </div>

  <datalist id="game-options"></datalist>

  <div class="err" id="error-box" data-i18n="connection_lost"></div>
</div>

<script>
const I18N = {
  "en": {
    "faq_title": "Questions & answers",
    "faq_q1": "Does DropStream actually watch streams?",
    "faq_a1": "No. It only asks Twitch for a stream's metadata every few seconds, which is enough for Twitch to count progress toward a drop. No video or audio is downloaded, so it barely uses any bandwidth.",
    "faq_q2": "Why is my progress stuck or wrong?",
    "faq_a2": "Most likely the same Twitch account is watching a stream somewhere else, in a browser for example. Twitch handles drop progress on its side, so this confuses the miner. Avoid watching other streams on that account while it mines.",
    "faq_q3": "What can I do from this page?",
    "faq_a3": "In view-only mode you can follow the current drop, the campaigns, your stats and the logs. In control mode you can also pause or resume, set a pause timer, and edit the priority and exclude lists. This page can't log in to your account or claim drops on its own, it only mirrors and steers the desktop app.",
    "faq_q4": "How do I choose which games get mined?",
    "faq_a4": "Add games to the priority list, pick a priority mode, then press Reload so the app applies the changes. With any mode other than priority list only, the miner also takes other available campaigns. Games in the exclude list are ignored.",
    "faq_q5": "A game I want doesn't show up. Why?",
    "faq_a5": "Your Twitch account has to be linked to that game on Twitch's campaigns page. Once it's linked, press Reload and the campaign can be picked up.",
    "faq_q6": "Is the link private? Can I use it away from home?",
    "faq_a6": "Anyone with the link can see your instance, so treat it like a password. If you shared it by mistake, generate a new one in the desktop app. By default it only works on your local network. To reach it from outside you need port forwarding or a VPN like Tailscale or WireGuard.",
    "faq_q7": "Is the remaining time exact?",
    "faq_a7": "No, it's an approximation. It counts down minute by minute and resets when the app gets the real value from Twitch. It has no effect on how fast you mine.",
    "faq_q8": "What are the logs?",
    "faq_a8": "The same lines you see in the Output box of the desktop app, in read-only mode.",
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
    "stats_weekly_title": "Last 7 days",
    "drops_per_game_title": "Drops per game",
    "campaigns_title": "Drop campaigns",
    "campaign_search_placeholder": "Search campaigns or games...",
    "sort_default": "Default",
    "sort_recent": "Most recent",
    "sort_progress": "Progress",
    "claimed": "Claimed",
    "account_status": "Account link",
    "linked": "Linked",
    "not_linked": "Not linked - click to link",
    "connect_account_hint": "Connect your account to earn this drop",
    "show_details": "Show details",
    "filter_all": "All accounts",
    "filter_linked_only": "Linked only",
    "filter_not_linked_only": "Not linked only",
    "allowed_channels": "Allowed channels",
    "all_channels": "All channels",
    "ends_at": "Ends",
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
    "tab_stats": "Stats",
    "tab_control": "Control",
    "tab_logs": "Logs",
    "logs_title": "Application logs",
    "logs_hint": "Read-only, live view of this instance's recent log output.",
    "tab_help": "Help",
    "help_about_title": "About DropStream",
    "help_about_body": "DropStream is an unofficial fork of Twitch Drops Miner, built by SanoBld. The drop-mining engine itself comes from the original project:",
    "help_version_label": "Version",
    "help_how_title": "How it works",
    "help_how_body": "Every few seconds, the app requests a stream's metadata instead of actually watching it, which is enough for Twitch to count progress toward a drop. This dashboard just mirrors and, if you're given control access, steers what the desktop app is already doing - it can't log into your account or claim drops on its own.",
    "help_links_title": "Useful links",
    "help_link_inventory": "View your Twitch inventory",
    "help_link_campaigns": "View and manage linked campaigns",
    "help_link_repo": "DropStream on GitHub",
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
    "exclude_list": "Exclude list",
    "range_day": "Today",
    "range_week": "7 days",
    "range_month": "30 days",
    "range_3months": "3 months",
    "range_all": "All time",
    "stats_drops_title": "Drops claimed",
    "stats_hours_title": "Watch hours saved",
    "stats_no_data": "No data for this period.",
    "mine_unlinked": "Mine unlinked campaigns",
    "mine_unlinked_hint": "Also mine drops when Twitch reports the game account as not linked."
  },
  "fr": {
    "faq_title": "Questions et réponses",
    "faq_q1": "DropStream regarde-t-il vraiment les streams ?",
    "faq_a1": "Non. Il demande seulement à Twitch les métadonnées d'un stream toutes les quelques secondes, ce qui suffit pour que Twitch fasse avancer un drop. Aucune vidéo ni aucun son n'est téléchargé, donc la consommation de bande passante est minime.",
    "faq_q2": "Pourquoi ma progression est bloquée ou fausse ?",
    "faq_a2": "Le plus souvent, le même compte Twitch regarde un stream ailleurs, dans un navigateur par exemple. Twitch gère la progression des drops de son côté, et ça perturbe le mineur. Évitez de regarder d'autres streams avec ce compte pendant le minage.",
    "faq_q3": "Que puis-je faire depuis cette page ?",
    "faq_a3": "En mode lecture seule, vous suivez le drop en cours, les campagnes, vos stats et les journaux. En mode contrôle, vous pouvez aussi mettre en pause ou reprendre, régler un minuteur de pause et modifier les listes de priorité et d'exclusion. Cette page ne peut ni se connecter à votre compte ni réclamer de drops seule, elle reflète et pilote l'application de bureau.",
    "faq_q4": "Comment choisir les jeux à miner ?",
    "faq_a4": "Ajoutez des jeux à la liste de priorité, choisissez un mode de priorité, puis appuyez sur Recharger pour que l'application applique les changements. Avec un mode autre que la liste de priorité seule, le mineur prend aussi les autres campagnes disponibles. Les jeux de la liste d'exclusion sont ignorés.",
    "faq_q5": "Un jeu que je veux n'apparaît pas. Pourquoi ?",
    "faq_a5": "Votre compte Twitch doit être lié à ce jeu sur la page des campagnes de Twitch. Une fois lié, appuyez sur Recharger et la campagne pourra être prise en compte.",
    "faq_q6": "Le lien est-il privé ? Puis-je l'utiliser hors de chez moi ?",
    "faq_a6": "Toute personne qui a le lien peut voir votre instance, traitez-le donc comme un mot de passe. Si vous l'avez partagé par erreur, générez-en un nouveau dans l'application de bureau. Par défaut, il ne fonctionne que sur votre réseau local. Pour y accéder de l'extérieur, il faut une redirection de port ou un VPN comme Tailscale ou WireGuard.",
    "faq_q7": "Le temps restant est-il exact ?",
    "faq_a7": "Non, c'est une approximation. Il décompte minute par minute et se recale quand l'application récupère la vraie valeur chez Twitch. Ça n'a aucun effet sur la vitesse de minage.",
    "faq_q8": "Que sont les journaux ?",
    "faq_a8": "Les mêmes lignes que dans la zone Sortie de l'application de bureau, en lecture seule.",
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
    "stats_weekly_title": "7 derniers jours",
    "drops_per_game_title": "Drops par jeu",
    "campaigns_title": "Campagnes de drops",
    "campaign_search_placeholder": "Rechercher une campagne ou un jeu...",
    "sort_default": "Par défaut",
    "sort_recent": "Plus récent",
    "sort_progress": "Progression",
    "claimed": "Récupéré",
    "account_status": "Lien du compte",
    "linked": "Lié",
    "not_linked": "Non lié - cliquer pour lier",
    "connect_account_hint": "Liez votre compte pour obtenir ce drop",
    "show_details": "Voir les détails",
    "filter_all": "Tous les comptes",
    "filter_linked_only": "Liés uniquement",
    "filter_not_linked_only": "Non liés uniquement",
    "allowed_channels": "Chaînes autorisées",
    "all_channels": "Toutes les chaînes",
    "ends_at": "Se termine",
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
    "tab_stats": "Statistiques",
    "tab_control": "Contrôle",
    "tab_logs": "Journaux",
    "logs_title": "Journaux de l'application",
    "logs_hint": "Vue en lecture seule des dernières lignes de log de cette instance.",
    "tab_help": "Aide",
    "help_about_title": "À propos de DropStream",
    "help_about_body": "DropStream est un fork non officiel de Twitch Drops Miner, réalisé par SanoBld. Le moteur de minage des drops lui-même vient du projet d'origine :",
    "help_version_label": "Version",
    "help_how_title": "Comment ça fonctionne",
    "help_how_body": "Toutes les quelques secondes, l'application récupère les métadonnées d'un flux au lieu de le regarder réellement, ce qui suffit à Twitch pour faire progresser un drop. Ce tableau de bord se contente de refléter, et si on vous donne l'accès contrôle, de piloter ce que l'application de bureau fait déjà - il ne peut ni se connecter à votre compte ni réclamer de drops de lui-même.",
    "help_links_title": "Liens utiles",
    "help_link_inventory": "Voir votre inventaire Twitch",
    "help_link_campaigns": "Voir et gérer les campagnes liées",
    "help_link_repo": "DropStream sur GitHub",
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
    "exclude_list": "Liste d'exclusion",
    "range_day": "Aujourd'hui",
    "range_week": "7 jours",
    "range_month": "30 jours",
    "range_3months": "3 mois",
    "range_all": "Depuis le début",
    "stats_drops_title": "Drops récupérés",
    "stats_hours_title": "Heures de visionnage économisées",
    "stats_no_data": "Aucune donnée sur cette période.",
    "mine_unlinked": "Miner les campagnes non liées",
    "mine_unlinked_hint": "Miner aussi les drops quand Twitch indique que le compte du jeu n'est pas lié."
  },
  "de": {
    "faq_title": "Fragen und Antworten",
    "faq_q1": "Schaut DropStream wirklich Streams an?",
    "faq_a1": "Nein. Es fragt nur alle paar Sekunden die Metadaten eines Streams bei Twitch ab, und das reicht Twitch, um den Fortschritt eines Drops zu zählen. Es werden weder Video noch Audio geladen, der Datenverbrauch ist also minimal.",
    "faq_q2": "Warum hängt mein Fortschritt oder ist falsch?",
    "faq_a2": "Meistens schaut dasselbe Twitch-Konto woanders einen Stream, zum Beispiel im Browser. Twitch verwaltet den Drop-Fortschritt auf seiner Seite, und das bringt den Miner durcheinander. Schau mit diesem Konto keine anderen Streams, solange es minet.",
    "faq_q3": "Was kann ich auf dieser Seite tun?",
    "faq_a3": "Im Nur-Ansicht-Modus siehst du den aktuellen Drop, die Kampagnen, deine Statistiken und die Protokolle. Im Steuerungsmodus kannst du außerdem pausieren oder fortsetzen, einen Pausen-Timer setzen und die Prioritäts- und Ausschlusslisten bearbeiten. Die Seite kann sich nicht selbst in dein Konto einloggen oder Drops einlösen, sie spiegelt und steuert nur die Desktop-App.",
    "faq_q4": "Wie wähle ich, welche Spiele gemint werden?",
    "faq_a4": "Füge Spiele zur Prioritätsliste hinzu, wähle einen Prioritätsmodus und drücke dann auf Neu laden, damit die App die Änderungen übernimmt. Bei jedem Modus außer nur Prioritätsliste nimmt der Miner auch andere verfügbare Kampagnen. Spiele auf der Ausschlussliste werden ignoriert.",
    "faq_q5": "Ein gewünschtes Spiel taucht nicht auf. Warum?",
    "faq_a5": "Dein Twitch-Konto muss auf der Kampagnenseite von Twitch mit diesem Spiel verknüpft sein. Danach auf Neu laden drücken, dann kann die Kampagne berücksichtigt werden.",
    "faq_q6": "Ist der Link privat? Kann ich ihn unterwegs nutzen?",
    "faq_a6": "Jeder mit dem Link kann deine Instanz sehen, behandle ihn also wie ein Passwort. Wenn du ihn versehentlich geteilt hast, erzeuge in der Desktop-App einen neuen. Standardmäßig funktioniert er nur im lokalen Netzwerk. Von außen brauchst du Portweiterleitung oder ein VPN wie Tailscale oder WireGuard.",
    "faq_q7": "Ist die verbleibende Zeit genau?",
    "faq_a7": "Nein, sie ist eine Schätzung. Sie zählt Minute für Minute herunter und wird angepasst, sobald die App den echten Wert von Twitch bekommt. Auf die Mining-Geschwindigkeit hat sie keinen Einfluss.",
    "faq_q8": "Was sind die Protokolle?",
    "faq_a8": "Dieselben Zeilen wie im Ausgabe-Feld der Desktop-App, nur zum Lesen.",
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
    "stats_weekly_title": "Letzte 7 Tage",
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
    "tab_stats": "Statistiken",
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
    "connect_account_hint": "Verbinde dein Konto, um diesen Drop zu erhalten",
    "show_details": "Details anzeigen",
    "filter_all": "Alle Konten",
    "filter_linked_only": "Nur verknüpfte",
    "filter_not_linked_only": "Nur nicht verknüpfte",
    "exclude_list": "Ausschlussliste",
    "account_status": "Kontostatus",
    "all_channels": "Alle Kanäle",
    "allowed_channels": "Erlaubte Kanäle",
    "ends_at": "Endet am",
    "linked": "Verknüpft",
    "not_linked": "Nicht verknüpft",
    "mine_unlinked": "Nicht verknüpfte Kampagnen minen",
    "mine_unlinked_hint": "Auch Drops minen, wenn Twitch das Spielkonto als nicht verknüpft meldet.",
    "range_day": "Heute",
    "range_week": "7 Tage",
    "range_month": "30 Tage",
    "range_3months": "3 Monate",
    "range_all": "Gesamt",
    "stats_drops_title": "Erhaltene Drops",
    "stats_hours_title": "Gesparte Stunden",
    "stats_no_data": "Keine Daten für diesen Zeitraum."
  },
  "es": {
    "faq_title": "Preguntas y respuestas",
    "faq_q1": "¿DropStream ve los streams de verdad?",
    "faq_a1": "No. Solo le pide a Twitch los metadatos de un stream cada pocos segundos, y eso basta para que Twitch cuente el progreso de un drop. No se descarga vídeo ni audio, así que casi no gasta ancho de banda.",
    "faq_q2": "¿Por qué mi progreso está bloqueado o es incorrecto?",
    "faq_a2": "Lo más probable es que la misma cuenta de Twitch esté viendo un stream en otro sitio, por ejemplo en el navegador. Twitch gestiona el progreso de los drops por su lado y eso confunde al minero. Evita ver otros streams con esa cuenta mientras mina.",
    "faq_q3": "¿Qué puedo hacer desde esta página?",
    "faq_a3": "En modo solo lectura puedes seguir el drop actual, las campañas, tus estadísticas y los registros. En modo control también puedes pausar o reanudar, poner un temporizador de pausa y editar las listas de prioridad y exclusión. Esta página no puede iniciar sesión en tu cuenta ni reclamar drops por sí sola, solo refleja y dirige la aplicación de escritorio.",
    "faq_q4": "¿Cómo elijo qué juegos se minan?",
    "faq_a4": "Añade juegos a la lista de prioridad, elige un modo de prioridad y pulsa Recargar para que la aplicación aplique los cambios. Con cualquier modo distinto de solo lista de prioridad, el minero también toma otras campañas disponibles. Los juegos de la lista de exclusión se ignoran.",
    "faq_q5": "Un juego que quiero no aparece. ¿Por qué?",
    "faq_a5": "Tu cuenta de Twitch debe estar vinculada a ese juego en la página de campañas de Twitch. Cuando lo esté, pulsa Recargar y la campaña podrá tomarse en cuenta.",
    "faq_q6": "¿El enlace es privado? ¿Puedo usarlo fuera de casa?",
    "faq_a6": "Cualquiera con el enlace puede ver tu instancia, así que trátalo como una contraseña. Si lo compartiste por error, genera uno nuevo en la aplicación de escritorio. Por defecto solo funciona en tu red local. Para entrar desde fuera necesitas redirección de puertos o una VPN como Tailscale o WireGuard.",
    "faq_q7": "¿El tiempo restante es exacto?",
    "faq_a7": "No, es una aproximación. Cuenta hacia atrás minuto a minuto y se reajusta cuando la aplicación recibe el valor real de Twitch. No afecta a la velocidad de minado.",
    "faq_q8": "¿Qué son los registros?",
    "faq_a8": "Las mismas líneas que ves en el cuadro Salida de la aplicación de escritorio, en modo solo lectura.",
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
    "stats_weekly_title": "Últimos 7 días",
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
    "tab_stats": "Estadísticas",
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
    "connect_account_hint": "Vincula tu cuenta para conseguir este drop",
    "show_details": "Mostrar detalles",
    "filter_all": "Todas las cuentas",
    "filter_linked_only": "Solo vinculadas",
    "filter_not_linked_only": "Solo no vinculadas",
    "exclude_list": "Lista de exclusión",
    "account_status": "Estado de la cuenta",
    "all_channels": "Todos los canales",
    "allowed_channels": "Canales permitidos",
    "ends_at": "Finaliza el",
    "linked": "Vinculada",
    "not_linked": "No vinculada",
    "mine_unlinked": "Minar campañas no vinculadas",
    "mine_unlinked_hint": "Minar drops también cuando Twitch indica que la cuenta del juego no está vinculada.",
    "range_day": "Hoy",
    "range_week": "7 días",
    "range_month": "30 días",
    "range_3months": "3 meses",
    "range_all": "Todo",
    "stats_drops_title": "Drops obtenidos",
    "stats_hours_title": "Horas de visionado ahorradas",
    "stats_no_data": "Sin datos para este período."
  },
  "it": {
    "faq_title": "Domande e risposte",
    "faq_q1": "DropStream guarda davvero gli stream?",
    "faq_a1": "No. Chiede a Twitch solo i metadati di uno stream ogni pochi secondi, e a Twitch basta per contare l'avanzamento di un drop. Non viene scaricato né video né audio, quindi usa pochissima banda.",
    "faq_q2": "Perché il mio avanzamento è fermo o sbagliato?",
    "faq_a2": "Molto probabilmente lo stesso account Twitch sta guardando uno stream altrove, ad esempio nel browser. Twitch gestisce l'avanzamento dei drop dal suo lato e questo manda in confusione il miner. Evita di guardare altri stream con quell'account mentre mina.",
    "faq_q3": "Cosa posso fare da questa pagina?",
    "faq_a3": "In modalità sola visualizzazione puoi seguire il drop corrente, le campagne, le statistiche e i log. In modalità controllo puoi anche mettere in pausa o riprendere, impostare un timer di pausa e modificare le liste di priorità ed esclusione. La pagina non può accedere al tuo account né riscattare drop da sola, rispecchia e guida solo l'app desktop.",
    "faq_q4": "Come scelgo quali giochi minare?",
    "faq_a4": "Aggiungi giochi alla lista di priorità, scegli una modalità di priorità e premi Ricarica perché l'app applichi le modifiche. Con qualsiasi modalità diversa da solo lista di priorità, il miner prende anche le altre campagne disponibili. I giochi nella lista di esclusione vengono ignorati.",
    "faq_q5": "Un gioco che voglio non compare. Perché?",
    "faq_a5": "Il tuo account Twitch deve essere collegato a quel gioco nella pagina delle campagne di Twitch. Una volta collegato, premi Ricarica e la campagna potrà essere presa in considerazione.",
    "faq_q6": "Il link è privato? Posso usarlo fuori casa?",
    "faq_a6": "Chiunque abbia il link può vedere la tua istanza, quindi trattalo come una password. Se l'hai condiviso per errore, generane uno nuovo nell'app desktop. Di default funziona solo sulla rete locale. Da fuori servono il port forwarding o una VPN come Tailscale o WireGuard.",
    "faq_q7": "Il tempo rimanente è esatto?",
    "faq_a7": "No, è un'approssimazione. Scala minuto per minuto e si riallinea quando l'app riceve il valore reale da Twitch. Non influisce sulla velocità di mining.",
    "faq_q8": "Cosa sono i log?",
    "faq_a8": "Le stesse righe che vedi nella casella Output dell'app desktop, in sola lettura.",
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
    "stats_weekly_title": "Ultimi 7 giorni",
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
    "tab_stats": "Statistiche",
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
    "connect_account_hint": "Collega il tuo account per ottenere questo drop",
    "show_details": "Mostra dettagli",
    "filter_all": "Tutti gli account",
    "filter_linked_only": "Solo collegati",
    "filter_not_linked_only": "Solo non collegati",
    "exclude_list": "Lista di esclusione",
    "account_status": "Stato account",
    "all_channels": "Tutti i canali",
    "allowed_channels": "Canali consentiti",
    "ends_at": "Termina il",
    "linked": "Collegato",
    "not_linked": "Non collegato",
    "mine_unlinked": "Estrai campagne non collegate",
    "mine_unlinked_hint": "Estrai i drop anche quando Twitch segnala che l'account di gioco non è collegato.",
    "range_day": "Oggi",
    "range_week": "7 giorni",
    "range_month": "30 giorni",
    "range_3months": "3 mesi",
    "range_all": "Sempre",
    "stats_drops_title": "Drop ottenuti",
    "stats_hours_title": "Ore di visione risparmiate",
    "stats_no_data": "Nessun dato per questo periodo."
  },
  "pt": {
    "faq_title": "Perguntas e respostas",
    "faq_q1": "O DropStream assiste mesmo às transmissões?",
    "faq_a1": "Não. Ele só pede à Twitch os metadados de uma transmissão a cada poucos segundos, e isso basta para a Twitch contar o progresso de um drop. Nenhum vídeo ou áudio é baixado, então quase não gasta banda.",
    "faq_q2": "Por que meu progresso está travado ou errado?",
    "faq_a2": "Provavelmente a mesma conta da Twitch está assistindo a uma transmissão em outro lugar, no navegador por exemplo. A Twitch controla o progresso dos drops do lado dela e isso confunde o minerador. Evite assistir a outras transmissões com essa conta enquanto ele minera.",
    "faq_q3": "O que posso fazer nesta página?",
    "faq_a3": "No modo somente leitura você acompanha o drop atual, as campanhas, as estatísticas e os logs. No modo controle também pode pausar ou retomar, definir um temporizador de pausa e editar as listas de prioridade e exclusão. A página não consegue entrar na sua conta nem resgatar drops sozinha, apenas espelha e conduz o aplicativo de desktop.",
    "faq_q4": "Como escolho quais jogos são minerados?",
    "faq_a4": "Adicione jogos à lista de prioridade, escolha um modo de prioridade e pressione Recarregar para o aplicativo aplicar as mudanças. Com qualquer modo diferente de somente lista de prioridade, o minerador também pega outras campanhas disponíveis. Jogos da lista de exclusão são ignorados.",
    "faq_q5": "Um jogo que eu quero não aparece. Por quê?",
    "faq_a5": "Sua conta da Twitch precisa estar vinculada a esse jogo na página de campanhas da Twitch. Depois de vincular, pressione Recarregar e a campanha poderá ser considerada.",
    "faq_q6": "O link é privado? Posso usar fora de casa?",
    "faq_a6": "Qualquer pessoa com o link pode ver sua instância, então trate-o como uma senha. Se compartilhou por engano, gere um novo no aplicativo de desktop. Por padrão só funciona na sua rede local. Para acessar de fora é preciso redirecionar portas ou usar uma VPN como Tailscale ou WireGuard.",
    "faq_q7": "O tempo restante é exato?",
    "faq_a7": "Não, é uma aproximação. Ele conta minuto a minuto e se ajusta quando o aplicativo recebe o valor real da Twitch. Não interfere na velocidade da mineração.",
    "faq_q8": "O que são os logs?",
    "faq_a8": "As mesmas linhas da caixa Saída do aplicativo de desktop, em modo somente leitura.",
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
    "stats_weekly_title": "Últimos 7 dias",
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
    "tab_stats": "Estatísticas",
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
    "connect_account_hint": "Vincule sua conta para receber este drop",
    "show_details": "Mostrar detalhes",
    "filter_all": "Todas as contas",
    "filter_linked_only": "Somente vinculadas",
    "filter_not_linked_only": "Somente não vinculadas",
    "exclude_list": "Lista de exclusão",
    "account_status": "Estado da conta",
    "all_channels": "Todos os canais",
    "allowed_channels": "Canais permitidos",
    "ends_at": "Termina em",
    "linked": "Vinculada",
    "not_linked": "Não vinculada",
    "mine_unlinked": "Minerar campanhas não vinculadas",
    "mine_unlinked_hint": "Também minerar drops quando a Twitch indicar que a conta do jogo não está vinculada.",
    "range_day": "Hoje",
    "range_week": "7 dias",
    "range_month": "30 dias",
    "range_3months": "3 meses",
    "range_all": "Todo o período",
    "stats_drops_title": "Drops obtidos",
    "stats_hours_title": "Horas de visualização poupadas",
    "stats_no_data": "Sem dados para este período."
  },
  "nl": {
    "faq_title": "Vragen en antwoorden",
    "faq_q1": "Kijkt DropStream echt naar streams?",
    "faq_a1": "Nee. Het vraagt om de paar seconden alleen de metadata van een stream op bij Twitch, en dat is genoeg voor Twitch om de voortgang van een drop bij te houden. Er wordt geen video of audio gedownload, dus het gebruikt nauwelijks bandbreedte.",
    "faq_q2": "Waarom staat mijn voortgang stil of klopt hij niet?",
    "faq_a2": "Waarschijnlijk kijkt hetzelfde Twitch-account ergens anders naar een stream, bijvoorbeeld in een browser. Twitch beheert de drop-voortgang aan zijn kant en dat brengt de miner in de war. Kijk met dat account geen andere streams terwijl het mined.",
    "faq_q3": "Wat kan ik op deze pagina doen?",
    "faq_a3": "In de weergavemodus volg je de huidige drop, de campagnes, je statistieken en de logs. In de besturingsmodus kun je ook pauzeren of hervatten, een pauzetimer instellen en de prioriteits- en uitsluitlijsten aanpassen. Deze pagina kan niet zelf inloggen op je account of drops claimen, ze spiegelt en stuurt alleen de desktopapp aan.",
    "faq_q4": "Hoe kies ik welke games gemined worden?",
    "faq_a4": "Voeg games toe aan de prioriteitslijst, kies een prioriteitsmodus en druk op Herladen zodat de app de wijzigingen toepast. Bij elke modus behalve alleen prioriteitslijst pakt de miner ook andere beschikbare campagnes. Games op de uitsluitlijst worden genegeerd.",
    "faq_q5": "Een game die ik wil verschijnt niet. Waarom?",
    "faq_a5": "Je Twitch-account moet aan die game gekoppeld zijn op de campagnepagina van Twitch. Druk daarna op Herladen, dan kan de campagne worden opgepakt.",
    "faq_q6": "Is de link privé? Kan ik hem buitenshuis gebruiken?",
    "faq_a6": "Iedereen met de link kan je instantie zien, behandel hem dus als een wachtwoord. Heb je hem per ongeluk gedeeld, maak dan in de desktopapp een nieuwe aan. Standaard werkt hij alleen op je lokale netwerk. Van buitenaf heb je port forwarding of een VPN zoals Tailscale of WireGuard nodig.",
    "faq_q7": "Is de resterende tijd exact?",
    "faq_a7": "Nee, het is een schatting. Hij telt per minuut af en wordt bijgesteld zodra de app de echte waarde van Twitch krijgt. Het heeft geen invloed op hoe snel je mined.",
    "faq_q8": "Wat zijn de logs?",
    "faq_a8": "Dezelfde regels als in het vak Uitvoer van de desktopapp, alleen-lezen.",
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
    "stats_weekly_title": "Laatste 7 dagen",
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
    "tab_stats": "Statistieken",
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
    "connect_account_hint": "Koppel je account om deze drop te ontvangen",
    "show_details": "Details tonen",
    "filter_all": "Alle accounts",
    "filter_linked_only": "Alleen gekoppeld",
    "filter_not_linked_only": "Alleen niet gekoppeld",
    "exclude_list": "Uitsluitingslijst",
    "account_status": "Accountstatus",
    "all_channels": "Alle kanalen",
    "allowed_channels": "Toegestane kanalen",
    "ends_at": "Eindigt op",
    "linked": "Gekoppeld",
    "not_linked": "Niet gekoppeld",
    "mine_unlinked": "Niet-gekoppelde campagnes minen",
    "mine_unlinked_hint": "Ook drops minen wanneer Twitch aangeeft dat het spelaccount niet gekoppeld is.",
    "range_day": "Vandaag",
    "range_week": "7 dagen",
    "range_month": "30 dagen",
    "range_3months": "3 maanden",
    "range_all": "Altijd",
    "stats_drops_title": "Verkregen drops",
    "stats_hours_title": "Bespaarde kijkuren",
    "stats_no_data": "Geen gegevens voor deze periode."
  },
  "da": {
    "faq_title": "Spørgsmål og svar",
    "faq_q1": "Ser DropStream faktisk streams?",
    "faq_a1": "Nej. Det beder kun Twitch om en streams metadata hvert par sekunder, og det er nok til, at Twitch tæller fremgangen mod et drop. Der hentes hverken video eller lyd, så det bruger næsten ingen båndbredde.",
    "faq_q2": "Hvorfor sidder min fremgang fast eller er forkert?",
    "faq_a2": "Sandsynligvis ser den samme Twitch-konto en stream et andet sted, for eksempel i en browser. Twitch styrer drop-fremgangen hos sig selv, og det forvirrer mineren. Undgå at se andre streams med den konto, mens den miner.",
    "faq_q3": "Hvad kan jeg gøre på denne side?",
    "faq_a3": "I visningstilstand kan du følge det aktuelle drop, kampagnerne, dine statistikker og loggene. I kontroltilstand kan du også sætte på pause eller genoptage, indstille en pausetimer og redigere prioritets- og udelukkelseslisterne. Siden kan ikke selv logge ind på din konto eller indløse drops, den spejler og styrer kun skrivebordsappen.",
    "faq_q4": "Hvordan vælger jeg, hvilke spil der mines?",
    "faq_a4": "Tilføj spil til prioritetslisten, vælg en prioritetstilstand og tryk på Genindlæs, så appen anvender ændringerne. Med en anden tilstand end kun prioritetsliste tager mineren også andre tilgængelige kampagner. Spil på udelukkelseslisten ignoreres.",
    "faq_q5": "Et spil, jeg vil have, dukker ikke op. Hvorfor?",
    "faq_a5": "Din Twitch-konto skal være forbundet til spillet på Twitchs kampagneside. Når den er det, så tryk på Genindlæs, og kampagnen kan blive taget med.",
    "faq_q6": "Er linket privat? Kan jeg bruge det uden for hjemmet?",
    "faq_a6": "Alle med linket kan se din instans, så behandl det som en adgangskode. Har du delt det ved en fejl, så lav et nyt i skrivebordsappen. Som standard virker det kun på dit lokale netværk. Udefra skal du bruge port forwarding eller et VPN som Tailscale eller WireGuard.",
    "faq_q7": "Er den resterende tid nøjagtig?",
    "faq_a7": "Nej, den er et estimat. Den tæller ned minut for minut og justeres, når appen får den rigtige værdi fra Twitch. Den påvirker ikke, hvor hurtigt du miner.",
    "faq_q8": "Hvad er loggene?",
    "faq_a8": "De samme linjer som i feltet Output i skrivebordsappen, skrivebeskyttet.",
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
    "stats_weekly_title": "Sidste 7 dage",
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
    "tab_stats": "Statistik",
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
    "connect_account_hint": "Tilknyt din konto for at få dette drop",
    "show_details": "Vis detaljer",
    "filter_all": "Alle konti",
    "filter_linked_only": "Kun tilknyttede",
    "filter_not_linked_only": "Kun ikke-tilknyttede",
    "exclude_list": "Udelukkelsesliste",
    "account_status": "Kontostatus",
    "all_channels": "Alle kanaler",
    "allowed_channels": "Tilladte kanaler",
    "ends_at": "Slutter den",
    "linked": "Forbundet",
    "not_linked": "Ikke forbundet",
    "mine_unlinked": "Udvind ikke-forbundne kampagner",
    "mine_unlinked_hint": "Udvind også drops, når Twitch rapporterer at spilkontoen ikke er forbundet.",
    "range_day": "I dag",
    "range_week": "7 dage",
    "range_month": "30 dage",
    "range_3months": "3 måneder",
    "range_all": "Alt",
    "stats_drops_title": "Hentede drops",
    "stats_hours_title": "Sparede sete timer",
    "stats_no_data": "Ingen data for denne periode."
  },
  "no": {
    "faq_title": "Spørsmål og svar",
    "faq_q1": "Ser DropStream faktisk på strømmer?",
    "faq_a1": "Nei. Det ber bare Twitch om en strøms metadata hvert par sekunder, og det er nok til at Twitch teller fremgangen mot en drop. Ingen video eller lyd lastes ned, så det bruker nesten ingen båndbredde.",
    "faq_q2": "Hvorfor står fremgangen min fast eller er feil?",
    "faq_a2": "Mest sannsynlig ser den samme Twitch-kontoen på en strøm et annet sted, for eksempel i en nettleser. Twitch styrer drop-fremgangen hos seg selv, og det forvirrer mineren. Unngå å se andre strømmer med den kontoen mens den miner.",
    "faq_q3": "Hva kan jeg gjøre på denne siden?",
    "faq_a3": "I visningsmodus kan du følge den aktuelle dropen, kampanjene, statistikken din og loggene. I kontrollmodus kan du også pause eller fortsette, stille inn en pausetimer og redigere prioritets- og utelukkelseslistene. Siden kan ikke logge inn på kontoen din eller hente drops selv, den speiler og styrer bare skrivebordsappen.",
    "faq_q4": "Hvordan velger jeg hvilke spill som mines?",
    "faq_a4": "Legg spill til i prioritetslisten, velg en prioritetsmodus og trykk Last på nytt så appen bruker endringene. Med en annen modus enn bare prioritetsliste tar mineren også andre tilgjengelige kampanjer. Spill på utelukkelseslisten blir ignorert.",
    "faq_q5": "Et spill jeg vil ha dukker ikke opp. Hvorfor?",
    "faq_a5": "Twitch-kontoen din må være koblet til spillet på Twitchs kampanjeside. Når den er det, trykk Last på nytt, så kan kampanjen tas med.",
    "faq_q6": "Er lenken privat? Kan jeg bruke den borte fra hjemmet?",
    "faq_a6": "Alle med lenken kan se instansen din, så behandle den som et passord. Har du delt den ved en feil, lag en ny i skrivebordsappen. Som standard fungerer den bare på det lokale nettverket. Utenfra trenger du port forwarding eller et VPN som Tailscale eller WireGuard.",
    "faq_q7": "Er gjenstående tid nøyaktig?",
    "faq_a7": "Nei, det er et anslag. Den teller ned minutt for minutt og justeres når appen får den ekte verdien fra Twitch. Den påvirker ikke hvor fort du miner.",
    "faq_q8": "Hva er loggene?",
    "faq_a8": "De samme linjene som i Utdata-feltet i skrivebordsappen, skrivebeskyttet.",
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
    "stats_weekly_title": "Siste 7 dager",
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
    "tab_stats": "Statistikk",
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
    "connect_account_hint": "Koble til kontoen din for å få dette droppet",
    "show_details": "Vis detaljer",
    "filter_all": "Alle kontoer",
    "filter_linked_only": "Kun tilkoblede",
    "filter_not_linked_only": "Kun ikke tilkoblede",
    "exclude_list": "Ekskluderingsliste",
    "account_status": "Kontostatus",
    "all_channels": "Alle kanaler",
    "allowed_channels": "Tillatte kanaler",
    "ends_at": "Slutter",
    "linked": "Koblet til",
    "not_linked": "Ikke koblet til",
    "mine_unlinked": "Utvinn ikke-koblede kampanjer",
    "mine_unlinked_hint": "Utvinn drops også når Twitch rapporterer at spillkontoen ikke er koblet til.",
    "range_day": "I dag",
    "range_week": "7 dager",
    "range_month": "30 dager",
    "range_3months": "3 måneder",
    "range_all": "Alt",
    "stats_drops_title": "Hentede drops",
    "stats_hours_title": "Sparte seertimer",
    "stats_no_data": "Ingen data for denne perioden."
  },
  "pl": {
    "faq_title": "Pytania i odpowiedzi",
    "faq_q1": "Czy DropStream naprawdę ogląda transmisje?",
    "faq_a1": "Nie. Co kilka sekund prosi Twitcha tylko o metadane transmisji, a to wystarcza, żeby Twitch zaliczał postęp dropa. Nie pobiera wideo ani dźwięku, więc prawie nie zużywa łącza.",
    "faq_q2": "Dlaczego mój postęp stoi lub jest błędny?",
    "faq_a2": "Najpewniej to samo konto Twitch ogląda gdzieś indziej transmisję, na przykład w przeglądarce. Twitch liczy postęp dropów po swojej stronie i to myli koparkę. Nie oglądaj innych transmisji na tym koncie, gdy działa kopanie.",
    "faq_q3": "Co mogę zrobić na tej stronie?",
    "faq_a3": "W trybie tylko do odczytu śledzisz bieżący drop, kampanie, statystyki i logi. W trybie sterowania możesz też wstrzymać lub wznowić, ustawić minutnik pauzy i edytować listy priorytetów i wykluczeń. Strona sama nie zaloguje się na Twoje konto ani nie odbierze dropów, tylko odzwierciedla i steruje aplikacją na komputerze.",
    "faq_q4": "Jak wybrać, które gry są kopane?",
    "faq_a4": "Dodaj gry do listy priorytetów, wybierz tryb priorytetu i naciśnij Odśwież, żeby aplikacja zastosowała zmiany. W każdym trybie innym niż tylko lista priorytetów koparka bierze też inne dostępne kampanie. Gry z listy wykluczeń są pomijane.",
    "faq_q5": "Gra, której chcę, się nie pojawia. Dlaczego?",
    "faq_a5": "Twoje konto Twitch musi być połączone z tą grą na stronie kampanii Twitcha. Gdy już będzie, naciśnij Odśwież, a kampania będzie mogła zostać uwzględniona.",
    "faq_q6": "Czy link jest prywatny? Czy mogę go używać poza domem?",
    "faq_a6": "Każdy, kto ma link, widzi Twoją instancję, więc traktuj go jak hasło. Jeśli udostępniłeś go przez pomyłkę, wygeneruj nowy w aplikacji na komputerze. Domyślnie działa tylko w sieci lokalnej. Z zewnątrz potrzebujesz przekierowania portów lub VPN, takiego jak Tailscale czy WireGuard.",
    "faq_q7": "Czy pozostały czas jest dokładny?",
    "faq_a7": "Nie, to przybliżenie. Odlicza minuta po minucie i koryguje się, gdy aplikacja dostanie prawdziwą wartość od Twitcha. Nie wpływa na szybkość kopania.",
    "faq_q8": "Czym są logi?",
    "faq_a8": "Tymi samymi wierszami, które widać w polu Wyjście aplikacji na komputerze, tylko do odczytu.",
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
    "stats_weekly_title": "Ostatnie 7 dni",
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
    "tab_stats": "Statystyki",
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
    "connect_account_hint": "Połącz konto, aby otrzymać ten drop",
    "show_details": "Pokaż szczegóły",
    "filter_all": "Wszystkie konta",
    "filter_linked_only": "Tylko połączone",
    "filter_not_linked_only": "Tylko niepołączone",
    "exclude_list": "Lista wykluczeń",
    "account_status": "Status konta",
    "all_channels": "Wszystkie kanały",
    "allowed_channels": "Dozwolone kanały",
    "ends_at": "Kończy się",
    "linked": "Połączone",
    "not_linked": "Niepołączone",
    "mine_unlinked": "Wydobywaj niepołączone kampanie",
    "mine_unlinked_hint": "Wydobywaj dropy nawet gdy Twitch zgłasza, że konto gry nie jest połączone.",
    "range_day": "Dziś",
    "range_week": "7 dni",
    "range_month": "30 dni",
    "range_3months": "3 miesiące",
    "range_all": "Cały okres",
    "stats_drops_title": "Odebrane dropy",
    "stats_hours_title": "Zaoszczędzone godziny oglądania",
    "stats_no_data": "Brak danych dla tego okresu."
  },
  "cs": {
    "faq_title": "Otázky a odpovědi",
    "faq_q1": "Opravdu DropStream sleduje streamy?",
    "faq_a1": "Ne. Jen každých pár sekund požádá Twitch o metadata streamu, a to Twitchi stačí k započítání postupu dropu. Nestahuje se video ani zvuk, takže skoro nespotřebovává data.",
    "faq_q2": "Proč můj postup stojí nebo je špatně?",
    "faq_a2": "Nejspíš stejný účet Twitch sleduje stream jinde, třeba v prohlížeči. Twitch řeší postup dropů u sebe a miner tím zmate. Na tomto účtu nesleduj jiné streamy, dokud těží.",
    "faq_q3": "Co můžu dělat na této stránce?",
    "faq_a3": "V režimu jen pro čtení sleduješ aktuální drop, kampaně, statistiky a logy. V režimu ovládání můžeš také pozastavit nebo pokračovat, nastavit časovač pauzy a upravovat seznamy priorit a vyloučených her. Stránka se sama nepřihlásí k tvému účtu ani nevyzvedne dropy, jen zrcadlí a řídí desktopovou aplikaci.",
    "faq_q4": "Jak vybrat, které hry se těží?",
    "faq_a4": "Přidej hry do seznamu priorit, vyber režim priority a stiskni Znovu načíst, aby aplikace změny použila. V jiném režimu než jen seznam priorit bere miner i další dostupné kampaně. Hry ze seznamu vyloučených se ignorují.",
    "faq_q5": "Hra, kterou chci, se nezobrazuje. Proč?",
    "faq_a5": "Tvůj účet Twitch musí být s touto hrou propojený na stránce kampaní Twitche. Po propojení stiskni Znovu načíst a kampaň bude možné zahrnout.",
    "faq_q6": "Je odkaz soukromý? Můžu ho použít mimo domov?",
    "faq_a6": "Kdokoli s odkazem uvidí tvoji instanci, takže s ním zacházej jako s heslem. Pokud jsi ho sdílel omylem, vygeneruj v desktopové aplikaci nový. Ve výchozím stavu funguje jen v místní síti. Zvenku potřebuješ přesměrování portů nebo VPN, například Tailscale či WireGuard.",
    "faq_q7": "Je zbývající čas přesný?",
    "faq_a7": "Ne, je to odhad. Odpočítává po minutách a srovná se, když aplikace získá skutečnou hodnotu od Twitche. Na rychlost těžení nemá vliv.",
    "faq_q8": "Co jsou logy?",
    "faq_a8": "Stejné řádky jako v poli Výstup v desktopové aplikaci, jen pro čtení.",
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
    "stats_weekly_title": "Posledních 7 dní",
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
    "tab_stats": "Statistiky",
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
    "connect_account_hint": "Propojte svůj účet, abyste získali tento drop",
    "show_details": "Zobrazit podrobnosti",
    "filter_all": "Všechny účty",
    "filter_linked_only": "Pouze propojené",
    "filter_not_linked_only": "Pouze nepropojené",
    "exclude_list": "Seznam vyloučení",
    "account_status": "Stav účtu",
    "all_channels": "Všechny kanály",
    "allowed_channels": "Povolené kanály",
    "ends_at": "Končí",
    "linked": "Propojeno",
    "not_linked": "Nepropojeno",
    "mine_unlinked": "Těžit nepropojené kampaně",
    "mine_unlinked_hint": "Těžit dropy i když Twitch hlásí, že herní účet není propojen.",
    "range_day": "Dnes",
    "range_week": "7 dní",
    "range_month": "30 dní",
    "range_3months": "3 měsíce",
    "range_all": "Vše",
    "stats_drops_title": "Získané dropy",
    "stats_hours_title": "Ušetřené hodiny sledování",
    "stats_no_data": "Pro toto období nejsou k dispozici žádná data."
  },
  "ro": {
    "faq_title": "Întrebări și răspunsuri",
    "faq_q1": "DropStream se uită într-adevăr la stream-uri?",
    "faq_a1": "Nu. Cere doar metadatele unui stream de la Twitch la câteva secunde, iar asta îi ajunge lui Twitch ca să numere progresul unui drop. Nu se descarcă video sau audio, deci consumă aproape deloc bandă.",
    "faq_q2": "De ce e progresul blocat sau greșit?",
    "faq_a2": "Cel mai probabil același cont Twitch se uită la un stream în altă parte, de exemplu în browser. Twitch gestionează progresul drop-urilor la el și asta încurcă minerul. Evită să te uiți la alte stream-uri cu acel cont cât timp minează.",
    "faq_q3": "Ce pot face din această pagină?",
    "faq_a3": "În modul doar vizualizare urmărești drop-ul curent, campaniile, statisticile și jurnalele. În modul control poți și să pui pe pauză sau să reiei, să setezi un cronometru de pauză și să editezi listele de prioritate și de excludere. Pagina nu se poate autentifica în contul tău și nici nu poate revendica drop-uri singură, doar reflectă și dirijează aplicația de desktop.",
    "faq_q4": "Cum aleg ce jocuri se minează?",
    "faq_a4": "Adaugă jocuri în lista de prioritate, alege un mod de prioritate și apasă Reîncarcă ca aplicația să aplice modificările. Cu orice mod în afară de doar lista de prioritate, minerul ia și alte campanii disponibile. Jocurile din lista de excludere sunt ignorate.",
    "faq_q5": "Un joc pe care îl vreau nu apare. De ce?",
    "faq_a5": "Contul tău Twitch trebuie să fie legat de acel joc pe pagina de campanii Twitch. După ce e legat, apasă Reîncarcă și campania poate fi luată în calcul.",
    "faq_q6": "Este linkul privat? Îl pot folosi în afara casei?",
    "faq_a6": "Oricine are linkul îți poate vedea instanța, așa că tratează-l ca pe o parolă. Dacă l-ai partajat din greșeală, generează unul nou în aplicația de desktop. Implicit funcționează doar în rețeaua locală. Din exterior ai nevoie de redirecționare de porturi sau de un VPN precum Tailscale ori WireGuard.",
    "faq_q7": "Este timpul rămas exact?",
    "faq_a7": "Nu, este o aproximare. Numără invers minut cu minut și se resetează când aplicația primește valoarea reală de la Twitch. Nu influențează viteza de minare.",
    "faq_q8": "Ce sunt jurnalele?",
    "faq_a8": "Aceleași rânduri ca în caseta Ieșire din aplicația de desktop, doar pentru citire.",
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
    "stats_weekly_title": "Ultimele 7 zile",
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
    "tab_stats": "Statistici",
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
    "connect_account_hint": "Conectează-ți contul pentru a primi acest drop",
    "show_details": "Arată detalii",
    "filter_all": "Toate conturile",
    "filter_linked_only": "Doar conectate",
    "filter_not_linked_only": "Doar neconectate",
    "exclude_list": "Listă de excludere",
    "account_status": "Stare cont",
    "all_channels": "Toate canalele",
    "allowed_channels": "Canale permise",
    "ends_at": "Se încheie la",
    "linked": "Conectat",
    "not_linked": "Neconectat",
    "mine_unlinked": "Minează campanii neconectate",
    "mine_unlinked_hint": "Minează drop-uri și când Twitch raportează că nu contul de joc nu este conectat.",
    "range_day": "Azi",
    "range_week": "7 zile",
    "range_month": "30 zile",
    "range_3months": "3 luni",
    "range_all": "Tot timpul",
    "stats_drops_title": "Drop-uri obținute",
    "stats_hours_title": "Ore de vizionare economisite",
    "stats_no_data": "Nu există date pentru această perioadă."
  },
  "hu": {
    "faq_title": "Kérdések és válaszok",
    "faq_q1": "Tényleg nézi a streameket a DropStream?",
    "faq_a1": "Nem. Néhány másodpercenként csak a stream metaadatait kéri le a Twitchtől, és ez elég ahhoz, hogy a Twitch számolja a drop haladását. Nem tölt le videót vagy hangot, ezért alig használ sávszélességet.",
    "faq_q2": "Miért áll vagy hibás a haladásom?",
    "faq_a2": "Valószínűleg ugyanaz a Twitch-fiók néz máshol streamet, például böngészőben. A Twitch a saját oldalán kezeli a dropok haladását, és ez összezavarja a bányászt. Ne nézz más streamet azzal a fiókkal, amíg bányászik.",
    "faq_q3": "Mit tehetek ezen az oldalon?",
    "faq_a3": "Csak megtekintés módban követheted az aktuális dropot, a kampányokat, a statisztikákat és a naplókat. Vezérlő módban szüneteltethetsz vagy folytathatsz, beállíthatsz szünet-időzítőt, és szerkesztheted a prioritási és kizárási listát. Az oldal nem tud magától belépni a fiókodba vagy dropot átvenni, csak tükrözi és irányítja az asztali alkalmazást.",
    "faq_q4": "Hogyan választom ki, mely játékokat bányássza?",
    "faq_a4": "Adj játékokat a prioritási listához, válassz prioritási módot, majd nyomd meg az Újratöltés gombot, hogy az alkalmazás alkalmazza a változásokat. A csak prioritási lista módtól eltérő módban a bányász más elérhető kampányokat is felvesz. A kizárási listán lévő játékokat figyelmen kívül hagyja.",
    "faq_q5": "Egy játék, amit szeretnék, nem jelenik meg. Miért?",
    "faq_a5": "A Twitch-fiókodat össze kell kötni az adott játékkal a Twitch kampányoldalán. Utána nyomd meg az Újratöltés gombot, és a kampány figyelembe vehető.",
    "faq_q6": "Privát a link? Használhatom otthonon kívül?",
    "faq_a6": "Bárki, aki ismeri a linket, látja a példányodat, ezért kezeld jelszóként. Ha véletlenül megosztottad, generálj újat az asztali alkalmazásban. Alapértelmezetten csak a helyi hálózaton működik. Kívülről porttovábbítás vagy VPN kell hozzá, például Tailscale vagy WireGuard.",
    "faq_q7": "Pontos a hátralévő idő?",
    "faq_a7": "Nem, csak becslés. Percenként számol vissza, és újraigazodik, amikor az alkalmazás megkapja a valódi értéket a Twitchtől. A bányászat sebességére nincs hatással.",
    "faq_q8": "Mik a naplók?",
    "faq_a8": "Ugyanazok a sorok, mint az asztali alkalmazás Kimenet mezőjében, csak olvasásra.",
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
    "stats_weekly_title": "Elmúlt 7 nap",
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
    "tab_stats": "Statisztika",
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
    "connect_account_hint": "Kösd össze a fiókod, hogy megkapd ezt a dropot",
    "show_details": "Részletek megjelenítése",
    "filter_all": "Minden fiók",
    "filter_linked_only": "Csak összekötött",
    "filter_not_linked_only": "Csak nem összekötött",
    "exclude_list": "Kizárási lista",
    "account_status": "Fiók állapota",
    "all_channels": "Minden csatorna",
    "allowed_channels": "Engedélyezett csatornák",
    "ends_at": "Vége",
    "linked": "Összekapcsolva",
    "not_linked": "Nincs összekapcsolva",
    "mine_unlinked": "Nem összekapcsolt kampányok bányászata",
    "mine_unlinked_hint": "Dropok bányászata akkor is, ha a Twitch szerint a játékfiók nincs összekapcsolva.",
    "range_day": "Ma",
    "range_week": "7 nap",
    "range_month": "30 nap",
    "range_3months": "3 hónap",
    "range_all": "Összes",
    "stats_drops_title": "Megszerzett dropok",
    "stats_hours_title": "Megtakarított nézési órák",
    "stats_no_data": "Nincs adat erre az időszakra."
  },
  "tr": {
    "faq_title": "Sorular ve cevaplar",
    "faq_q1": "DropStream gerçekten yayınları izliyor mu?",
    "faq_a1": "Hayır. Birkaç saniyede bir Twitch'ten yalnızca yayının meta verilerini ister ve Twitch'in drop ilerlemesini saymasına bu yeter. Video ya da ses indirilmez, bu yüzden neredeyse hiç bant genişliği harcamaz.",
    "faq_q2": "İlerlemem neden takılı kaldı ya da yanlış?",
    "faq_a2": "Büyük ihtimalle aynı Twitch hesabı başka bir yerde, örneğin tarayıcıda yayın izliyor. Twitch drop ilerlemesini kendi tarafında yönetiyor ve bu madenciyi şaşırtıyor. Madencilik sürerken o hesapla başka yayın izlemekten kaçının.",
    "faq_q3": "Bu sayfada neler yapabilirim?",
    "faq_a3": "Yalnızca görüntüleme modunda mevcut drop'u, kampanyaları, istatistikleri ve günlükleri takip edebilirsiniz. Kontrol modunda ayrıca duraklatıp devam ettirebilir, duraklatma zamanlayıcısı kurabilir, öncelik ve hariç tutma listelerini düzenleyebilirsiniz. Sayfa kendi başına hesabınıza giremez ya da drop alamaz, yalnızca masaüstü uygulamayı yansıtır ve yönlendirir.",
    "faq_q4": "Hangi oyunların kazılacağını nasıl seçerim?",
    "faq_a4": "Oyunları öncelik listesine ekleyin, bir öncelik modu seçin ve uygulamanın değişiklikleri uygulaması için Yeniden yükle'ye basın. Yalnızca öncelik listesi dışındaki her modda madenci diğer uygun kampanyaları da alır. Hariç tutma listesindeki oyunlar yok sayılır.",
    "faq_q5": "İstediğim bir oyun görünmüyor. Neden?",
    "faq_a5": "Twitch hesabınızın Twitch'in kampanyalar sayfasında o oyuna bağlı olması gerekir. Bağladıktan sonra Yeniden yükle'ye basın, kampanya değerlendirilebilir.",
    "faq_q6": "Bağlantı özel mi? Evin dışında kullanabilir miyim?",
    "faq_a6": "Bağlantıya sahip herkes örneğinizi görebilir, bu yüzden onu bir parola gibi düşünün. Yanlışlıkla paylaştıysanız masaüstü uygulamada yenisini oluşturun. Varsayılan olarak yalnızca yerel ağınızda çalışır. Dışarıdan erişmek için port yönlendirme ya da Tailscale veya WireGuard gibi bir VPN gerekir.",
    "faq_q7": "Kalan süre kesin mi?",
    "faq_a7": "Hayır, bir tahmindir. Dakika dakika geri sayar ve uygulama Twitch'ten gerçek değeri alınca kendini düzeltir. Madencilik hızını etkilemez.",
    "faq_q8": "Günlükler nedir?",
    "faq_a8": "Masaüstü uygulamadaki Çıktı kutusunda gördüğünüz satırların aynısı, yalnızca okunabilir.",
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
    "stats_weekly_title": "Son 7 gün",
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
    "tab_stats": "İstatistikler",
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
    "connect_account_hint": "Bu drop'u almak için hesabını bağla",
    "show_details": "Ayrıntıları göster",
    "filter_all": "Tüm hesaplar",
    "filter_linked_only": "Yalnızca bağlı",
    "filter_not_linked_only": "Yalnızca bağlı olmayan",
    "exclude_list": "Hariç tutma listesi",
    "account_status": "Hesap durumu",
    "all_channels": "Tüm kanallar",
    "allowed_channels": "İzin verilen kanallar",
    "ends_at": "Bitiş",
    "linked": "Bağlı",
    "not_linked": "Bağlı değil",
    "mine_unlinked": "Bağlı olmayan kampanyaları madenle",
    "mine_unlinked_hint": "Twitch oyun hesabının bağlı olmadığını bildirse bile drop madenciliği yap.",
    "range_day": "Bugün",
    "range_week": "7 gün",
    "range_month": "30 gün",
    "range_3months": "3 ay",
    "range_all": "Tüm zamanlar",
    "stats_drops_title": "Alınan droplar",
    "stats_hours_title": "Kazanılan izleme saati",
    "stats_no_data": "Bu dönem için veri yok."
  },
  "ru": {
    "faq_title": "Вопросы и ответы",
    "faq_q1": "DropStream правда смотрит стримы?",
    "faq_a1": "Нет. Он лишь раз в несколько секунд запрашивает у Twitch метаданные стрима, и этого Twitch достаточно, чтобы засчитывать прогресс дропа. Видео и звук не скачиваются, поэтому трафика почти не тратится.",
    "faq_q2": "Почему прогресс застрял или неверный?",
    "faq_a2": "Скорее всего, тот же аккаунт Twitch смотрит стрим в другом месте, например в браузере. Twitch считает прогресс дропов у себя, и это сбивает майнер. Не смотри другие стримы с этого аккаунта, пока идёт майнинг.",
    "faq_q3": "Что можно делать на этой странице?",
    "faq_a3": "В режиме просмотра ты следишь за текущим дропом, кампаниями, статистикой и журналами. В режиме управления можно ещё ставить на паузу и возобновлять, задавать таймер паузы и редактировать списки приоритета и исключений. Сама страница не может войти в твой аккаунт или забрать дропы, она только отражает и управляет настольным приложением.",
    "faq_q4": "Как выбрать, какие игры майнить?",
    "faq_a4": "Добавь игры в список приоритета, выбери режим приоритета и нажми Перезагрузить, чтобы приложение применило изменения. В любом режиме, кроме только списка приоритета, майнер берёт и другие доступные кампании. Игры из списка исключений игнорируются.",
    "faq_q5": "Нужная мне игра не появляется. Почему?",
    "faq_a5": "Твой аккаунт Twitch должен быть привязан к этой игре на странице кампаний Twitch. После привязки нажми Перезагрузить, и кампанию можно будет учесть.",
    "faq_q6": "Ссылка приватная? Можно пользоваться ею вне дома?",
    "faq_a6": "Любой, у кого есть ссылка, видит твой экземпляр, поэтому относись к ней как к паролю. Если поделился ею случайно, создай новую в настольном приложении. По умолчанию она работает только в локальной сети. Снаружи нужен проброс портов или VPN вроде Tailscale или WireGuard.",
    "faq_q7": "Оставшееся время точное?",
    "faq_a7": "Нет, это приблизительная оценка. Оно отсчитывает по минуте и подстраивается, когда приложение получает реальное значение от Twitch. На скорость майнинга это не влияет.",
    "faq_q8": "Что такое журналы?",
    "faq_a8": "Те же строки, что в поле Вывод настольного приложения, только для чтения.",
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
    "stats_weekly_title": "Последние 7 дней",
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
    "tab_stats": "Статистика",
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
    "connect_account_hint": "Привяжите аккаунт, чтобы получить этот дроп",
    "show_details": "Показать детали",
    "filter_all": "Все аккаунты",
    "filter_linked_only": "Только привязанные",
    "filter_not_linked_only": "Только непривязанные",
    "exclude_list": "Список исключений",
    "account_status": "Статус аккаунта",
    "all_channels": "Все каналы",
    "allowed_channels": "Разрешённые каналы",
    "ends_at": "Заканчивается",
    "linked": "Привязан",
    "not_linked": "Не привязан",
    "mine_unlinked": "Майнить непривязанные кампании",
    "mine_unlinked_hint": "Майнить дропы, даже если Twitch сообщает, что игровой аккаунт не привязан.",
    "range_day": "Сегодня",
    "range_week": "7 дней",
    "range_month": "30 дней",
    "range_3months": "3 месяца",
    "range_all": "Всё время",
    "stats_drops_title": "Полученные дропы",
    "stats_hours_title": "Сэкономленные часы просмотра",
    "stats_no_data": "Нет данных за этот период."
  },
  "uk": {
    "faq_title": "Питання та відповіді",
    "faq_q1": "DropStream справді дивиться стріми?",
    "faq_a1": "Ні. Він лише раз на кілька секунд запитує в Twitch метадані стріму, і цього Twitch достатньо, щоб зараховувати прогрес дропу. Відео й звук не завантажуються, тож трафіку майже не витрачається.",
    "faq_q2": "Чому прогрес застряг або хибний?",
    "faq_a2": "Найімовірніше, той самий акаунт Twitch дивиться стрім деінде, наприклад у браузері. Twitch рахує прогрес дропів у себе, і це збиває майнер. Не дивись інші стріми з цього акаунта, поки триває майнінг.",
    "faq_q3": "Що можна робити на цій сторінці?",
    "faq_a3": "У режимі перегляду ти стежиш за поточним дропом, кампаніями, статистикою та журналами. У режимі керування можна ще ставити на паузу й відновлювати, задавати таймер паузи та редагувати списки пріоритету й винятків. Сама сторінка не може увійти до твого акаунта чи забрати дропи, вона лише відображає та керує настільним застосунком.",
    "faq_q4": "Як вибрати, які ігри майнити?",
    "faq_a4": "Додай ігри до списку пріоритету, вибери режим пріоритету й натисни Перезавантажити, щоб застосунок застосував зміни. У будь-якому режимі, крім лише списку пріоритету, майнер бере й інші доступні кампанії. Ігри зі списку винятків ігноруються.",
    "faq_q5": "Потрібна мені гра не з'являється. Чому?",
    "faq_a5": "Твій акаунт Twitch має бути прив'язаний до цієї гри на сторінці кампаній Twitch. Після прив'язки натисни Перезавантажити, і кампанію можна буде врахувати.",
    "faq_q6": "Посилання приватне? Чи можна користуватися ним поза домом?",
    "faq_a6": "Будь-хто з посиланням бачить твій екземпляр, тому стався до нього як до пароля. Якщо поділився ним випадково, створи нове в настільному застосунку. За замовчуванням воно працює лише в локальній мережі. Ззовні потрібне перенаправлення портів або VPN на кшталт Tailscale чи WireGuard.",
    "faq_q7": "Час, що залишився, точний?",
    "faq_a7": "Ні, це наближена оцінка. Він відлічує хвилину за хвилиною й підлаштовується, коли застосунок отримує реальне значення від Twitch. На швидкість майнінгу це не впливає.",
    "faq_q8": "Що таке журнали?",
    "faq_a8": "Ті самі рядки, що в полі Вивід настільного застосунку, лише для читання.",
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
    "stats_weekly_title": "Останні 7 днів",
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
    "tab_stats": "Статистика",
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
    "connect_account_hint": "Прив'яжіть акаунт, щоб отримати цей дроп",
    "show_details": "Показати деталі",
    "filter_all": "Всі акаунти",
    "filter_linked_only": "Тільки прив'язані",
    "filter_not_linked_only": "Тільки непов'язані",
    "exclude_list": "Список виключень",
    "account_status": "Статус акаунта",
    "all_channels": "Усі канали",
    "allowed_channels": "Дозволені канали",
    "ends_at": "Закінчується",
    "linked": "Прив'язано",
    "not_linked": "Не прив'язано",
    "mine_unlinked": "Майнити неприв'язані кампанії",
    "mine_unlinked_hint": "Майнити дропи, навіть якщо Twitch повідомляє, що ігровий акаунт не прив'язаний.",
    "range_day": "Сьогодні",
    "range_week": "7 днів",
    "range_month": "30 днів",
    "range_3months": "3 місяці",
    "range_all": "Увесь час",
    "stats_drops_title": "Отримані дропи",
    "stats_hours_title": "Заощаджені години перегляду",
    "stats_no_data": "Немає даних за цей період."
  },
  "ar": {
    "faq_title": "أسئلة وأجوبة",
    "faq_q1": "هل يشاهد DropStream البثوث فعلًا؟",
    "faq_a1": "لا. هو يطلب من Twitch البيانات الوصفية للبث كل بضع ثوانٍ فقط، وهذا يكفي لتحتسب Twitch تقدّم الدروب. لا يُحمَّل أي فيديو أو صوت، لذلك يستهلك القليل جدًا من الإنترنت.",
    "faq_q2": "لماذا تقدّمي متوقف أو غير صحيح؟",
    "faq_a2": "على الأرجح أن حساب Twitch نفسه يشاهد بثًا في مكان آخر، في المتصفح مثلًا. تدير Twitch تقدّم الدروب من جهتها، وهذا يربك المُعدِّن. تجنّب مشاهدة بثوث أخرى بهذا الحساب أثناء التعدين.",
    "faq_q3": "ماذا أستطيع أن أفعل من هذه الصفحة؟",
    "faq_a3": "في وضع العرض فقط يمكنك متابعة الدروب الحالي والحملات والإحصاءات والسجلات. وفي وضع التحكم يمكنك أيضًا الإيقاف المؤقت أو الاستئناف وضبط مؤقت للإيقاف وتعديل قائمتَي الأولوية والاستبعاد. الصفحة لا تستطيع تسجيل الدخول إلى حسابك أو استلام الدروبات بنفسها، فهي تعكس تطبيق سطح المكتب وتوجّهه فقط.",
    "faq_q4": "كيف أختار الألعاب التي يتم تعدينها؟",
    "faq_a4": "أضف ألعابًا إلى قائمة الأولوية، واختر وضع الأولوية، ثم اضغط إعادة التحميل ليطبّق التطبيق التغييرات. مع أي وضع غير قائمة الأولوية فقط، يأخذ المُعدِّن حملات أخرى متاحة أيضًا. الألعاب في قائمة الاستبعاد يتم تجاهلها.",
    "faq_q5": "لعبة أريدها لا تظهر. لماذا؟",
    "faq_a5": "يجب أن يكون حساب Twitch مرتبطًا بتلك اللعبة في صفحة الحملات على Twitch. بعد الربط اضغط إعادة التحميل، وعندها يمكن أخذ الحملة في الاعتبار.",
    "faq_q6": "هل الرابط خاص؟ وهل أستطيع استخدامه خارج المنزل؟",
    "faq_a6": "أي شخص لديه الرابط يستطيع رؤية نسختك، لذا تعامل معه كما تتعامل مع كلمة مرور. إذا شاركته بالخطأ فأنشئ رابطًا جديدًا من تطبيق سطح المكتب. افتراضيًا يعمل على شبكتك المحلية فقط. وللوصول من الخارج تحتاج إلى إعادة توجيه المنافذ أو VPN مثل Tailscale أو WireGuard.",
    "faq_q7": "هل الوقت المتبقي دقيق؟",
    "faq_a7": "لا، هو تقدير تقريبي. يعدّ تنازليًا دقيقة بدقيقة ويُعاد ضبطه عندما يحصل التطبيق على القيمة الحقيقية من Twitch. ولا يؤثر في سرعة التعدين.",
    "faq_q8": "ما هي السجلات؟",
    "faq_a8": "هي نفس الأسطر الظاهرة في مربع المخرجات في تطبيق سطح المكتب، للقراءة فقط.",
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
    "stats_weekly_title": "آخر 7 أيام",
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
    "tab_stats": "الإحصائيات",
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
    "connect_account_hint": "اربط حسابك للحصول على هذا الدروب",
    "show_details": "إظهار التفاصيل",
    "filter_all": "كل الحسابات",
    "filter_linked_only": "المرتبطة فقط",
    "filter_not_linked_only": "غير المرتبطة فقط",
    "exclude_list": "قائمة الاستبعاد",
    "account_status": "حالة الحساب",
    "all_channels": "جميع القنوات",
    "allowed_channels": "القنوات المسموح بها",
    "ends_at": "ينتهي في",
    "linked": "مرتبط",
    "not_linked": "غير مرتبط",
    "mine_unlinked": "تعدين الحملات غير المرتبطة",
    "mine_unlinked_hint": "تعدين الدروبس أيضًا عندما يفيد Twitch بأن حساب اللعبة غير مرتبط.",
    "range_day": "اليوم",
    "range_week": "7 أيام",
    "range_month": "30 يومًا",
    "range_3months": "3 أشهر",
    "range_all": "كل الوقت",
    "stats_drops_title": "الدروبس المستلمة",
    "stats_hours_title": "ساعات المشاهدة الموفرة",
    "stats_no_data": "لا توجد بيانات لهذه الفترة."
  },
  "ja": {
    "faq_title": "よくある質問",
    "faq_q1": "DropStreamは本当に配信を視聴しているのですか？",
    "faq_a1": "いいえ。数秒ごとにTwitchへ配信のメタデータを問い合わせるだけで、それだけでTwitchはドロップの進行を数えてくれます。映像も音声もダウンロードしないので、通信量はほとんどかかりません。",
    "faq_q2": "進行が止まる、または表示がおかしいのはなぜですか？",
    "faq_a2": "多くの場合、同じTwitchアカウントがブラウザなど別の場所で配信を見ているためです。ドロップの進行はTwitch側で管理されているので、マイナーが混乱します。マイニング中は、そのアカウントで他の配信を見ないでください。",
    "faq_q3": "このページでは何ができますか？",
    "faq_a3": "閲覧専用モードでは、現在のドロップ、キャンペーン、統計、ログを確認できます。操作モードでは、一時停止と再開、一時停止タイマーの設定、優先リストと除外リストの編集もできます。このページ自体はアカウントにログインしたりドロップを受け取ったりできず、デスクトップアプリの状態を映して操作するだけです。",
    "faq_q4": "マイニングするゲームはどう選びますか？",
    "faq_a4": "優先リストにゲームを追加し、優先モードを選んで、再読み込みを押すとアプリに反映されます。優先リストのみ以外のモードでは、他の受け取れるキャンペーンも対象になります。除外リストのゲームは無視されます。",
    "faq_q5": "欲しいゲームが表示されないのはなぜですか？",
    "faq_a5": "Twitchのキャンペーンページで、そのゲームとTwitchアカウントを連携しておく必要があります。連携したら再読み込みを押すと、キャンペーンが対象になります。",
    "faq_q6": "リンクは非公開ですか？外出先でも使えますか？",
    "faq_a6": "リンクを知っている人は誰でもあなたのインスタンスを見られるので、パスワードのように扱ってください。誤って共有した場合は、デスクトップアプリで新しいリンクを作成してください。初期設定ではローカルネットワーク内でのみ動作します。外部からはポート転送か、TailscaleやWireGuardのようなVPNが必要です。",
    "faq_q7": "残り時間は正確ですか？",
    "faq_a7": "いいえ、目安です。1分ずつカウントダウンし、アプリがTwitchから実際の値を取得すると補正されます。マイニングの速度には影響しません。",
    "faq_q8": "ログとは何ですか？",
    "faq_a8": "デスクトップアプリの出力欄に表示されているものと同じ行を、読み取り専用で表示します。",
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
    "stats_weekly_title": "過去7日間",
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
    "tab_stats": "統計",
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
    "connect_account_hint": "このドロップを獲得するにはアカウントを連携してください",
    "show_details": "詳細を表示",
    "filter_all": "すべてのアカウント",
    "filter_linked_only": "連携済みのみ",
    "filter_not_linked_only": "未連携のみ",
    "exclude_list": "除外リスト",
    "account_status": "アカウント状態",
    "all_channels": "すべてのチャンネル",
    "allowed_channels": "許可されたチャンネル",
    "ends_at": "終了日時",
    "linked": "リンク済み",
    "not_linked": "未リンク",
    "mine_unlinked": "未リンクのキャンペーンもマイニングする",
    "mine_unlinked_hint": "Twitchがゲームアカウント未リンクと報告した場合でもドロップをマイニングします。",
    "range_day": "今日",
    "range_week": "7日間",
    "range_month": "30日間",
    "range_3months": "3か月",
    "range_all": "全期間",
    "stats_drops_title": "獲得したドロップ",
    "stats_hours_title": "節約した視聴時間",
    "stats_no_data": "この期間のデータはありません。"
  },
  "zh-CN": {
    "faq_title": "常见问题",
    "faq_q1": "DropStream 真的在观看直播吗？",
    "faq_a1": "没有。它只是每隔几秒向 Twitch 请求一次直播的元数据，这就足以让 Twitch 计算掉宝进度。不会下载任何视频或音频，所以几乎不占用流量。",
    "faq_q2": "为什么我的进度卡住了或不对？",
    "faq_a2": "很可能是同一个 Twitch 账号在别处观看直播，比如在浏览器里。Twitch 在自己那边管理掉宝进度，这会让挖矿程序混乱。挖矿期间请避免用该账号观看其他直播。",
    "faq_q3": "我在这个页面上能做什么？",
    "faq_a3": "在仅查看模式下，你可以查看当前掉宝、活动、统计数据和日志。在控制模式下，还可以暂停或继续、设置暂停计时器，并编辑优先列表和排除列表。这个页面本身不能登录你的账号，也不能自己领取掉宝，它只是映射并操控桌面应用。",
    "faq_q4": "如何选择要挖哪些游戏？",
    "faq_a4": "把游戏加入优先列表，选择优先模式，然后点击重新加载，应用就会应用这些更改。在仅优先列表之外的任何模式下，程序也会接取其他可用的活动。排除列表中的游戏会被忽略。",
    "faq_q5": "我想要的游戏没有出现，为什么？",
    "faq_a5": "你的 Twitch 账号需要在 Twitch 的活动页面上与该游戏关联。关联后点击重新加载，该活动就可以被纳入。",
    "faq_q6": "链接是私密的吗？出门在外能用吗？",
    "faq_a6": "任何拿到链接的人都能看到你的实例，所以请像对待密码一样对待它。如果误分享了，可以在桌面应用里生成一个新的。默认只能在本地网络中使用。要从外部访问，需要端口转发，或使用 Tailscale、WireGuard 之类的 VPN。",
    "faq_q7": "剩余时间准确吗？",
    "faq_a7": "不准确，只是估算。它按分钟倒数，并在应用从 Twitch 获取到真实值时校正。这不会影响挖矿速度。",
    "faq_q8": "日志是什么？",
    "faq_a8": "就是桌面应用输出框里显示的那些行，以只读方式呈现。",
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
    "stats_weekly_title": "最近7天",
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
    "tab_stats": "统计",
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
    "connect_account_hint": "关联您的账户以获取此掉落物",
    "show_details": "显示详情",
    "filter_all": "所有账户",
    "filter_linked_only": "仅已关联",
    "filter_not_linked_only": "仅未关联",
    "exclude_list": "排除列表",
    "account_status": "账户状态",
    "all_channels": "所有频道",
    "allowed_channels": "允许的频道",
    "ends_at": "结束时间",
    "linked": "已关联",
    "not_linked": "未关联",
    "mine_unlinked": "挖掘未关联的活动",
    "mine_unlinked_hint": "即使 Twitch 报告游戏账户未关联，也继续挖掘掉落。",
    "range_day": "今天",
    "range_week": "7天",
    "range_month": "30天",
    "range_3months": "3个月",
    "range_all": "全部时间",
    "stats_drops_title": "已获得的掉落",
    "stats_hours_title": "节省的观看时长",
    "stats_no_data": "该时间段没有数据。"
  },
  "zh-TW": {
    "faq_title": "常見問題",
    "faq_q1": "DropStream 真的在觀看直播嗎？",
    "faq_a1": "沒有。它只是每隔幾秒向 Twitch 請求一次直播的中繼資料，這就足以讓 Twitch 計算掉寶進度。不會下載任何影片或音訊，所以幾乎不佔用流量。",
    "faq_q2": "為什麼我的進度卡住了或不對？",
    "faq_a2": "很可能是同一個 Twitch 帳號在別處觀看直播，例如在瀏覽器裡。Twitch 在自己那邊管理掉寶進度，這會讓挖礦程式混亂。挖礦期間請避免用該帳號觀看其他直播。",
    "faq_q3": "我在這個頁面上能做什麼？",
    "faq_a3": "在僅檢視模式下，你可以查看目前的掉寶、活動、統計資料和記錄。在控制模式下，還可以暫停或繼續、設定暫停計時器，並編輯優先清單和排除清單。這個頁面本身不能登入你的帳號，也不能自己領取掉寶，它只是映射並操控桌面應用程式。",
    "faq_q4": "如何選擇要挖哪些遊戲？",
    "faq_a4": "把遊戲加入優先清單，選擇優先模式，然後按重新載入，應用程式就會套用這些變更。在僅優先清單以外的任何模式下，程式也會接取其他可用的活動。排除清單中的遊戲會被忽略。",
    "faq_q5": "我想要的遊戲沒有出現，為什麼？",
    "faq_a5": "你的 Twitch 帳號需要在 Twitch 的活動頁面上與該遊戲連結。連結後按重新載入，該活動就可以被納入。",
    "faq_q6": "連結是私密的嗎？出門在外能用嗎？",
    "faq_a6": "任何拿到連結的人都能看到你的實例，所以請像對待密碼一樣對待它。如果誤分享了，可以在桌面應用程式裡產生新的。預設只能在區域網路中使用。要從外部存取，需要連接埠轉發，或使用 Tailscale、WireGuard 之類的 VPN。",
    "faq_q7": "剩餘時間準確嗎？",
    "faq_a7": "不準確，只是估算。它按分鐘倒數，並在應用程式從 Twitch 取得真實值時校正。這不會影響挖礦速度。",
    "faq_q8": "記錄是什麼？",
    "faq_a8": "就是桌面應用程式輸出框裡顯示的那些行，以唯讀方式呈現。",
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
    "stats_weekly_title": "最近7天",
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
    "tab_stats": "統計",
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
    "connect_account_hint": "連結您的帳號以取得此掉落物",
    "show_details": "顯示詳情",
    "filter_all": "所有帳號",
    "filter_linked_only": "僅已連結",
    "filter_not_linked_only": "僅未連結",
    "exclude_list": "排除清單",
    "account_status": "帳戶狀態",
    "all_channels": "所有頻道",
    "allowed_channels": "允許的頻道",
    "ends_at": "結束時間",
    "linked": "已連結",
    "not_linked": "未連結",
    "mine_unlinked": "挖掘未連結的活動",
    "mine_unlinked_hint": "即使 Twitch 回報遊戲帳戶未連結，也繼續挖掘掉落物。",
    "range_day": "今天",
    "range_week": "7天",
    "range_month": "30天",
    "range_3months": "3個月",
    "range_all": "全部時間",
    "stats_drops_title": "已獲得的掉落物",
    "stats_hours_title": "節省的觀看時數",
    "stats_no_data": "此期間沒有資料。"
  },
  "id": {
    "faq_title": "Tanya jawab",
    "faq_q1": "Apakah DropStream benar-benar menonton stream?",
    "faq_a1": "Tidak. Aplikasi ini hanya meminta metadata stream ke Twitch setiap beberapa detik, dan itu cukup bagi Twitch untuk menghitung progres drop. Tidak ada video atau audio yang diunduh, jadi hampir tidak memakai kuota.",
    "faq_q2": "Kenapa progres saya macet atau salah?",
    "faq_a2": "Kemungkinan besar akun Twitch yang sama sedang menonton stream di tempat lain, misalnya di browser. Twitch mengatur progres drop di sisinya, dan itu membingungkan miner. Hindari menonton stream lain dengan akun itu saat sedang menambang.",
    "faq_q3": "Apa yang bisa saya lakukan di halaman ini?",
    "faq_a3": "Di mode hanya lihat, kamu bisa memantau drop saat ini, kampanye, statistik, dan log. Di mode kontrol, kamu juga bisa menjeda atau melanjutkan, mengatur timer jeda, dan mengedit daftar prioritas serta pengecualian. Halaman ini tidak bisa masuk ke akunmu atau mengklaim drop sendiri, hanya mencerminkan dan mengarahkan aplikasi desktop.",
    "faq_q4": "Bagaimana cara memilih game yang ditambang?",
    "faq_a4": "Tambahkan game ke daftar prioritas, pilih mode prioritas, lalu tekan Muat ulang agar aplikasi menerapkan perubahan. Dengan mode apa pun selain hanya daftar prioritas, miner juga mengambil kampanye lain yang tersedia. Game di daftar pengecualian diabaikan.",
    "faq_q5": "Game yang saya mau tidak muncul. Kenapa?",
    "faq_a5": "Akun Twitch kamu harus tertaut ke game itu di halaman kampanye Twitch. Setelah tertaut, tekan Muat ulang dan kampanyenya bisa diproses.",
    "faq_q6": "Apakah tautannya privat? Bisakah dipakai di luar rumah?",
    "faq_a6": "Siapa pun yang punya tautan bisa melihat instansmu, jadi perlakukan seperti kata sandi. Jika terlanjur terbagikan, buat yang baru di aplikasi desktop. Secara bawaan hanya berfungsi di jaringan lokal. Untuk akses dari luar dibutuhkan port forwarding atau VPN seperti Tailscale atau WireGuard.",
    "faq_q7": "Apakah sisa waktunya akurat?",
    "faq_a7": "Tidak, hanya perkiraan. Hitungan mundurnya per menit dan disesuaikan ketika aplikasi mendapat nilai asli dari Twitch. Tidak memengaruhi kecepatan menambang.",
    "faq_q8": "Apa itu log?",
    "faq_a8": "Baris yang sama seperti di kotak Output pada aplikasi desktop, hanya bisa dibaca.",
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
    "stats_weekly_title": "7 hari terakhir",
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
    "tab_stats": "Statistik",
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
    "connect_account_hint": "Hubungkan akunmu untuk mendapatkan drop ini",
    "show_details": "Tampilkan detail",
    "filter_all": "Semua akun",
    "filter_linked_only": "Hanya yang tertaut",
    "filter_not_linked_only": "Hanya yang belum tertaut",
    "exclude_list": "Daftar pengecualian",
    "account_status": "Status akun",
    "all_channels": "Semua saluran",
    "allowed_channels": "Saluran yang diizinkan",
    "ends_at": "Berakhir pada",
    "linked": "Tertaut",
    "not_linked": "Tidak tertaut",
    "mine_unlinked": "Tambang kampanye yang tidak tertaut",
    "mine_unlinked_hint": "Tetap menambang drop meskipun Twitch melaporkan akun game tidak tertaut.",
    "range_day": "Hari ini",
    "range_week": "7 hari",
    "range_month": "30 hari",
    "range_3months": "3 bulan",
    "range_all": "Sepanjang waktu",
    "stats_drops_title": "Drop yang diperoleh",
    "stats_hours_title": "Jam tontonan yang dihemat",
    "stats_no_data": "Tidak ada data untuk periode ini."
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
let statsRange = localStorage.getItem("statsRange") || "week";
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
    // canvases report 0 width while their tab is hidden (display:none), so redraw
    // once this tab becomes visible instead of relying on the last background poll
    if (btn.dataset.tab === "stats") {
      loadStats(statsRange);
    }
    // starts log polling when the Logs tab opens, stops it when any other tab is opened
    syncLogsPolling();
  });
});

let logsPollTimer = null;
let lastLogsText = null;
async function loadLogs() {
  try {
    const res = await fetch(base + "/api/logs");
    if (!res.ok) return;
    const data = await res.json();
    const box = document.getElementById("logs-box");
    const wasScrolledDown = box.scrollTop + box.clientHeight >= box.scrollHeight - 4;
    const text = (data.lines || []).join("\\n");
    if (text === lastLogsText) return;  // nothing new, don't touch the DOM
    lastLogsText = text;
    box.textContent = text;
    if (wasScrolledDown) box.scrollTop = box.scrollHeight;
  } catch (e) {
    // logs are non-critical; ignore transient fetch errors
  }
}
// Keep the log view live only while its tab is open and the page is visible. The timer is
// fully stopped otherwise (instead of ticking and checking), so an idle or backgrounded
// page doesn't wake the CPU every few seconds.
function syncLogsPolling() {
  const want = !document.hidden && document.getElementById("logs-tab-btn").classList.contains("active");
  if (want && !logsPollTimer) {
    loadLogs();
    logsPollTimer = setInterval(loadLogs, 5000);
  } else if (!want && logsPollTimer) {
    clearInterval(logsPollTimer);
    logsPollTimer = null;
  }
}

function setStatsRangeButtons() {
  document.querySelectorAll("#stats-filter button").forEach(b => {
    b.classList.toggle("active", b.dataset.range === statsRange);
  });
}
setStatsRangeButtons();
document.querySelectorAll("#stats-filter button").forEach(btn => {
  btn.addEventListener("click", () => {
    statsRange = btn.dataset.range;
    localStorage.setItem("statsRange", statsRange);
    setStatsRangeButtons();
    loadStats(statsRange);
  });
});

async function loadStats(range) {
  let data;
  try {
    const res = await fetch(`${base}/api/stats?range=${encodeURIComponent(range)}`);
    if (!res.ok) return;
    data = await res.json();
  } catch (e) {
    return;
  }
  if (statsRange !== range) return; // a newer filter click already superseded this one
  document.getElementById("stat-range-total").textContent = data.total_drops;
  document.getElementById("stat-range-hours").textContent = data.hours_saved;
  const labels = data.series.map(p => p.label);
  drawBarChart("chart-weekly", labels, data.series.map(p => p.drops));
  drawBarChart("chart-hours", labels, data.series.map(p => p.hours));
  document.getElementById("chart-drops-empty").style.display = data.total_drops ? "none" : "block";
  document.getElementById("chart-hours-empty").style.display = data.hours_saved ? "none" : "block";
  renderRankList(data.per_game, "rank-list-stats");
  document.getElementById("rank-list-empty").style.display = data.per_game.length ? "none" : "block";
}

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
const mineUnlinkedToggle = document.getElementById("mine-unlinked-toggle");
let applyingMineUnlinked = false;
mineUnlinkedToggle.addEventListener("change", async () => {
  applyingMineUnlinked = true;
  await post("/api/mine_unlinked", { enabled: mineUnlinkedToggle.checked });
  applyingMineUnlinked = false;
});
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
  mineUnlinkedToggle.disabled = !enabled;
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

// Skip DOM rebuilds when the data hasn't changed since the last render. The page polls every
// few seconds, and rebuilding lists (with images) each time means needless layout work, image
// re-decoding and garbage collection, which costs CPU/battery on the viewing device.
const _renderSigs = new Map();
function renderChanged(key, data) {
  const sig = JSON.stringify([currentLang, data]);
  if (_renderSigs.get(key) === sig) return false;
  _renderSigs.set(key, sig);
  return true;
}

function drawBarChart(canvasId, labels, values) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  // match the canvas's actual rendered (CSS) size so it stays crisp on any screen
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const w = rect.width, h = rect.height;
  ctx.clearRect(0, 0, w, h);
  if (!labels.length) return;
  const max = Math.max(1, ...values);
  // padTop leaves room for the value label drawn above the tallest bar (11px font + gap),
  // otherwise that label gets cut off at the top of the canvas
  const padBottom = 24, padTop = 24;
  const barAreaH = h - padBottom - padTop;
  const barW = w / labels.length;
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#7c5cff";
  const textColor = getComputedStyle(document.documentElement).getPropertyValue("--dim").trim() || "#888";
  ctx.font = "11px sans-serif";
  ctx.textAlign = "center";
  // skip labels when bars are too narrow for them to fit without overlapping
  // (e.g. ~90 daily bars for the 3-month view) - show at most one label per ~46px
  const labelStep = Math.max(1, Math.ceil(46 / barW));
  labels.forEach((label, i) => {
    const value = values[i];
    const barH = (value / max) * barAreaH;
    const x = i * barW + barW * 0.2;
    const barW2 = barW * 0.6;
    const y = padTop + barAreaH - barH;
    ctx.fillStyle = accent;
    ctx.fillRect(x, y, barW2, barH);
    ctx.fillStyle = textColor;
    if (value > 0 && barW > 18) ctx.fillText(String(value), x + barW2 / 2, y - 4);
    if (i % labelStep === 0) {
      ctx.fillText(String(label).slice(0, 10), i * barW + barW / 2, h - 6);
    }
  });
}

function renderRankList(perGame, listId = "rank-list") {
  const list = document.getElementById(listId);
  if (!list) return;
  if (!renderChanged("rank:" + listId, perGame)) return;
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
  if (!renderChanged("campaigns", campaigns)) return;
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
    const titleRow = document.createElement("div");
    titleRow.className = "campaign-title-row";
    const title = document.createElement("div");
    title.className = "campaign-title";
    title.textContent = c.name;
    titleRow.appendChild(title);
    if (!c.linked) {
      const dot = document.createElement("span");
      dot.className = "unlinked-dot";
      dot.title = t("not_linked");
      titleRow.appendChild(dot);
    }
    const expandBtn = document.createElement("button");
    expandBtn.className = "expand-arrow";
    expandBtn.type = "button";
    expandBtn.textContent = "▼";
    expandBtn.title = t("show_details");
    expandBtn.addEventListener("click", () => {
      const isOpen = details.classList.toggle("open");
      expandBtn.classList.toggle("open", isOpen);
    });
    titleRow.appendChild(expandBtn);
    body.appendChild(titleRow);
    const game = document.createElement("div");
    game.className = "campaign-game";
    game.textContent = c.game + " - " + c.claimed_drops + "/" + c.total_drops;
    body.appendChild(game);
    const progressRow = document.createElement("div");
    progressRow.className = "campaign-progress-row";
    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("div");
    fill.style.width = pct(c.progress);
    bar.appendChild(fill);
    progressRow.appendChild(bar);
    const pctLabel = document.createElement("div");
    pctLabel.className = "campaign-pct";
    pctLabel.textContent = pct(c.progress);
    progressRow.appendChild(pctLabel);
    body.appendChild(progressRow);
    const details = document.createElement("div");
    details.className = "campaign-details";
    progressRow.addEventListener("click", () => {
      const isOpen = details.classList.toggle("open");
      expandBtn.classList.toggle("open", isOpen);
    });
    const linkRow = document.createElement("div");
    linkRow.className = "row";
    const linkLabel = document.createElement("div");
    linkLabel.className = "label";
    linkLabel.textContent = t("account_status") + ":";
    linkRow.appendChild(linkLabel);
    const linkValue = document.createElement("a");
    linkValue.href = c.link_url || "#";
    linkValue.target = "_blank";
    linkValue.rel = "noopener";
    linkValue.className = "link-status " + (c.linked ? "linked" : "not-linked");
    linkValue.textContent = c.linked ? t("linked") : t("not_linked");
    linkRow.appendChild(linkValue);
    if (!c.linked && c.link_url) {
      const hint = document.createElement("span");
      hint.className = "link-status not-linked-hint";
      hint.textContent = t("connect_account_hint");
      linkRow.appendChild(hint);
    }
    details.appendChild(linkRow);
    const acRow = document.createElement("div");
    acRow.className = "row";
    const acLabel = document.createElement("div");
    acLabel.className = "label";
    acLabel.textContent = t("allowed_channels") + ":";
    acRow.appendChild(acLabel);
    const acValue = document.createElement("div");
    acValue.className = "value";
    if (c.allowed_channels.length) {
      c.allowed_channels.forEach((chName, i) => {
        if (i > 0) acValue.appendChild(document.createTextNode(", "));
        const chLink = document.createElement("a");
        chLink.className = "link-hover";
        chLink.href = "https://twitch.tv/" + encodeURIComponent(chName);
        chLink.target = "_blank";
        chLink.rel = "noopener";
        chLink.textContent = chName;
        acValue.appendChild(chLink);
      });
    } else {
      acValue.textContent = t("all_channels");
    }
    acRow.appendChild(acValue);
    details.appendChild(acRow);
    const endsRow = document.createElement("div");
    endsRow.className = "row";
    const endsLabel = document.createElement("div");
    endsLabel.className = "label";
    endsLabel.textContent = t("ends_at") + ":";
    endsRow.appendChild(endsLabel);
    const endsValue = document.createElement("div");
    endsValue.className = "value";
    endsValue.textContent = new Date(c.ends_at).toLocaleString();
    endsRow.appendChild(endsValue);
    details.appendChild(endsRow);
    body.appendChild(details);
    const thumbs = document.createElement("div");
    thumbs.className = "drop-thumbs";
    for (const d of c.drops) {
      const thumb = document.createElement("div");
      thumb.className = "drop-thumb" + (d.claimed ? " claimed" : "");
      thumb.title = d.rewards + (d.claimed ? " (" + t("claimed") + ")" : " " + pct(d.progress));
      thumb.addEventListener("click", () => openDropModal(d));
      const dimg = document.createElement("img");
      dimg.src = d.image_url || "";
      dimg.alt = "";
      thumb.appendChild(dimg);
      if (d.claimed) {
        const check = document.createElement("div");
        check.className = "check";
        check.textContent = "✓";
        thumb.appendChild(check);
      } else if (d.progress > 0) {
        const pctBadge = document.createElement("div");
        pctBadge.className = "drop-thumb-pct";
        pctBadge.textContent = pct(d.progress);
        thumb.appendChild(pctBadge);
      }
      thumbs.appendChild(thumb);
    }
    body.appendChild(thumbs);
    card.appendChild(body);
    list.appendChild(card);
  }
}

function renderOtherDrops(otherDrops) {
  const row = document.getElementById("other-drops-row");
  if (!renderChanged("other-drops", otherDrops)) return;
  row.innerHTML = "";
  for (const d of otherDrops) {
    const thumb = document.createElement("div");
    thumb.className = "other-drop-thumb" + (d.claimed ? " claimed" : "");
    thumb.title = d.rewards + (d.claimed ? " (" + t("claimed") + ")" : " " + pct(d.progress));
    const img = document.createElement("img");
    img.src = d.image_url || "";
    img.alt = "";
    thumb.appendChild(img);
    thumb.addEventListener("click", () => openDropModal(d));
    row.appendChild(thumb);
  }
}

function openDropModal(d) {
  document.getElementById("drop-modal-image").src = d.image_url || "";
  document.getElementById("drop-modal-title").textContent = d.rewards;
  const claimed = d.claimed;
  document.getElementById("drop-modal-status").textContent = claimed
    ? t("claimed") : pct(d.progress) + (d.remaining_minutes != null ? " - " + fmtMinutes(d.remaining_minutes) : "");
  document.getElementById("drop-modal-bar").style.width = pct(d.progress);
  const minutesEl = document.getElementById("drop-modal-minutes");
  if (d.required_minutes != null) {
    minutesEl.textContent = (d.current_minutes || 0) + " / " + d.required_minutes + " min";
    minutesEl.style.display = "";
  } else {
    minutesEl.style.display = "none";
  }
  const benefitsEl = document.getElementById("drop-modal-benefits");
  benefitsEl.innerHTML = "";
  for (const b of (d.benefits || [])) {
    const img = document.createElement("img");
    img.src = b.image_url || "";
    img.alt = b.name || "";
    img.title = b.name || "";
    benefitsEl.appendChild(img);
  }
  document.getElementById("drop-modal-overlay").style.display = "flex";
}

document.getElementById("drop-modal-close").addEventListener("click", () => {
  document.getElementById("drop-modal-overlay").style.display = "none";
});
document.getElementById("drop-modal-overlay").addEventListener("click", (e) => {
  if (e.target.id === "drop-modal-overlay") {
    document.getElementById("drop-modal-overlay").style.display = "none";
  }
});

function renderEditList(listEl, emptyEl, games, kind, controlEnabled) {
  if (!renderChanged("edit:" + kind, [games, !!controlEnabled])) return;
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
  const linkFilter = document.getElementById("campaign-link-filter").value;
  if (linkFilter === "linked") {
    list = list.filter(c => c.linked);
  } else if (linkFilter === "not_linked") {
    list = list.filter(c => !c.linked);
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
    const controlTabBtn = document.getElementById("control-tab-btn");
    controlTabBtn.style.display = s.control_enabled ? "" : "none";
    document.getElementById("pause-card").style.display = s.control_enabled ? "block" : "none";
    if (!s.control_enabled && controlTabBtn.classList.contains("active")) {
      controlTabBtn.classList.remove("active");
      document.getElementById("tab-control").classList.remove("active");
      document.querySelector('.tab-btn[data-tab="dashboard"]').classList.add("active");
      document.getElementById("tab-dashboard").classList.add("active");
    }
    const logsTabBtn = document.getElementById("logs-tab-btn");
    logsTabBtn.style.display = s.logs_enabled ? "" : "none";
    if (!s.logs_enabled && logsTabBtn.classList.contains("active")) {
      logsTabBtn.classList.remove("active");
      document.getElementById("tab-logs").classList.remove("active");
      document.querySelector('.tab-btn[data-tab="dashboard"]').classList.add("active");
      document.getElementById("tab-dashboard").classList.add("active");
      syncLogsPolling();
    }
    if (s.app && s.app.version) {
      document.getElementById("help-version").textContent = "DropStream v" + s.app.version;
    }

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
      renderOtherDrops(s.current_drop.other_drops || []);
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
      const channelNameEl = document.getElementById("channel-name");
      channelNameEl.textContent = label;
      channelNameEl.href = "https://twitch.tv/" + encodeURIComponent(s.watching_channel.name);
    } else {
      channelCard.style.display = "none";
    }

    document.getElementById("stat-total").textContent = s.stats.total_drops;
    document.getElementById("stat-hours").textContent = s.stats.hours_saved;

    if (!applyingMode) modeSelect.value = s.priority_mode.value;

    renderRankList(s.stats.per_game);

    // wire the "mine unlinked campaigns" toggle to the current setting, without
    // fighting the user while they're actively dragging it (mirrors the
    // applyingMode guard used for the priority-mode select above)
    if (!applyingMineUnlinked) mineUnlinkedToggle.checked = !!s.mine_unlinked_campaigns;
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
    if (renderChanged("game-options", s.available_games || [])) {
    options.innerHTML = "";
    for (const game of (s.available_games || [])) {
      const opt = document.createElement("option");
      opt.value = game;
      options.appendChild(opt);
    }
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
document.getElementById("campaign-link-filter").addEventListener("change", applyCampaignFilters);

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
    syncLogsPolling();
  } else if (!refreshTimer) {
    refresh();
    refreshCampaigns();
    refreshTimer = setInterval(refresh, 4000);
    campaignsTimer = setInterval(refreshCampaigns, 15000);
    syncLogsPolling();
  }
});
</script>
</body>
</html>
"""
