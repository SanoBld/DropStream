from __future__ import annotations

from typing import Any, TypedDict, TYPE_CHECKING

from yarl import URL

from utils import json_load, json_save
from constants import SETTINGS_PATH, DEFAULT_LANG, PriorityMode
from scheduler import PowerAction

if TYPE_CHECKING:
    from main import ParsedArgs


class SettingsFile(TypedDict):
    proxy: URL
    language: str
    dark_mode: bool  # deprecated, kept for migrating pre-existing settings files
    theme: str  # "light" | "dark" | "auto" | "modern_light" | "modern_dark" | "modern_auto"
    use_system_accent: bool
    exclude: set[str]
    priority: list[str]
    autostart_tray: bool
    connection_quality: int
    tray_notifications: bool
    enable_badges_emotes: bool
    available_drops_check: bool
    priority_mode: PriorityMode
    # scheduler: daily run window + action once nothing's left to farm today
    schedule_enabled: bool
    schedule_start: str  # "HH:MM"
    schedule_end: str  # "HH:MM"
    auto_action: PowerAction
    # optional remote web dashboard: view/control this instance from another device
    web_server_enabled: bool
    web_server_port: int
    web_server_token: str  # secret path segment, part of the share link; empty = not generated yet
    web_server_allow_control: bool  # False = visitors can only view; True = they can also act
    web_server_password: str  # optional extra check gating control actions; empty = no password
    web_server_public: bool  # also show a public-IP link, for sharing beyond the LAN
    web_server_show_viewers: bool  # show live viewer count on the dashboard page itself
    low_power_tray_mode: bool  # aggressively trim RAM/CPU usage while minimized to the tray
    # if a critical task dies even after its built-in retries, optionally relaunch the app
    # automatically after a delay, instead of leaving it sitting on a "Terminated" screen
    auto_restart_enabled: bool
    auto_restart_minutes: int


default_settings: SettingsFile = {
    "proxy": URL(),
    "priority": [],
    "exclude": set(),
    "dark_mode": False,
    "theme": "auto",
    "use_system_accent": False,
    "autostart_tray": False,
    "connection_quality": 1,
    "language": DEFAULT_LANG,
    "tray_notifications": True,
    "enable_badges_emotes": False,
    "available_drops_check": False,
    "priority_mode": PriorityMode.PRIORITY_ONLY,
    "schedule_enabled": False,
    "schedule_start": "",
    "schedule_end": "",
    "auto_action": PowerAction.NONE,
    "web_server_enabled": False,
    "web_server_port": 21000,
    "web_server_token": "",
    "web_server_allow_control": False,
    "web_server_password": "",
    "web_server_public": False,
    "web_server_show_viewers": False,
    "low_power_tray_mode": False,
    "auto_restart_enabled": False,
    "auto_restart_minutes": 5,
}


class Settings:
    # from args
    log: bool
    tray: bool
    dump: bool
    # args properties
    debug_ws: int
    debug_gql: int
    logging_level: int
    # from settings file
    proxy: URL
    language: str
    dark_mode: bool
    theme: str
    exclude: set[str]
    use_system_accent: bool
    priority: list[str]
    autostart_tray: bool
    connection_quality: int
    tray_notifications: bool
    enable_badges_emotes: bool
    available_drops_check: bool
    priority_mode: PriorityMode
    schedule_enabled: bool
    schedule_start: str
    schedule_end: str
    auto_action: PowerAction
    web_server_enabled: bool
    web_server_port: int
    web_server_token: str
    web_server_allow_control: bool
    web_server_password: str
    web_server_public: bool
    web_server_show_viewers: bool
    low_power_tray_mode: bool
    auto_restart_enabled: bool
    auto_restart_minutes: int

    PASSTHROUGH = ("_settings", "_args", "_altered")

    def __init__(self, args: ParsedArgs):
        self._settings: SettingsFile = json_load(SETTINGS_PATH, default_settings)
        self._args: ParsedArgs = args
        self._altered: bool = False

    # default logic of reading settings is to check args first, then the settings file
    def __getattr__(self, name: str, /) -> Any:
        if name in self.PASSTHROUGH:
            # passthrough
            return getattr(super(), name)
        elif hasattr(self._args, name):
            return getattr(self._args, name)
        elif name in self._settings:
            return self._settings[name]  # type: ignore[literal-required]
        return getattr(super(), name)

    def __setattr__(self, name: str, value: Any, /) -> None:
        if name in self.PASSTHROUGH:
            # passthrough
            return super().__setattr__(name, value)
        elif name in self._settings:
            self._settings[name] = value  # type: ignore[literal-required]
            self._altered = True
            return
        raise TypeError(f"{name} is missing a custom setter")

    def __delattr__(self, name: str, /) -> None:
        raise RuntimeError("settings can't be deleted")

    def alter(self) -> None:
        self._altered = True

    def save(self, *, force: bool = False) -> None:
        if self._altered or force:
            json_save(SETTINGS_PATH, self._settings, sort=True)
