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

    PASSTHROUGH = ("_settings", "_args", "_altered")

    def __init__(self, args: ParsedArgs):
        settings_existed = SETTINGS_PATH.exists()
        self._settings: SettingsFile = json_load(SETTINGS_PATH, default_settings)
        self._args: ParsedArgs = args
        self._altered: bool = False
        # one-time migration: pre-existing settings files only had a boolean dark_mode toggle;
        # new installs keep the "auto" default instead
        if settings_existed and self._settings.get("_migrated_theme") is not True:
            self._settings["theme"] = "dark" if self._settings["dark_mode"] else "light"
            self._settings["_migrated_theme"] = True  # type: ignore[typeddict-unknown-key]
            self._altered = True

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
