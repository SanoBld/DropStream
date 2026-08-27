from __future__ import annotations

from collections import abc
from pathlib import Path
from typing import Any, TypedDict, TYPE_CHECKING

from exceptions import MinerException
from utils import json_load, json_save
from constants import IS_PACKAGED, LANG_PATH, DEFAULT_LANG

if TYPE_CHECKING:
    from typing_extensions import NotRequired


class StatusMessages(TypedDict):
    terminated: str
    watching: str
    goes_online: str
    goes_offline: str
    claimed_drop: str
    no_channel: str
    no_campaign: str
    auto_action: str


class ChromeMessages(TypedDict):
    startup: str
    login_to_complete: str
    no_token: str
    closed_window: str


class LoginMessages(TypedDict):
    chrome: ChromeMessages
    error_code: str
    unexpected_content: str
    email_code_required: str
    twofa_code_required: str
    incorrect_login_pass: str
    incorrect_email_code: str
    incorrect_twofa_code: str


class ErrorMessages(TypedDict):
    captcha: str
    no_connection: str
    site_down: str


class GUIStatus(TypedDict):
    name: str
    idle: str
    exiting: str
    terminated: str
    cleanup: str
    gathering: str
    switching: str
    fetching_inventory: str
    fetching_campaigns: str
    adding_campaigns: str
    paused: str
    paused_until: str
    scheduled: str


class GUITabs(TypedDict):
    main: str
    dashboard: str
    inventory: str
    settings: str
    help: str


class GUITray(TypedDict):
    notification_title: str
    minimize: str
    show: str
    quit: str
    pause: str
    resume: str
    switch_channel: str


class GUIDashboard(TypedDict):
    now_mining: str
    campaign: str
    drop_remaining: str
    campaign_remaining: str
    status: str
    pause: str
    resume: str
    resume_at: str
    resume_at_info: str
    pause_until: str
    weekly: str
    per_game: str
    total_drops: str
    hours_saved: str
    no_data: str


class GUILoginForm(TypedDict):
    name: str
    labels: str
    logging_in: str
    logged_in: str
    logged_out: str
    request: str
    required: str
    username: str
    password: str
    twofa_code: str
    button: str


class GUIWebsocket(TypedDict):
    name: str
    websocket: str
    initializing: str
    connected: str
    disconnected: str
    connecting: str
    disconnecting: str
    reconnecting: str


class GUIProgress(TypedDict):
    name: str
    drop: str
    game: str
    campaign: str
    remaining: str
    drop_progress: str
    campaign_progress: str


class GUIChannelHeadings(TypedDict):
    channel: str
    status: str
    game: str
    viewers: str


class GUIChannels(TypedDict):
    name: str
    switch: str
    online: str
    pending: str
    offline: str
    headings: GUIChannelHeadings


class GUIInvFilter(TypedDict):
    name: str
    show: str
    not_linked: str
    upcoming: str
    expired: str
    excluded: str
    finished: str
    refresh: str


class GUIInvStatus(TypedDict):
    linked: str
    not_linked: str
    active: str
    expired: str
    upcoming: str
    claimed: str
    ready_to_claim: str


class GUIInventory(TypedDict):
    filter: GUIInvFilter
    status: GUIInvStatus
    starts: str
    ends: str
    allowed_channels: str
    all_channels: str
    and_more: str
    percent_progress: str
    minutes_progress: str


class GUISettingsGeneral(TypedDict):
    name: str
    autostart: str
    tray: str
    tray_notifications: str
    theme: str
    use_system_accent: str
    priority_mode: str
    priority_mode_info: str
    proxy: str


class GUIThemes(TypedDict):
    auto: str
    light: str
    dark: str
    modern_auto: str
    modern_light: str
    modern_dark: str


class GUISettingsAccounts(TypedDict):
    name: str
    current: str
    create: str
    launch: str
    switch: str
    delete: str
    delete_confirm: str


class GUISettingsScheduler(TypedDict):
    name: str
    enabled: str
    start: str
    end: str
    auto_action: str


class GUISettingsRemote(TypedDict):
    name: str
    info: str
    enabled: str
    port: str
    new_link: str
    link: str
    link_disabled: str
    copy_link: str


class GUIPowerActions(TypedDict):
    none: str
    sleep: str
    shutdown: str


class GUISettingsAdvanced(TypedDict):
    name: str
    warning: str
    warning_text: str
    enable_badges_emotes: str
    available_drops_check: str


class GUIPriorityModes(TypedDict):
    priority_only: str
    priority_only_continue: str
    ending_soonest: str
    priority_ending_soonest: str
    low_availability: str
    priority_low_availability: str


class GUISettings(TypedDict):
    general: GUISettingsGeneral
    themes: GUIThemes
    accounts: GUISettingsAccounts
    scheduler: GUISettingsScheduler
    remote: GUISettingsRemote
    power_actions: GUIPowerActions
    advanced: GUISettingsAdvanced
    priority_modes: GUIPriorityModes
    game_name: str
    priority: str
    exclude: str
    reload: str
    reload_text: str


class GUIHelpLinks(TypedDict):
    name: str
    inventory: str
    campaigns: str


class GUIHelpInvalidate(TypedDict):
    button: str
    text: str


class GUIHelp(TypedDict):
    links: GUIHelpLinks
    how_it_works: str
    how_it_works_text: str
    getting_started: str
    getting_started_text: str
    invalidate: GUIHelpInvalidate


class GUIMessages(TypedDict):
    output: str
    status: GUIStatus
    tabs: GUITabs
    tray: GUITray
    login: GUILoginForm
    websocket: GUIWebsocket
    progress: GUIProgress
    channels: GUIChannels
    inventory: GUIInventory
    settings: GUISettings
    help: GUIHelp
    dashboard: GUIDashboard


class Translation(TypedDict):
    language_name: NotRequired[str]
    english_name: str
    status: StatusMessages
    login: LoginMessages
    error: ErrorMessages
    gui: GUIMessages


default_translation: Translation = {
    "english_name": "English",
    "status": {
        "terminated": "\nApplication Terminated.\nClose the window to exit the application.",
        "watching": "Watching: {channel}",
        "goes_online": "{channel} goes ONLINE, switching...",
        "goes_offline": "{channel} goes OFFLINE, switching...",
        "claimed_drop": "Claimed drop: {drop}",
        "no_channel": "No available channels to watch. Waiting for an ONLINE channel...",
        "no_campaign": "No active campaigns to mine drops for. Waiting for an active campaign...",
        "auto_action": "All drops claimed for now, running configured action: {action}",
    },
    "login": {
        "unexpected_content": (
            "Unexpected content type returned, usually due to being redirected. "
            "Do you need to login for internet access?"
        ),
        "chrome": {
            "startup": "Opening Chrome...",
            "login_to_complete": (
                "Complete the login procedure manually by pressing the Login button again."
            ),
            "no_token": "No authorization token could be found.",
            "closed_window": (
                "The Chrome window was closed before the login procedure could be completed."
            ),
        },
        "error_code": "Login error code: {error_code}",
        "incorrect_login_pass": "Incorrect username or password.",
        "incorrect_email_code": "Incorrect email code.",
        "incorrect_twofa_code": "Incorrect 2FA code.",
        "email_code_required": "Email code required. Check your email.",
        "twofa_code_required": "2FA token required.",
    },
    "error": {
        "captcha": "Your login attempt was denied by CAPTCHA.\nPlease try again in 12+ hours.",
        "site_down": "Twitch is down, retrying in {seconds} seconds...",
        "no_connection": "Cannot connect to Twitch, retrying in {seconds} seconds... ({url})",
    },
    "gui": {
        "output": "Output",
        "status": {
            "name": "Status",
            "idle": "Idle",
            "exiting": "Exiting...",
            "terminated": "Terminated",
            "cleanup": "Cleaning up channels...",
            "gathering": "Gathering channels...",
            "switching": "Switching the channel...",
            "fetching_inventory": "Fetching inventory...",
            "fetching_campaigns": "Fetching campaigns...",
            "adding_campaigns": "Adding campaigns to inventory... {counter}",
            "paused": "Paused",
            "paused_until": "Paused until {time}",
            "scheduled": "Outside scheduled hours",
        },
        "tabs": {
            "main": "Details",
            "dashboard": "Dashboard",
            "inventory": "Inventory",
            "settings": "Settings",
            "help": "Help",
        },
        "dashboard": {
            "now_mining": "Currently Mining",
            "campaign": "Drop Campaign",
            "drop_remaining": "Time left for this drop: ",
            "campaign_remaining": "Time left for the campaign: ",
            "status": "Status",
            "pause": "Pause",
            "resume": "Resume",
            "resume_at": "Resume at: ",
            "resume_at_info": (
                "Example: type 14:30 and click \"Pause until\" to pause mining now and "
                "automatically resume at 2:30 PM (today, or tomorrow if that time already "
                "passed today)."
            ),
            "pause_until": "Pause until",
            "weekly": "Last 7 days",
            "per_game": "Drops per game",
            "total_drops": "Total drops claimed: {count}",
            "hours_saved": "Watch hours saved: {hours}",
            "no_data": "No data yet",
        },
        "tray": {
            "notification_title": "Mined Drop",
            "minimize": "Minimize to Tray",
            "show": "Show",
            "quit": "Quit",
            "pause": "Pause",
            "resume": "Resume",
            "switch_channel": "Switch Channel",
        },
        "login": {
            "name": "Login Form",
            "labels": "Status:\nUser ID:",
            "logged_in": "Logged in",
            "logged_out": "Logged out",
            "logging_in": "Logging in...",
            "required": "Login required",
            "request": "Please log in to continue.",
            "username": "Username",
            "password": "Password",
            "twofa_code": "2FA code (optional)",
            "button": "Login",
        },
        "websocket": {
            "name": "Websocket Status",
            "websocket": "Websocket #{id}:",
            "initializing": "Initializing...",
            "connected": "Connected",
            "disconnected": "Disconnected",
            "connecting": "Connecting...",
            "disconnecting": "Disconnecting...",
            "reconnecting": "Reconnecting...",
        },
        "progress": {
            "name": "Campaign Progress",
            "drop": "Drop:",
            "game": "Game:",
            "campaign": "Campaign:",
            "remaining": "{time} remaining",
            "drop_progress": "Progress:",
            "campaign_progress": "Progress:",
        },
        "channels": {
            "name": "Channels",
            "switch": "Switch",
            "online": "ONLINE  ✔",
            "pending": "OFFLINE ⏳",
            "offline": "OFFLINE ❌",
            "headings": {
                "channel": "Channel",
                "status": "Status",
                "game": "Game",
                "viewers": "Viewers",
            },
        },
        "inventory": {
            "filter": {
                "name": "Filter",
                "show": "Show:",
                "not_linked": "Not linked",
                "upcoming": "Upcoming",
                "expired": "Expired",
                "excluded": "Excluded",
                "finished": "Finished",
                "refresh": "Refresh",
            },
            "status": {
                "linked": "Linked ✔",
                "not_linked": "Not Linked ❌",
                "active": "Active ✔",
                "upcoming": "Upcoming ⏳",
                "expired": "Expired ❌",
                "claimed": "Claimed ✔",
                "ready_to_claim": "Ready to claim ⏳",
            },
            "starts": "Starts: {time}",
            "ends": "Ends: {time}",
            "allowed_channels": "Allowed Channels:",
            "all_channels": "All",
            "and_more": "and {amount} more...",
            "percent_progress": "{percent} of {minutes} minutes",
            "minutes_progress": "{minutes} minutes",
        },
        "settings": {
            "general": {
                "name": "General",
                "autostart": "Autostart: ",
                "tray": "Autostart into tray: ",
                "tray_notifications": "Tray notifications: ",
                "theme": "Theme: ",
                "use_system_accent": "Use system accent color: ",
                "priority_mode": "Priority mode: ",
                "priority_mode_info": (
                    "Priority list games are always tried first, in the order you set them. "
                    "\"Ending soonest\"/\"Low availability first\" only decide the order for "
                    "everything else, once nothing from the priority list is available."
                ),
                "proxy": "Proxy (requires restart):",
            },
            "themes": {
                "auto": "Light/Dark (Auto)",
                "light": "Light",
                "dark": "Dark",
                "modern_auto": "Twitch Colors (Auto)",
                "modern_light": "Twitch Colors Light",
                "modern_dark": "Twitch Colors Dark",
            },
            "accounts": {
                "name": "Accounts",
                "current": "Active profile: {name}",
                "create": "Create",
                "launch": "Launch (parallel)",
                "switch": "Switch to",
                "delete": "Delete",
                "delete_confirm": "Permanently delete the profile \"{name}\" and all of its data (settings, cookies, cache, stats)? This can't be undone.",
            },
            "advanced": {
                "name": "Advanced",
                "warning": "Warning!",
                "warning_text": (
                    "These options will cause the miner to misbehave.\n"
                    "If you're experiencing any issues, "
                    "make sure all of these options are disabled."
                ),
                "enable_badges_emotes": "Enable partial support for badges and emotes: ",
                "available_drops_check": "Enable extra available drops check: ",
            },
            "priority_modes": {
                "priority_only": "Priority list only",
                "priority_only_continue": "Priority list only, then continue with the rest",
                "ending_soonest": "Ending soonest",
                "priority_ending_soonest": "Priority list first, then ending soonest",
                "low_availability": "Low availability first",
                "priority_low_availability": "Priority list first, then low availability",
            },
            "scheduler": {
                "name": "Scheduler",
                "enabled": "Enable run schedule: ",
                "start": "Start (HH:MM): ",
                "end": "End (HH:MM): ",
                "auto_action": "When all drops are done: ",
            },
            "remote": {
                "name": "Remote Access",
                "info": (
                    "Lets you view live status and control this instance (pause/resume, "
                    "priority mode) from a browser on another device, using the link below. "
                    "Anyone with the link has full access, so only share it with people you "
                    "trust."
                ),
                "enabled": "Enable web dashboard: ",
                "port": "Port: ",
                "new_link": "Generate a new link",
                "link": "Share link:",
                "link_disabled": "Enable the dashboard to generate a link.",
                "copy_link": "Copy link",
            },
            "power_actions": {
                "none": "Do nothing",
                "sleep": "Sleep PC",
                "shutdown": "Shutdown PC",
            },
            "game_name": "Game name",
            "priority": "Priority",
            "exclude": "Exclude",
            "reload": "Reload",
            "reload_text": "Most changes require a reload to take an immediate effect: ",
        },
        "help": {
            "links": {
                "name": "Useful Links",
                "inventory": "See Twitch inventory",
                "campaigns": "See all campaigns and manage account links",
            },
            "how_it_works": "How It Works",
            "how_it_works_text": (
                "Every several seconds, the application pretends to watch a particular stream "
                "by fetching stream metadata - this is enough to advance the drops. "
                "Note that this completely bypasses the need to download "
                "any actual stream of video and sound. "
                "To keep the status (ONLINE or OFFLINE) of the channels up-to-date, "
                "there's a websocket connection established that receives events about streams "
                "going up or down, or updates regarding the current number of viewers."
            ),
            "getting_started": "Getting Started",
            "getting_started_text": (
                "1. Login to the application.\n"
                "2. Ensure your Twitch account is linked to all campaigns "
                "you're interested in mining.\n"
                "3. If you're interested in mining everything possible, "
                "change the Priority Mode to anything other than \"Priority list only\" "
                "and press on \"Reload\".\n"
                "4. If you want to mine specific games first, use the \"Priority\" list "
                "to set up an ordered list of games of your choice. "
                "Games from the top of the list will be attempted to be mined first, "
                "before the ones lower down the list.\n"
                "5. Keep the \"Priority mode\" selected as \"Priority list only\", "
                "to avoid mining games that are not on the priority list. "
                "Or not - it's up to you.\n"
                "6. Use the \"Exclude\" list to tell the application "
                "which games should never be mined.\n"
                "7. Changing the contents of either of the lists, or changing "
                "the \"Priority mode\", requires you to press on \"Reload\" "
                "for the changes to take an effect."
            ),
            "invalidate": {
                "button": "Invalidate",
                "text": "Invalidate the authentication token (log out):",
            },
        },
    },
}


class Translator:
    def __init__(self) -> None:
        self._langs: list[str] = []
        # start with (and always copy) the default translation
        self._translation: Translation = default_translation.copy()
        # if we're in dev, update the template English.json file
        if not IS_PACKAGED:
            default_langpath = LANG_PATH.joinpath(f"{DEFAULT_LANG}.json")
            json_save(default_langpath, default_translation)
        self._translation["language_name"] = DEFAULT_LANG
        self._cleanup_legacy_filenames()
        # load available translation names
        seen_stems: set[str] = set()
        for filepath in sorted(LANG_PATH.glob("*.json")):
            if filepath.stem in seen_stems:
                continue  # skip duplicate/leftover files pointing at the same language
            seen_stems.add(filepath.stem)
            self._langs.append(filepath.stem)
        self._langs.sort()
        if DEFAULT_LANG in self._langs:
            self._langs.remove(DEFAULT_LANG)
        self._langs.insert(0, DEFAULT_LANG)

    # Known-good filename (without .json) for every shipped language, keyed by the
    # "english_name" stored inside each file. Used to robustly detect and repair/dedupe
    # mis-decoded filenames (mojibake, "#Uxxxx" escapes, etc.) regardless of the exact
    # corruption pattern, since content encoding is unaffected, only filenames can break.
    _CANONICAL_LANG_NAMES: dict[str, str] = {
        "English": "English", "Czech": "Čeština", "Russian": "Русский",
        "Ukrainian": "Українська", "Arabic": "العربية", "Japanese": "日本語",
        "Simplified Chinese": "简体中文", "Traditional Chinese": "繁體中文",
        "Danish": "Dansk", "German": "Deutsch", "Spanish": "Español",
        "French": "Français", "Indonesian": "Indonesian", "Italian": "Italiano",
        "Hungarian": "Magyar", "Dutch": "Nederlandse", "Norwegian": "Norsk",
        "Polish": "Polski", "Portuguese": "Português", "Romanian": "Română",
        "Turkish": "Türkçe",
    }

    def _cleanup_legacy_filenames(self) -> None:
        """
        Older releases could end up with mis-decoded language filenames (accented/CJK/
        Cyrillic/Arabic characters replaced by "#Uxxxx" escapes or turned into mojibake,
        due to a packaging bug). Filename content itself (the JSON) is unaffected, so this
        reads each file's "english_name" and renames/dedupes it against a known-good
        canonical filename, fixing any corruption pattern rather than just one specific case.
        """
        import json as _json
        try:
            files = list(LANG_PATH.glob("*.json"))
        except OSError:
            return
        existing_stems = {f.stem for f in files}
        for f in files:
            try:
                with f.open(encoding="utf-8") as fh:
                    english_name = _json.load(fh).get("english_name")
            except (OSError, ValueError):
                continue
            canonical = self._CANONICAL_LANG_NAMES.get(english_name)
            if canonical is None or f.stem == canonical:
                continue
            try:
                if canonical in existing_stems:
                    # the correctly-named file already exists: this one is a stale duplicate
                    f.unlink()
                else:
                    f.rename(f.with_name(f"{canonical}.json"))
                    existing_stems.add(canonical)
                existing_stems.discard(f.stem)
            except OSError:
                pass

    @property
    def languages(self) -> abc.Iterable[str]:
        return iter(self._langs)

    @property
    def current(self) -> str:
        return self._translation.get("language_name", DEFAULT_LANG)

    def set_language(self, language: str):
        if language not in self._langs:
            raise ValueError("Unrecognized language")
        elif self._translation.get("language_name", DEFAULT_LANG) == language:
            # same language as loaded selected
            return
        elif language == DEFAULT_LANG:
            # default language selected - use the memory value
            self._translation = default_translation.copy()
        else:
            self._translation = json_load(
                LANG_PATH.joinpath(f"{language}.json"), default_translation
            )
            if "language_name" in self._translation:
                raise ValueError("Translations cannot define 'language_name'")
        self._translation["language_name"] = language

    def __call__(self, *path: str) -> str:
        if not path:
            raise ValueError("Language path expected")
        v: Any = self._translation
        try:
            for key in path:
                v = v[key]
        except KeyError:
            # this can only really happen for the default translation
            raise MinerException(
                f"{self.current} translation is missing the '{' -> '.join(path)}' translation key"
            )
        return v


_ = Translator()
