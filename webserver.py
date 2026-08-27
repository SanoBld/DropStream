from __future__ import annotations

import json
import socket
import secrets
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from constants import PriorityMode

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


class WebDashboard:
    """
    Optional, local HTTP server exposing a small read/control dashboard, meant to be
    reached from other devices on the same network (or a remote one, through port
    forwarding/a tunnel, at the user's own risk). Disabled by default.
    Every route is namespaced under the current secret token, so the link itself is
    what grants access - there's no separate login step.
    """

    def __init__(self, twitch: Twitch) -> None:
        self._twitch: Twitch = twitch
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

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
        app = web.Application()
        app.add_routes([
            web.get(f"/{token}", self._handle_index),
            web.get(f"/{token}/", self._handle_index),
            web.get(f"/{token}/api/state", self._handle_state),
            web.post(f"/{token}/api/pause", self._handle_pause),
            web.post(f"/{token}/api/resume", self._handle_resume),
            web.post(f"/{token}/api/priority_mode", self._handle_priority_mode),
        ])
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

    # -- state snapshot --

    def _state_dict(self) -> dict[str, Any]:
        twitch = self._twitch
        settings = twitch.settings
        drop = twitch.gui.progress._drop
        current_drop: dict[str, Any] | None = None
        if drop is not None:
            campaign = drop.campaign
            current_drop = {
                "game": campaign.game.name,
                "campaign": campaign.name,
                "rewards": drop.rewards_text(),
                "drop_progress": round(drop.progress, 4),
                "campaign_progress": round(campaign.progress, 4),
                "claimed_drops": campaign.claimed_drops,
                "total_drops": campaign.total_drops,
            }
        watching = twitch.watching_channel.get_with_default(None)
        watching_channel: dict[str, Any] | None = None
        if watching is not None:
            watching_channel = {
                "name": watching.name,
                "game": watching.game.name if watching.game is not None else None,
                "viewers": watching.viewers,
            }
        priority_mode = settings.priority_mode
        priority_value = priority_mode.value if hasattr(priority_mode, "value") else priority_mode
        return {
            "app": {"name": "DropStream", "version": self._version()},
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
            "stats": {
                "total_drops": twitch.stats.total_drops_claimed(),
                "hours_saved": round(twitch.stats.total_hours_saved(), 2),
                "weekly": twitch.stats.weekly_progress(),
                "per_game": twitch.stats.drops_per_game(),
            },
        }

    @staticmethod
    def _version() -> str:
        from version import __version__
        return __version__

    # -- handlers --

    async def _handle_index(self, request: web.Request) -> web.Response:
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    async def _handle_state(self, request: web.Request) -> web.Response:
        return web.json_response(self._state_dict())

    async def _handle_pause(self, request: web.Request) -> web.Response:
        self._twitch.pause()
        return web.json_response(self._state_dict())

    async def _handle_resume(self, request: web.Request) -> web.Response:
        self._twitch.resume()
        return web.json_response(self._state_dict())

    async def _handle_priority_mode(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            mode_value = int(body["mode"])
            self._twitch.settings.priority_mode = PriorityMode(mode_value)
        except (json.JSONDecodeError, KeyError, ValueError):
            return web.json_response({"error": "invalid mode"}, status=400)
        return web.json_response(self._state_dict())


# Single-file dashboard: plain HTML/CSS/JS, no build step, no external requests.
# Kept intentionally simple (polling, not websockets) to keep the server dependency-free.
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DropStream - Remote Dashboard</title>
<style>
  :root {
    --bg: #0e0e10; --card: #18181b; --border: #2f2f35; --fg: #efeff1;
    --dim: #adadb8; --accent: #9147ff; --green: #2ecc71; --amber: #e0a800;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px; background: var(--bg); color: var(--fg);
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
  }
  .wrap { max-width: 760px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: var(--dim); font-size: 13px; margin-bottom: 20px; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px; margin-bottom: 14px;
  }
  .row { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  .label { color: var(--dim); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  .value { font-size: 15px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 8px; }
  .bar { background: #303034; border-radius: 6px; overflow: hidden; height: 10px; margin-top: 6px; }
  .bar > div { background: var(--accent); height: 100%; transition: width .3s; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  button {
    background: var(--accent); color: #fff; border: none; border-radius: 6px;
    padding: 10px 16px; font-size: 14px; cursor: pointer;
  }
  button:hover { opacity: .9; }
  button.secondary { background: #303034; }
  select {
    background: #303034; color: var(--fg); border: 1px solid var(--border);
    border-radius: 6px; padding: 8px; font-size: 13px; width: 100%;
  }
  ul.chips { list-style: none; padding: 0; margin: 8px 0 0; display: flex; flex-wrap: wrap; gap: 6px; }
  ul.chips li { background: #303034; padding: 4px 10px; border-radius: 999px; font-size: 12px; }
  .muted { color: var(--dim); font-size: 13px; }
  .err { color: #ff6b6b; font-size: 13px; margin-top: 8px; display: none; }
</style>
</head>
<body>
<div class="wrap">
  <h1>DropStream</h1>
  <div class="sub">Remote dashboard - anyone with this link can view and control this instance.</div>

  <div class="card">
    <div class="row">
      <div><span class="dot" id="status-dot"></span><span class="value" id="status-text">Loading...</span></div>
      <div>
        <button id="pause-btn">Pause</button>
        <button id="resume-btn" class="secondary">Resume</button>
      </div>
    </div>
  </div>

  <div class="card" id="drop-card" style="display:none">
    <div class="label">Currently mining</div>
    <div class="value" id="drop-game" style="font-size:18px;margin:4px 0"></div>
    <div class="muted" id="drop-rewards"></div>
    <div class="label" style="margin-top:12px">Drop progress <span id="drop-pct"></span></div>
    <div class="bar"><div id="drop-bar" style="width:0%"></div></div>
    <div class="label" style="margin-top:12px">Campaign progress <span id="campaign-pct"></span></div>
    <div class="bar"><div id="campaign-bar" style="width:0%"></div></div>
  </div>

  <div class="card" id="channel-card" style="display:none">
    <div class="label">Watching</div>
    <div class="value" id="channel-name"></div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="label">Total drops claimed</div>
      <div class="value" id="stat-total" style="font-size:22px">-</div>
    </div>
    <div class="card">
      <div class="label">Watch hours saved</div>
      <div class="value" id="stat-hours" style="font-size:22px">-</div>
    </div>
  </div>

  <div class="card">
    <div class="label">Priority mode</div>
    <select id="priority-mode"></select>
  </div>

  <div class="card">
    <div class="label">Priority list</div>
    <ul class="chips" id="priority-list"></ul>
  </div>

  <div class="err" id="error-box">Connection lost - retrying...</div>
</div>

<script>
const base = location.pathname.replace(/\\/$/, "");
const PRIORITY_MODES = [
  [0, "Priority list only"],
  [3, "Priority list only, then continue with the rest"],
  [1, "Ending soonest"],
  [4, "Priority list first, then ending soonest"],
  [2, "Low availability first"],
  [5, "Priority list first, then low availability"],
];
const modeSelect = document.getElementById("priority-mode");
for (const [value, label] of PRIORITY_MODES) {
  const opt = document.createElement("option");
  opt.value = value;
  opt.textContent = label;
  modeSelect.appendChild(opt);
}
let applyingMode = false;

function pct(x) { return (x * 100).toFixed(1) + "%"; }

async function refresh() {
  try {
    const res = await fetch(base + "/api/state");
    if (!res.ok) throw new Error("bad response");
    const s = await res.json();
    document.getElementById("error-box").style.display = "none";

    const dot = document.getElementById("status-dot");
    const text = document.getElementById("status-text");
    dot.style.background = s.paused ? "var(--amber)" : "var(--green)";
    text.textContent = s.paused ? "Paused" : "Mining";

    const dropCard = document.getElementById("drop-card");
    if (s.current_drop) {
      dropCard.style.display = "block";
      document.getElementById("drop-game").textContent = s.current_drop.game;
      document.getElementById("drop-rewards").textContent = s.current_drop.rewards;
      document.getElementById("drop-pct").textContent = pct(s.current_drop.drop_progress);
      document.getElementById("drop-bar").style.width = pct(s.current_drop.drop_progress);
      document.getElementById("campaign-pct").textContent =
        pct(s.current_drop.campaign_progress) + " (" + s.current_drop.claimed_drops +
        "/" + s.current_drop.total_drops + ")";
      document.getElementById("campaign-bar").style.width = pct(s.current_drop.campaign_progress);
    } else {
      dropCard.style.display = "none";
    }

    const channelCard = document.getElementById("channel-card");
    if (s.watching_channel) {
      channelCard.style.display = "block";
      let label = s.watching_channel.name;
      if (s.watching_channel.game) label += " - " + s.watching_channel.game;
      if (s.watching_channel.viewers != null) label += " (" + s.watching_channel.viewers + " viewers)";
      document.getElementById("channel-name").textContent = label;
    } else {
      channelCard.style.display = "none";
    }

    document.getElementById("stat-total").textContent = s.stats.total_drops;
    document.getElementById("stat-hours").textContent = s.stats.hours_saved;

    if (!applyingMode) modeSelect.value = s.priority_mode.value;

    const list = document.getElementById("priority-list");
    list.innerHTML = "";
    for (const game of s.priority_list) {
      const li = document.createElement("li");
      li.textContent = game;
      list.appendChild(li);
    }
  } catch (e) {
    document.getElementById("error-box").style.display = "block";
  }
}

document.getElementById("pause-btn").addEventListener("click", async () => {
  await fetch(base + "/api/pause", { method: "POST" });
  refresh();
});
document.getElementById("resume-btn").addEventListener("click", async () => {
  await fetch(base + "/api/resume", { method: "POST" });
  refresh();
});
modeSelect.addEventListener("change", async () => {
  applyingMode = true;
  await fetch(base + "/api/priority_mode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode: parseInt(modeSelect.value, 10) }),
  });
  applyingMode = false;
  refresh();
});

refresh();
setInterval(refresh, 4000);
</script>
</body>
</html>
"""
