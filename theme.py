from __future__ import annotations

import sys
import logging
from typing import TypedDict

logger = logging.getLogger("TwitchDrops")


class Palette(TypedDict):
    bg: str
    fg: str
    sel_bg: str
    sel_fg: str
    link: str
    surface: str
    header: str
    fieldbg: str
    border: str
    muted: str
    accent: str


# "classic": the original palette, mostly unchanged.
# "modern": flatter, higher-contrast, Twitch-purple accent, closer to native Windows 11 styling.
PALETTES: dict[str, dict[str, Palette]] = {
    "classic": {
        "light": {
            "bg": "#f0f0f0", "fg": "#000000", "sel_bg": "#cce5ff", "sel_fg": "#000000",
            "link": "blue", "surface": "#ffffff", "header": "#eeeeee", "fieldbg": "#ffffff",
            "border": "#cccccc", "muted": "#404040", "accent": "#0a84ff",
        },
        "dark": {
            "bg": "#1e1e1e", "fg": "#e6e6e6", "sel_bg": "#094771", "sel_fg": "#ffffff",
            "link": "#4ea3ff", "surface": "#252525", "header": "#2a2a2a", "fieldbg": "#2b2b2b",
            "border": "#3c3c3c", "muted": "#b3b3b3", "accent": "#0d99ff",
        },
    },
    "modern": {
        "light": {
            "bg": "#f3f3f3", "fg": "#1a1a1a", "sel_bg": "#e4d6ff", "sel_fg": "#1a1a1a",
            "link": "#772ce8", "surface": "#ffffff", "header": "#ececec", "fieldbg": "#ffffff",
            "border": "#d9d9d9", "muted": "#6e6e6e", "accent": "#9146ff",
        },
        "dark": {
            "bg": "#181818", "fg": "#f2f2f2", "sel_bg": "#3a1e70", "sel_fg": "#ffffff",
            "link": "#bf94ff", "surface": "#202020", "header": "#262626", "fieldbg": "#242424",
            "border": "#333333", "muted": "#9e9e9e", "accent": "#a970ff",
        },
    },
}


def system_prefers_dark() -> bool:
    """Best-effort detection of the OS-wide light/dark preference. Defaults to light."""
    try:
        if sys.platform == "win32":
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _type = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return value == 0
        elif sys.platform == "darwin":
            import subprocess
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True,
            )
            return result.returncode == 0 and "dark" in result.stdout.lower()
        else:
            import subprocess
            result = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                capture_output=True, text=True,
            )
            return "dark" in result.stdout.lower()
    except Exception:
        logger.debug("System theme detection failed, defaulting to light", exc_info=True)
        return False


def resolve_theme(theme: str) -> tuple[bool, str]:
    """
    Turns a settings.theme value ("light"/"dark"/"auto"/"modern_light"/"modern_dark"/"modern_auto")
    into (is_dark, style), where style is "classic" or "modern".
    """
    style = "modern" if theme.startswith("modern") else "classic"
    mode = theme.split("_", 1)[1] if theme.startswith("modern_") else theme
    if mode == "auto":
        return system_prefers_dark(), style
    return mode == "dark", style
