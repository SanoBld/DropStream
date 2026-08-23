from __future__ import annotations

import sys
import logging
import subprocess
from enum import Enum
from datetime import datetime, time as dtime

logger = logging.getLogger("TwitchDrops")


class PowerAction(Enum):
    NONE = "none"
    SLEEP = "sleep"
    SHUTDOWN = "shutdown"


def parse_hhmm(value: str) -> dtime | None:
    # parses "HH:MM" into a time object, returns None if invalid/empty
    try:
        h, m = value.strip().split(":")
        return dtime(hour=int(h), minute=int(m))
    except (ValueError, AttributeError):
        return None


def within_window(start: str, end: str, now: datetime | None = None) -> bool:
    # returns True if 'now' falls within the [start, end) daily window
    # supports overnight windows (e.g. 22:00 -> 06:00)
    t_start = parse_hhmm(start)
    t_end = parse_hhmm(end)
    if t_start is None or t_end is None or t_start == t_end:
        # no valid/complete window configured: always allowed
        return True
    now_t = (now or datetime.now()).time()
    if t_start < t_end:
        return t_start <= now_t < t_end
    # window wraps past midnight
    return now_t >= t_start or now_t < t_end


def run_power_action(action: PowerAction) -> None:
    # triggers OS sleep/shutdown, best-effort, never raises
    if action is PowerAction.NONE:
        return
    try:
        if sys.platform == "win32":
            if action is PowerAction.SLEEP:
                subprocess.run(
                    ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], check=False
                )
            else:
                subprocess.run(["shutdown", "/s", "/t", "30"], check=False)
        elif sys.platform == "darwin":
            if action is PowerAction.SLEEP:
                subprocess.run(["pmset", "sleepnow"], check=False)
            else:
                subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)
        else:
            if action is PowerAction.SLEEP:
                subprocess.run(["systemctl", "suspend"], check=False)
            else:
                subprocess.run(["systemctl", "poweroff"], check=False)
    except Exception:
        logger.exception("Power action failed")
