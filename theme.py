from __future__ import annotations

import sys
import math
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
        # Windows 11 Settings-app inspired: soft neutral window bg, brighter "card" surfaces
        # with more contrast than classic, and a violet accent used consistently.
        "light": {
            "bg": "#f3f3f3", "fg": "#1a1a1a", "sel_bg": "#e9ddff", "sel_fg": "#3d0a91",
            "link": "#772ce8", "surface": "#fbfbfb", "header": "#f3ecff", "fieldbg": "#ffffff",
            "border": "#e2e2e2", "muted": "#6e6e6e", "accent": "#8338ec",
        },
        "dark": {
            "bg": "#202020", "fg": "#f2f2f2", "sel_bg": "#3d2769", "sel_fg": "#e6d6ff",
            "link": "#c69bff", "surface": "#2b2b2b", "header": "#33244a", "fieldbg": "#2d2d2d",
            "border": "#3a3a3a", "muted": "#a3a3a3", "accent": "#b48cff",
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


def system_accent_color() -> str | None:
    """
    Best-effort read of the OS accent color as a "#rrggbb" hex string.
    Returns None if unavailable (unsupported platform, or read failure).
    """
    try:
        if sys.platform == "win32":
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _type = winreg.QueryValueEx(key, "AccentColorMenu")
                # value is a 32-bit ABGR DWORD
                b = (value >> 8) & 0xFF
                g = (value >> 16) & 0xFF
                r = (value >> 24) & 0xFF
                return f"#{r:02x}{g:02x}{b:02x}"
        elif sys.platform == "darwin":
            import subprocess
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleAccentColor"],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                return None
            # macOS accent color index -> approximate hex (no direct RGB exposed by defaults)
            mac_accents = {
                "-1": "#8e8e93", "0": "#e5484d", "1": "#ff8c00", "2": "#ffd60a",
                "3": "#34c759", "4": "#0a84ff", "5": "#5e5ce6", "6": "#af52de",
                "7": "#ff2d78",
            }
            return mac_accents.get(result.stdout.strip())
        else:
            import subprocess
            result = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "accent-color"],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                return None
            gnome_accents = {
                "blue": "#3584e4", "teal": "#2190a4", "green": "#3a944a",
                "yellow": "#c88800", "orange": "#ed5b00", "red": "#e62d42",
                "pink": "#d56199", "purple": "#9141ac", "slate": "#6f8396",
            }
            return gnome_accents.get(result.stdout.strip().strip("'"))
    except Exception:
        logger.debug("System accent color detection failed", exc_info=True)
        return None


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


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def build_tab_icons(color: str, size: int = 18):
    """
    Renders a small set of clean, flat, single-color glyph icons (home, dashboard,
    inventory, settings, help) for the tab bar, drawn as vector shapes via PIL so
    they stay crisp at any DPI, instead of relying on emoji/text glyphs.
    Returns {name: {"normal": PhotoImage, "selected": PhotoImage}}.
    """
    from PIL import Image, ImageDraw, ImageTk

    SS = 4  # supersampling factor for anti-aliasing
    S = size * SS

    def canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        return img, ImageDraw.Draw(img)

    def finish(img: Image.Image) -> Image.Image:
        return img.resize((size, size), Image.LANCZOS)

    def draw_home(fg):
        img, d = canvas()
        m = S * 0.12
        w = S - 2 * m
        apex = (S / 2, m)
        roof_l = (m, S * 0.48)
        roof_r = (S - m, S * 0.48)
        base_l = (S * 0.22, S - m)
        base_r = (S * 0.78, S - m)
        lw = max(2, int(S * 0.09))
        d.line([roof_l, apex, roof_r], fill=fg, width=lw, joint="curve")
        d.line([roof_l, (S * 0.22, S * 0.48)], fill=fg, width=lw)
        d.line([(S * 0.22, S * 0.48), base_l], fill=fg, width=lw)
        d.line([roof_r, (S * 0.78, S * 0.48)], fill=fg, width=lw)
        d.line([(S * 0.78, S * 0.48), base_r], fill=fg, width=lw)
        d.line([base_l, base_r], fill=fg, width=lw)
        return finish(img)

    def draw_dashboard(fg):
        img, d = canvas()
        bar_w = S * 0.18
        gap = S * 0.10
        base_y = S * 0.85
        heights = (0.35, 0.6, 0.45)
        x = S * 0.12
        for h in heights:
            top = base_y - S * h
            d.rounded_rectangle([x, top, x + bar_w, base_y], radius=bar_w * 0.25, fill=fg)
            x += bar_w + gap
        return finish(img)

    def draw_inventory(fg):
        img, d = canvas()
        m = S * 0.14
        lw = max(2, int(S * 0.09))
        d.rounded_rectangle([m, m * 1.6, S - m, S - m], radius=S * 0.08, outline=fg, width=lw)
        d.line([(m, S * 0.42), (S - m, S * 0.42)], fill=fg, width=lw)
        return finish(img)

    def draw_settings(fg):
        img, d = canvas()
        cx, cy = S / 2, S / 2
        r_outer = S * 0.42
        r_inner = S * 0.30
        tooth_w = S * 0.11
        lw = max(2, int(S * 0.085))
        n_teeth = 8
        for i in range(n_teeth):
            a = math.pi * 2 * i / n_teeth
            # blocky tooth: a small square rotated out to the rim
            tx = cx + (r_outer - tooth_w / 2) * math.cos(a)
            ty = cy + (r_outer - tooth_w / 2) * math.sin(a)
            d.ellipse(
                [tx - tooth_w / 2, ty - tooth_w / 2, tx + tooth_w / 2, ty + tooth_w / 2], fill=fg
            )
        d.ellipse([cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner], fill=fg)
        hole = S * 0.14
        d.ellipse([cx - hole, cy - hole, cx + hole, cy + hole], fill=(0, 0, 0, 0))
        return finish(img)

    def draw_help(fg):
        img, d = canvas()
        m = S * 0.10
        lw = max(2, int(S * 0.1))
        d.ellipse([m, m, S - m, S - m], outline=fg, width=lw)
        try:
            from PIL import ImageFont
            font = ImageFont.truetype("arial.ttf", int(S * 0.55))
        except Exception:
            font = None
        text = "?"
        if font is not None:
            bbox = d.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            d.text((S / 2 - tw / 2 - bbox[0], S / 2 - th / 2 - bbox[1]), text, fill=fg, font=font)
        else:
            # fallback: draw a simple dot + curve approximation without a font
            d.ellipse(
                [S / 2 - S * 0.05, S * 0.62, S / 2 + S * 0.05, S * 0.72], fill=fg
            )
            d.arc(
                [S * 0.32, S * 0.22, S * 0.68, S * 0.55], start=200, end=430, fill=fg, width=lw
            )
        return finish(img)

    def draw_details(fg):
        img, d = canvas()
        m = S * 0.16
        lw = max(2, int(S * 0.10))
        d.rounded_rectangle([m, m * 0.7, S - m, S - m * 0.7], radius=S * 0.06, outline=fg, width=lw)
        line_x0, line_x1 = m + S * 0.10, S - m - S * 0.10
        for i, ly in enumerate((0.38, 0.55, 0.72)):
            w = line_x1 - (S * 0.18 if i == 2 else 0)
            d.line([(line_x0, S * ly), (w, S * ly)], fill=fg, width=max(2, int(S * 0.055)))
        return finish(img)

    builders = {
        "main": draw_home,
        "details": draw_details,
        "dashboard": draw_dashboard,
        "inventory": draw_inventory,
        "settings": draw_settings,
        "help": draw_help,
    }
    fg = _hex_to_rgb(color) + (255,)
    return {key: ImageTk.PhotoImage(builder(fg)) for key, builder in builders.items()}
