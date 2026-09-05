from __future__ import annotations

import time
import logging
from typing import Any

logger = logging.getLogger("DropStream")

try:
    from pypresence import Presence
    from pypresence.exceptions import PyPresenceException
except ImportError:
    Presence = None  # type: ignore[assignment,misc]
    PyPresenceException = Exception  # type: ignore[assignment,misc]

# DropStream's own Discord Application, used by default so Rich Presence works
# out of the box. Users who prefer their own branded application can still
# override this with their own client ID in Settings.
DEFAULT_CLIENT_ID = "1545749912262680717"


class DiscordRPC:
    """
    Thin, best-effort wrapper around pypresence: any failure here (Discord not
    running, pypresence missing, IPC hiccup, ...) must never affect mining -
    every public method swallows its own errors and just disables itself.
    """

    def __init__(self, client_id: str = DEFAULT_CLIENT_ID) -> None:
        self._client_id = client_id or DEFAULT_CLIENT_ID
        self._rpc: Any = None
        self._connected = False
        self._start_time = time.time()

    @property
    def available(self) -> bool:
        return Presence is not None and bool(self._client_id)

    def connect(self) -> bool:
        if Presence is None:
            logger.warning("pypresence isn't installed - Discord Rich Presence is unavailable")
            return False
        if not self._client_id:
            logger.warning("Discord Rich Presence: no Application ID set in Settings")
            return False
        try:
            self._rpc = Presence(self._client_id)
            self._rpc.connect()
            self._connected = True
            self._start_time = time.time()
            return True
        except Exception:
            logger.info("Discord Rich Presence: couldn't connect (is Discord running?)")
            self._connected = False
            return False

    def update(self, *, channel_name: str, game_name: str | None, image_url: str | None) -> None:
        if not self._connected or self._rpc is None:
            return
        try:
            self._rpc.update(
                state=f"Watching {channel_name}",
                details=game_name or "Mining drops",
                large_image=image_url or "dropstream_logo",
                large_text=game_name or "DropStream",
                start=self._start_time,
                buttons=[{"label": "Open on Twitch", "url": f"https://twitch.tv/{channel_name}"}],
            )
        except (PyPresenceException, Exception):
            # Discord was closed mid-session, or the pipe broke - just drop the
            # connection, next connect() attempt will re-establish it
            logger.info("Discord Rich Presence: lost connection to Discord")
            self._connected = False

    def clear(self) -> None:
        if self._connected and self._rpc is not None:
            try:
                self._rpc.clear()
            except Exception:
                pass

    def close(self) -> None:
        if self._connected and self._rpc is not None:
            try:
                self._rpc.close()
            except Exception:
                pass
        self._connected = False
