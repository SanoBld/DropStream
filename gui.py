from __future__ import annotations

import os
import re
import gc
import sys
import shlex
import ctypes
import asyncio
import logging
import plistlib
import webbrowser
import tkinter as tk
from pathlib import Path
from collections import abc
from textwrap import dedent
from math import log10, ceil
from dataclasses import dataclass
from tkinter.font import Font, nametofont
import tkinter.font as tkfont
from functools import partial, cached_property
from datetime import datetime, timedelta, timezone
from tkinter import Tk, ttk, StringVar, DoubleVar, IntVar, messagebox
from typing import Any, Union, Tuple, TypedDict, Generic, TYPE_CHECKING, Literal, cast

# Local aliases for tkinter's private (underscore-prefixed) stub-only type aliases.
# tkinter exposes these only for its own internal typing use, so referencing them
# directly as `_ScreenUnits` etc. triggers static-analysis attribute warnings.
# Defining them locally keeps the same typing behavior without touching tkinter internals.
_ScreenUnits = Union[str, float]
_Relief = Literal["raised", "sunken", "flat", "ridge", "solid", "groove"]
_Anchor = Literal["nw", "n", "ne", "w", "center", "e", "sw", "s", "se"]

import pystray
from yarl import URL
from PIL.ImageTk import PhotoImage
from PIL import Image as Image_module

if sys.platform == "win32":
    import win32api
    import win32con
    import win32gui

if sys.platform == "darwin":
    import AppKit

from translate import _
from cache import ImageCache
from exceptions import MinerException, ExitRequest
from utils import resource_path, set_root_icon, webopen, task_wrapper, Game, _T
from constants import (
    MAX_INT,
    SELF_PATH,
    IS_PACKAGED,
    SCRIPTS_PATH,
    WINDOW_TITLE,
    ACTIVE_PROFILE,
    LOGGING_LEVELS,
    MAX_WEBSOCKETS,
    WS_TOPICS_LIMIT,
    OUTPUT_FORMATTER,
    State,
    PriorityMode,
)
from scheduler import PowerAction, parse_hhmm
import profiles as profiles_module
from theme import PALETTES, resolve_theme, build_tab_icons
from version import __version__
if sys.platform == "win32":
    from registry import RegistryKey, ValueType, ValueNotFound


if TYPE_CHECKING:
    from twitch import Twitch
    from channel import Channel
    from settings import Settings
    from inventory import DropsCampaign, TimedDrop


logger = logging.getLogger("TwitchDrops")
TK_PADDING = Union[int, Tuple[int, int], Tuple[int, int, int], Tuple[int, int, int, int]]
DIGITS = ceil(log10(WS_TOPICS_LIMIT))


######################
# GUI ELEMENTS START #
######################


class _TKOutputHandler(logging.Handler):
    def __init__(self, output: GUIManager):
        super().__init__()
        self._output = output

    def emit(self, record):
        self._output.print(self.format(record))


class PlaceholderEntry(ttk.Entry):
    def __init__(
        self,
        master: ttk.Widget,
        *args: Any,
        placeholder: str,
        prefill: str = '',
        placeholdercolor: str = "grey60",
        **kwargs: Any,
    ):
        super().__init__(master, *args, **kwargs)
        self._prefill: str = prefill
        self._show: str = kwargs.get("show", '')
        self._text_color: str = kwargs.get("foreground", '')
        self._ph_color: str = placeholdercolor
        self._ph_text: str = placeholder
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)
        if isinstance(self, ttk.Combobox):
            # only bind this for comboboxes
            self.bind("<<ComboboxSelected>>", self._combobox_select)
        self._ph: bool = False
        self._insert_placeholder()

    def _insert_placeholder(self) -> None:
        """
        If we're empty, insert a placeholder, set placeholder text color and make sure it's shown.
        If we're not empty, leave the box as is.
        """
        if not super().get():
            self._ph = True
            super().config(foreground=self._ph_color, show='')
            super().insert("end", self._ph_text)

    def _remove_placeholder(self) -> None:
        """
        If we've had a placeholder, clear the box and set normal text colour and show.
        """
        if self._ph:
            self._ph = False
            super().delete(0, "end")
            super().config(foreground=self._text_color, show=self._show)
            if self._prefill:
                super().insert("end", self._prefill)

    def _focus_in(self, event: tk.Event[PlaceholderEntry]) -> None:
        self._remove_placeholder()

    def _focus_out(self, event: tk.Event[PlaceholderEntry]) -> None:
        self._insert_placeholder()

    def _combobox_select(self, event: tk.Event[PlaceholderEntry]):
        # combobox clears and inserts the selected value internally, bypassing the insert method.
        # disable the placeholder flag and set the color here, so _focus_in doesn't clear the entry
        self._ph = False
        super().config(foreground=self._text_color, show=self._show)

    def _store_option(
        self, options: dict[str, object], name: str, attr: str, *, remove: bool = False
    ) -> None:
        if name in options:
            if remove:
                value = options.pop(name)
            else:
                value = options[name]
            setattr(self, attr, value)

    def configure(self, *args: Any, **kwargs: Any) -> Any:
        options: dict[str, Any] = {}
        if args and args[0] is not None:
            options.update(args[0])
        if kwargs:
            options.update(kwargs)
        self._store_option(options, "show", "_show")
        self._store_option(options, "foreground", "_text_color")
        self._store_option(options, "placeholder", "_ph_text", remove=True)
        self._store_option(options, "prefill", "_prefill", remove=True)
        self._store_option(options, "placeholdercolor", "_ph_color", remove=True)
        return super().configure(**kwargs)

    def config(self, *args: Any, **kwargs: Any) -> Any:
        # because 'config = configure' makes mypy complain
        self.configure(*args, **kwargs)

    def get(self) -> str:
        if self._ph:
            return ''
        return super().get()

    def insert(self, index: str | int, string: str) -> None:
        # when inserting into the entry externally, disable the placeholder flag
        if not string:
            # if an empty string was passed in
            return
        self._remove_placeholder()
        super().insert(index, string)

    def delete(self, first: str | int, last: str | int | None = None) -> None:
        super().delete(first, last)
        self._insert_placeholder()

    def clear(self) -> None:
        self.delete(0, "end")

    def replace(self, content: str) -> None:
        super().delete(0, "end")
        self.insert("end", content)


class PlaceholderCombobox(PlaceholderEntry, ttk.Combobox):
    pass


class PaddedListbox(tk.Listbox):
    def __init__(self, master: ttk.Widget, *args, padding: TK_PADDING = (0, 0, 0, 0), **kwargs):
        # we place the listbox inside a frame with the same background
        # this means we need to forward the 'grid' method to the frame, not the listbox
        self._frame = tk.Frame(master)
        self._frame.rowconfigure(0, weight=1)
        self._frame.columnconfigure(0, weight=1)
        super().__init__(self._frame)
        # mimic default listbox style with sunken relief and borderwidth of 1
        if "relief" not in kwargs:
            kwargs["relief"] = "sunken"
        if "borderwidth" not in kwargs:
            kwargs["borderwidth"] = 1
        self.configure(*args, padding=padding, **kwargs)

    def grid(self, *args, **kwargs):
        return self._frame.grid(*args, **kwargs)

    def grid_remove(self) -> None:
        return self._frame.grid_remove()

    def grid_info(self) -> tk._GridInfo:
        return self._frame.grid_info()

    def grid_forget(self) -> None:
        return self._frame.grid_forget()

    def configure(self, *args: Any, **kwargs: Any) -> Any:
        options: dict[str, Any] = {}
        if args and args[0] is not None:
            options.update(args[0])
        if kwargs:
            options.update(kwargs)
        # NOTE on processed options:
        # • relief is applied to the frame only
        # • background is copied, so that both listbox and frame change color
        # • borderwidth is applied to the frame only
        # bg is folded into background for easier processing
        if "bg" in options:
            options["background"] = options.pop("bg")
        frame_options = {}
        if "relief" in options:
            frame_options["relief"] = options.pop("relief")
        if "background" in options:
            frame_options["background"] = options["background"]  # copy
        if "borderwidth" in options:
            frame_options["borderwidth"] = options.pop("borderwidth")
        self._frame.configure(frame_options)
        # update padding
        if "padding" in options:
            padding: TK_PADDING = options.pop("padding")
            padx1: _ScreenUnits
            padx2: _ScreenUnits
            pady1: _ScreenUnits
            pady2: _ScreenUnits
            if not isinstance(padding, tuple) or len(padding) == 1:
                if isinstance(padding, tuple):
                    padding = padding[0]
                # pyright can't verify tuple length via len() at runtime, so the
                # remaining Union members (2/3/4-tuples) are still considered possible
                padx1 = padx2 = pady1 = pady2 = cast(int, padding)
            elif len(padding) == 2:
                # each padding[i] is an int here, but pyright can't narrow the element
                # type across the TK_PADDING Union-of-tuples, so it's cast explicitly
                padx1 = padx2 = cast(int, padding[0])
                pady1 = pady2 = cast(int, padding[1])
            elif len(padding) == 3:
                padx1, padx2 = cast(int, padding[0]), cast(int, padding[1])
                pady1 = pady2 = cast(int, padding[2])
            else:
                padx1, padx2, pady1, pady2 = padding
            super().grid(column=0, row=0, padx=(padx1, padx2), pady=(pady1, pady2), sticky="nsew")
        else:
            super().grid(column=0, row=0, sticky="nsew")
        # listbox uses flat relief to blend in with the inside of the frame
        options["relief"] = "flat"
        return super().configure(options)

    def config(self, *args: Any, **kwargs: Any) -> Any:
        # because 'config = configure' makes mypy complain
        self.configure(*args, **kwargs)

    def configure_theme(self, *, bg: str, fg: str, sel_bg: str, sel_fg: str):
        # Apply basic colors for dark/light mode
        super().config(bg=bg, fg=fg, selectbackground=sel_bg, selectforeground=sel_fg)


class MouseOverLabel(ttk.Label):
    def __init__(self, *args, alt_text: str = '', reverse: bool = False, **kwargs) -> None:
        self._org_text: str = ''
        self._alt_text: str = ''
        self._alt_reverse: bool = reverse
        self._bind_enter: str | None = None
        self._bind_leave: str | None = None
        super().__init__(*args, **kwargs)
        self.configure(text=kwargs.get("text", ''), alt_text=alt_text, reverse=reverse)

    def _set_org(self, event: tk.Event[MouseOverLabel]):
        super().config(text=self._org_text)

    def _set_alt(self, event: tk.Event[MouseOverLabel]):
        super().config(text=self._alt_text)

    def configure(self, *args: Any, **kwargs: Any) -> Any:
        options: dict[str, Any] = {}
        if args and args[0] is not None:
            options.update(args[0])
        if kwargs:
            options.update(kwargs)
        applicable_options: set[str] = set((
            "text",
            "reverse",
            "alt_text",
        ))
        if applicable_options.intersection(options.keys()):
            # we need to pop some options, because they can't be passed down to the label,
            # as that will result in an error later down the line
            events_change: bool = False
            if "text" in options:
                if bool(self._org_text) != bool(options["text"]):
                    events_change = True
                self._org_text = options["text"]
            if "alt_text" in options:
                if bool(self._alt_text) != bool(options["alt_text"]):
                    events_change = True
                self._alt_text = options.pop("alt_text")
            if "reverse" in options:
                if bool(self._alt_reverse) != bool(options["reverse"]):
                    events_change = True
                self._alt_reverse = options.pop("reverse")
            if self._org_text and not self._alt_text:
                options["text"] = self._org_text
            elif (not self._org_text or self._alt_reverse) and self._alt_text:
                options["text"] = self._alt_text
            if events_change:
                if self._bind_enter is not None:
                    self.unbind(self._bind_enter)
                    self._bind_enter = None
                if self._bind_leave is not None:
                    self.unbind(self._bind_leave)
                    self._bind_leave = None
                if self._org_text and self._alt_text:
                    if self._alt_reverse:
                        self._bind_enter = self.bind("<Enter>", self._set_org)
                        self._bind_leave = self.bind("<Leave>", self._set_alt)
                    else:
                        self._bind_enter = self.bind("<Enter>", self._set_alt)
                        self._bind_leave = self.bind("<Leave>", self._set_org)
        return super().configure(options)

    def config(self, *args: Any, **kwargs: Any) -> Any:
        # because 'config = configure' makes mypy complain
        self.configure(*args, **kwargs)


class InfoTooltip(ttk.Label):
    """A small 'ⓘ' hint icon that shows an explanatory popup on hover."""

    def __init__(self, master: tk.Misc, text: str, **kwargs) -> None:
        super().__init__(master, text="ⓘ", cursor="question_arrow", **kwargs)
        self._text = text
        self._popup: tk.Toplevel | None = None
        self.bind("<Enter>", self._show)
        self.bind("<Leave>", self._hide)

    def _show(self, event: tk.Event[tk.Misc]) -> None:
        if self._popup is not None:
            return
        x = self.winfo_rootx() + 16
        y = self.winfo_rooty() + self.winfo_height()
        self._popup = popup = tk.Toplevel(self)
        popup.wm_overrideredirect(True)
        popup.wm_geometry(f"+{x}+{y}")
        label = ttk.Label(
            popup, text=self._text, justify="left", wraplength=320,
            padding=(6, 4), relief="solid", borderwidth=1,
        )
        label.pack()

    def _hide(self, event: tk.Event[tk.Misc]) -> None:
        if self._popup is not None:
            self._popup.destroy()
            self._popup = None


class LinkLabel(ttk.Label):
    def __init__(self, *args, link: str, **kwargs) -> None:
        self._link: str = link
        # style provides font and foreground color
        if "style" not in kwargs:
            kwargs["style"] = "Link.TLabel"
        elif not kwargs["style"]:
            super().__init__(*args, **kwargs)
            return
        if "cursor" not in kwargs:
            kwargs["cursor"] = "hand2"
        if "padding" not in kwargs:
            # W, N, E, S
            kwargs["padding"] = (0, 2, 0, 2)
        super().__init__(*args, **kwargs)
        self.bind("<ButtonRelease-1>", lambda e: webopen(self._link))


class SelectMenu(tk.Menubutton, Generic[_T]):
    def __init__(
        self,
        master: tk.Misc,
        *args: Any,
        tearoff: bool = False,
        options: dict[str, _T],
        command: abc.Callable[[_T], Any] | None = None,
        default: str | None = None,
        relief: _Relief = "solid",
        background: str = "white",
        **kwargs: Any,
    ):
        width = max((len(k) for k in options.keys()), default=20)
        super().__init__(
            master, *args, background=background, relief=relief, width=width, **kwargs
        )
        self._menu_options: dict[str, _T] = options
        self._command = command
        self.menu = tk.Menu(self, tearoff=tearoff)
        self.config(menu=self.menu)
        for name in options.keys():
            self.menu.add_command(label=name, command=partial(self._select, name))
        if default is not None and default in self._menu_options:
            self.config(text=default)

    def _select(self, option: str) -> None:
        self.config(text=option)
        if self._command is not None:
            self._command(self._menu_options[option])

    def get(self) -> _T:
        return self._menu_options[self.cget("text")]


class SelectCombobox(ttk.Combobox):
    def __init__(
        self,
        master: tk.Misc,
        *args,
        width_offset: int = 0,
        width: int | None = None,
        textvariable: tk.StringVar,
        values: list[str] | tuple[str, ...],
        command: abc.Callable[[tk.Event[SelectCombobox]], None] | None = None,
        **kwargs,
    ) -> None:
        if width is None:
            font = Font(master, ttk.Style().lookup("TCombobox", "font"))
            # font.measure returns width in pixels, using '0' as the average character,
            # which is 6 pixels wide. We can convert it to width in characters by dividing.
            width = max(font.measure(v) // 6 + 1 for v in values)
        width += width_offset
        super().__init__(
            master,
            *args,
            width=width,
            values=values,
            state="readonly",
            exportselection=False,
            textvariable=textvariable,
            **kwargs,
        )
        if command is not None:
            self.bind("<<ComboboxSelected>>", command)


###########################################
# GUI ELEMENTS END / GUI DEFINITION START #
###########################################


class StatusBar:
    def __init__(self, manager: GUIManager, master: ttk.Widget):
        frame = ttk.LabelFrame(master, text=_("gui", "status", "name"), padding=(4, 0, 4, 4))
        frame.grid(column=0, row=0, columnspan=3, sticky="nsew", padx=2)
        frame.columnconfigure(0, weight=1)
        self.text_var = StringVar(frame, '')
        self._label = ttk.Label(frame, textvariable=self.text_var)
        self._label.grid(column=0, row=0, sticky="nsew")
        # real reload button for this tab (the desktop Inventory tab has its own,
        # but it can be hidden via settings.show_inventory_tab - this one is always here)
        ttk.Button(
            frame, text=_("gui", "dashboard", "reload"),
            command=lambda: manager._twitch.force_reload(),
        ).grid(column=1, row=0, sticky="e", padx=(6, 0))
        self._server_status_var = StringVar(frame, _("gui", "inventory", "server", "unknown"))
        ttk.Label(
            frame, textvariable=self._server_status_var
        ).grid(column=2, row=0, sticky="e", padx=(10, 4))
        ttk.Button(
            frame, text=_("gui", "inventory", "server", "check"),
            command=lambda: asyncio.create_task(self._check_server(manager)),
        ).grid(column=3, row=0, sticky="e")

    async def _check_server(self, manager: GUIManager) -> None:
        self._server_status_var.set(_("gui", "inventory", "server", "checking"))
        ok, latency_ms = await manager._twitch.check_server_status()
        key = "ok" if ok else "down"
        self._server_status_var.set(_("gui", "inventory", "server", key).format(ms=latency_ms))

    def update(self, text: str):
        self.text_var.set(text)

    def clear(self):
        self.text_var.set('')


class _WSEntry(TypedDict):
    status: str
    topics: int


class WebsocketStatus:
    def __init__(self, manager: GUIManager, master: ttk.Widget):
        frame = ttk.LabelFrame(master, text=_("gui", "websocket", "name"), padding=(4, 0, 4, 4))
        frame.grid(column=0, row=1, sticky="nsew", padx=2)
        self._status_var = StringVar(frame)
        self._topics_var = StringVar(frame)
        ttk.Label(
            frame,
            text='\n'.join(
                _("gui", "websocket", "websocket").format(id=i)
                for i in range(1, MAX_WEBSOCKETS + 1)
            ),
            style="MS.TLabel",
        ).grid(column=0, row=0)
        ttk.Label(
            frame,
            textvariable=self._status_var,
            width=16,
            justify="left",
            style="MS.TLabel",
        ).grid(column=1, row=0)
        ttk.Label(
            frame,
            textvariable=self._topics_var,
            width=(DIGITS * 2 + 1),
            justify="right",
            style="MS.TLabel",
        ).grid(column=2, row=0)
        self._items: dict[int, _WSEntry | None] = {i: None for i in range(MAX_WEBSOCKETS)}
        self._update()

    def update(self, idx: int, status: str | None = None, topics: int | None = None):
        if status is None and topics is None:
            raise TypeError("You need to provide at least one of: status, topics")
        entry = self._items.get(idx)
        if entry is None:
            entry = self._items[idx] = _WSEntry(
                status=_("gui", "websocket", "disconnected"), topics=0
            )
        if status is not None:
            entry["status"] = status
        if topics is not None:
            entry["topics"] = topics
        self._update()

    def remove(self, idx: int):
        if idx in self._items:
            del self._items[idx]
            self._update()

    def _update(self):
        status_lines: list[str] = []
        topic_lines: list[str] = []
        for idx in range(MAX_WEBSOCKETS):
            if (item := self._items.get(idx)) is None:
                status_lines.append('')
                topic_lines.append('')
            else:
                status_lines.append(item["status"])
                topic_lines.append(f"{item['topics']:>{DIGITS}}/{WS_TOPICS_LIMIT}")
        self._status_var.set('\n'.join(status_lines))
        self._topics_var.set('\n'.join(topic_lines))


@dataclass
class LoginData:
    username: str
    password: str
    token: str


class LoginForm:
    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._manager = manager
        self._var = StringVar(master)
        frame = ttk.LabelFrame(master, text=_("gui", "login", "name"), padding=(4, 0, 4, 4))
        frame.grid(column=1, row=1, sticky="nsew", padx=2)
        frame.columnconfigure(0, weight=2)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(4, weight=1)
        ttk.Label(frame, text=_("gui", "login", "labels")).grid(column=0, row=0)
        ttk.Label(frame, textvariable=self._var, justify="center").grid(column=1, row=0)
        self._login_entry = PlaceholderEntry(frame, placeholder=_("gui", "login", "username"))
        # self._login_entry.grid(column=0, row=1, columnspan=2)
        self._pass_entry = PlaceholderEntry(
            frame, placeholder=_("gui", "login", "password"), show='•'
        )
        # self._pass_entry.grid(column=0, row=2, columnspan=2)
        self._token_entry = PlaceholderEntry(frame, placeholder=_("gui", "login", "twofa_code"))
        # self._token_entry.grid(column=0, row=3, columnspan=2)

        self._confirm = asyncio.Event()
        self._button = ttk.Button(
            frame, text=_("gui", "login", "button"), command=self._confirm.set, state="disabled"
        )
        self._button.grid(column=0, row=4, columnspan=2)
        self.update(_("gui", "login", "logged_out"), None)

    def clear(self, login: bool = False, password: bool = False, token: bool = False):
        clear_all = not login and not password and not token
        if login or clear_all:
            self._login_entry.clear()
        if password or clear_all:
            self._pass_entry.clear()
        if token or clear_all:
            self._token_entry.clear()

    async def wait_for_login_press(self) -> None:
        self._confirm.clear()
        try:
            self._button.config(state="normal")
            await self._manager.coro_unless_closed(self._confirm.wait())
        finally:
            self._button.config(state="disabled")

    async def ask_login(self) -> LoginData:
        self.update(_("gui", "login", "required"), None)
        # ensure the window isn't hidden into tray when this runs
        self._manager.grab_attention(sound=False)
        while True:
            self._manager.print(_("gui", "login", "request"))
            await self.wait_for_login_press()
            login_data = LoginData(
                self._login_entry.get().strip(),
                self._pass_entry.get(),
                self._token_entry.get().strip(),
            )
            # basic input data validation: 3-25 characters in length, only ascii and underscores
            if (
                not 3 <= len(login_data.username) <= 25
                and re.match(r'^[a-zA-Z0-9_]+$', login_data.username)
            ):
                self.clear(login=True)
                continue
            if len(login_data.password) < 8:
                self.clear(password=True)
                continue
            if login_data.token and len(login_data.token) < 6:
                self.clear(token=True)
                continue
            return login_data

    async def ask_enter_code(self, page_url: URL, user_code: str) -> None:
        self.update(_("gui", "login", "required"), None)
        # ensure the window isn't hidden into tray when this runs
        self._manager.grab_attention(sound=False)
        self._manager.print(_("gui", "login", "request"))
        await self.wait_for_login_press()
        self._manager.print(f"Enter this code on the Twitch's device activation page: {user_code}")
        await asyncio.sleep(4)
        webopen(page_url)

    def update(self, status: str, user_id: int | None):
        if user_id is not None:
            user_str = str(user_id)
        else:
            user_str = "-"
        self._var.set(f"{status}\n{user_str}")


class _BaseVars(TypedDict):
    progress: DoubleVar
    percentage: StringVar
    remaining: StringVar


class _CampaignVars(_BaseVars):
    name: StringVar
    game: StringVar


class _DropVars(_BaseVars):
    rewards: StringVar


class _ProgressVars(TypedDict):
    campaign: _CampaignVars
    drop: _DropVars


class CampaignProgress:
    BAR_LENGTH = 420
    ALMOST_DONE_SECONDS = 10

    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._manager = manager
        self._vars: _ProgressVars = {
            "campaign": {
                "name": StringVar(master),  # campaign name
                "game": StringVar(master),  # game name
                "progress": DoubleVar(master),  # controls the progress bar
                "percentage": StringVar(master),  # percentage display string
                "remaining": StringVar(master),  # time remaining string, filled via _update_time
            },
            "drop": {
                "rewards": StringVar(master),  # drop rewards
                "progress": DoubleVar(master),  # as above
                "percentage": StringVar(master),  # as above
                "remaining": StringVar(master),  # as above
            },
        }
        self._frame = frame = ttk.LabelFrame(
            master, text=_("gui", "progress", "name"), padding=(4, 0, 4, 4)
        )
        frame.grid(column=0, row=2, columnspan=2, sticky="nsew", padx=2)
        frame.columnconfigure(0, weight=2)
        frame.columnconfigure(1, weight=1)
        game_campaign = ttk.Frame(frame)
        game_campaign.grid(column=0, row=0, columnspan=2, sticky="nsew")
        game_campaign.columnconfigure(0, weight=1)
        game_campaign.columnconfigure(1, weight=1)
        ttk.Label(game_campaign, text=_("gui", "progress", "game")).grid(column=0, row=0)
        ttk.Label(game_campaign, textvariable=self._vars["campaign"]["game"]).grid(column=0, row=1)
        ttk.Label(game_campaign, text=_("gui", "progress", "campaign")).grid(column=1, row=0)
        ttk.Label(game_campaign, textvariable=self._vars["campaign"]["name"]).grid(column=1, row=1)
        ttk.Label(
            frame, text=_("gui", "progress", "campaign_progress")
        ).grid(column=0, row=2, rowspan=2)
        ttk.Label(frame, textvariable=self._vars["campaign"]["percentage"]).grid(column=1, row=2)
        ttk.Label(frame, textvariable=self._vars["campaign"]["remaining"]).grid(column=1, row=3)
        ttk.Progressbar(
            frame,
            mode="determinate",
            length=self.BAR_LENGTH,
            maximum=1,
            variable=self._vars["campaign"]["progress"],
        ).grid(column=0, row=4, columnspan=2)
        ttk.Separator(
            frame, orient="horizontal"
        ).grid(row=5, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(frame, text=_("gui", "progress", "drop")).grid(column=0, row=6, columnspan=2)
        ttk.Label(
            frame, textvariable=self._vars["drop"]["rewards"]
        ).grid(column=0, row=7, columnspan=2)
        ttk.Label(
            frame, text=_("gui", "progress", "drop_progress")
        ).grid(column=0, row=8, rowspan=2)
        ttk.Label(frame, textvariable=self._vars["drop"]["percentage"]).grid(column=1, row=8)
        ttk.Label(frame, textvariable=self._vars["drop"]["remaining"]).grid(column=1, row=9)
        ttk.Progressbar(
            frame,
            mode="determinate",
            length=self.BAR_LENGTH,
            maximum=1,
            variable=self._vars["drop"]["progress"],
        ).grid(column=0, row=10, columnspan=2)
        self._drop: TimedDrop | None = None
        self._seconds: int = 0
        self._timer_task: asyncio.Task[None] | None = None
        self.display(None)

    def _divmod(self, minutes: int) -> tuple[int, int]:
        if self._seconds < 60 and minutes > 0:
            minutes -= 1
        hours, minutes = divmod(minutes, 60)
        return (hours, minutes)

    def _update_time(self, seconds: int | None = None):
        if seconds is not None:
            self._seconds = seconds
        drop = self._drop
        if drop is not None:
            drop_minutes = drop.remaining_minutes
            campaign_minutes = drop.campaign.remaining_minutes
        else:
            drop_minutes = 0
            campaign_minutes = 0
        drop_vars: _DropVars = self._vars["drop"]
        campaign_vars: _CampaignVars = self._vars["campaign"]
        dseconds = self._seconds % 60
        hours, minutes = self._divmod(drop_minutes)
        drop_vars["remaining"].set(
            _("gui", "progress", "remaining").format(time=f"{hours:>2}:{minutes:02}:{dseconds:02}")
        )
        hours, minutes = self._divmod(campaign_minutes)
        campaign_vars["remaining"].set(
            _("gui", "progress", "remaining").format(time=f"{hours:>2}:{minutes:02}:{dseconds:02}")
        )

    async def _timer_loop(self):
        self._update_time(60)
        while self._seconds > 0:
            await asyncio.sleep(1)
            self._seconds -= 1
            self._update_time()
        self._timer_task = None

    def start_timer(self):
        if self._timer_task is None:
            if self._drop is None or self._drop.remaining_minutes <= 0:
                # if we're starting the timer at 0 drop minutes,
                # all we need is a single instant time update setting seconds to 60,
                # to avoid substracting a minute from campaign minutes
                self._update_time(60)
            else:
                self._timer_task = asyncio.create_task(self._timer_loop())

    def stop_timer(self):
        if self._timer_task is not None:
            self._timer_task.cancel()
            self._timer_task = None

    def minute_almost_done(self) -> bool:
        # already or almost done
        return self._timer_task is None or self._seconds <= self.ALMOST_DONE_SECONDS

    def display(self, drop: TimedDrop | None, *, countdown: bool = True, subone: bool = False):
        self._drop = drop
        if hasattr(self._manager, "dashboard"):
            try:
                self._manager.dashboard.refresh_campaign()
            except Exception:
                logger.exception("Failed to refresh the dashboard's campaign view")
        vars_drop = self._vars["drop"]
        vars_campaign = self._vars["campaign"]
        self.stop_timer()
        if drop is None:
            # clear the drop display
            vars_drop["rewards"].set("...")
            vars_drop["progress"].set(0.0)
            vars_drop["percentage"].set("-%")
            vars_campaign["name"].set("...")
            vars_campaign["game"].set("...")
            vars_campaign["progress"].set(0.0)
            vars_campaign["percentage"].set("-%")
            self._update_time(0)
            return
        vars_drop["rewards"].set(drop.rewards_text())
        vars_drop["progress"].set(drop.progress)
        vars_drop["percentage"].set(f"{drop.progress:6.1%}")
        campaign = drop.campaign
        vars_campaign["name"].set(campaign.name)
        vars_campaign["game"].set(campaign.game.name)
        vars_campaign["progress"].set(campaign.progress)
        vars_campaign["percentage"].set(
            f"{campaign.progress:6.1%} ({campaign.claimed_drops}/{campaign.total_drops})"
        )
        if countdown:
            # restart our seconds update timer
            self.start_timer()
        elif subone:
            # display the current remaining time at 0 seconds (after substracting the minute)
            # this is because the watch loop will substract this minute
            # right after the first watch payload returns with a time update
            self._update_time(0)
        else:
            # display full time with no substracting
            self._update_time(60)


class ConsoleOutput:
    def __init__(self, manager: GUIManager, master: ttk.Widget):
        frame = ttk.LabelFrame(master, text=_("gui", "output"), padding=(4, 0, 4, 4))
        frame.grid(column=0, row=3, columnspan=3, sticky="nsew", padx=2)
        # tell master frame that the containing row can expand
        master.rowconfigure(3, weight=1)
        frame.rowconfigure(0, weight=1)  # let the frame expand
        frame.columnconfigure(0, weight=1)
        xscroll = ttk.Scrollbar(frame, orient="horizontal")
        yscroll = ttk.Scrollbar(frame, orient="vertical")
        self._text = tk.Text(
            frame,
            width=52,
            height=10,
            wrap="none",
            state="disabled",
            exportselection=False,
            xscrollcommand=xscroll.set,
            yscrollcommand=yscroll.set,
        )
        xscroll.config(command=self._text.xview)
        yscroll.config(command=self._text.yview)
        self._text.grid(column=0, row=0, sticky="nsew")
        xscroll.grid(column=0, row=1, sticky="ew")
        yscroll.grid(column=1, row=0, sticky="ns")

    def print(self, message: str):
        stamp = datetime.now().strftime("%X")
        if '\n' in message:
            message = message.replace('\n', f"\n{stamp}: ")
        self._text.config(state="normal")
        self._text.insert("end", f"{stamp}: {message}\n")
        self._text.see("end")  # scroll to the newly added line
        self._text.config(state="disabled")

    def configure_theme(self, *, bg: str, fg: str, sel_bg: str, sel_fg: str):
        # Apply colors to the Tk Text widget used for console output
        self._text.config(
            bg=bg,
            fg=fg,
            insertbackground=fg,
            selectbackground=sel_bg,
            selectforeground=sel_fg,
        )


class _Buttons(TypedDict):
    frame: ttk.Frame
    switch: ttk.Button


class ChannelList:
    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._manager = manager
        frame = ttk.LabelFrame(master, text=_("gui", "channels", "name"), padding=(4, 0, 4, 4))
        frame.grid(column=2, row=1, rowspan=2, sticky="nsew", padx=2)
        # tell master frame that the containing column can expand
        master.columnconfigure(2, weight=1)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        buttons_frame = ttk.Frame(frame)
        self._buttons: _Buttons = {
            "frame": buttons_frame,
            "switch": ttk.Button(
                buttons_frame,
                text=_("gui", "channels", "switch"),
                state="disabled",
                command=manager._twitch.state_change(State.CHANNEL_SWITCH),
            ),
        }
        buttons_frame.grid(column=0, row=0, columnspan=2)
        self._buttons["switch"].grid(column=0, row=0)
        scroll = ttk.Scrollbar(frame, orient="vertical")
        self._table = table = ttk.Treeview(
            frame,
            # columns definition is updated by _add_column
            yscrollcommand=scroll.set,
        )
        scroll.config(command=table.yview)
        table.grid(column=0, row=1, sticky="nsew")
        scroll.grid(column=1, row=1, sticky="ns")
        self._font = Font(frame, manager._style.lookup("Treeview", "font"))
        self._const_width: set[str] = set()
        table.tag_configure("watching", background="gray70")
        table.bind("<Button-1>", self._disable_column_resize)
        table.bind("<<TreeviewSelect>>", self._selected)
        self._add_column("#0", '', width=0)
        self._add_column(
            "channel", _("gui", "channels", "headings", "channel"), width=100, anchor='w'
        )
        self._add_column(
            "status",
            _("gui", "channels", "headings", "status"),
            width_template=[
                _("gui", "channels", "online"),
                _("gui", "channels", "pending"),
                _("gui", "channels", "offline"),
            ],
        )
        self._add_column("game", _("gui", "channels", "headings", "game"), width=50)
        self._add_column("drops", "🎁", width_template="✔")
        self._add_column(
            "viewers", _("gui", "channels", "headings", "viewers"), width_template="1234567"
        )
        self._add_column("acl_base", "📋", width_template="✔")
        self._channel_map: dict[str, Channel] = {}

    def _add_column(
        self,
        cid: str,
        name: str,
        *,
        anchor: _Anchor = "center",
        width: int | None = None,
        width_template: str | list[str] | None = None,
    ):
        table = self._table
        # NOTE: we don't do this for the icon column
        if cid != "#0":
            # we need to save the column settings and headings before modifying the columns...
            columns: tuple[str, ...] = table.cget("columns") or ()
            column_settings: dict[str, tuple[str, _Anchor, int, int]] = {}
            for s_cid in columns:
                s_column = table.column(s_cid)
                assert s_column is not None
                s_heading = table.heading(s_cid)
                assert s_heading is not None
                column_settings[s_cid] = (
                    s_heading["text"], s_heading["anchor"], s_column["width"], s_column["minwidth"]
                )
            # ..., then add the column
            table.config(columns=columns + (cid,))
            # ..., and then restore column settings and headings afterwards
            for s_cid, (s_name, s_anchor, s_width, s_minwidth) in column_settings.items():
                table.heading(s_cid, text=s_name, anchor=s_anchor)
                table.column(s_cid, minwidth=s_minwidth, width=s_width, stretch=False)
        # set heading and column settings for the new column
        if width_template is not None:
            if isinstance(width_template, str):
                width = self._measure(width_template)
            else:
                width = max((self._measure(template) for template in width_template), default=20)
            self._const_width.add(cid)
        assert width is not None
        table.heading(cid, text=name, anchor=anchor)
        table.column(cid, minwidth=width, width=width, stretch=False)

    def _disable_column_resize(self, event):
        if self._table.identify_region(event.x, event.y) == "separator":
            return "break"

    def _selected(self, event):
        selection = self._table.selection()
        if selection:
            self._buttons["switch"].config(state="normal")
        else:
            self._buttons["switch"].config(state="disabled")

    def _measure(self, text: str) -> int:
        # we need this because columns have 9-10 pixels of padding that cuts text off
        return self._font.measure(text) + 10

    def _redraw(self):
        # this forces a redraw that recalculates widget width
        self._table.event_generate("<<ThemeChanged>>")

    def _adjust_width(self, column: str, value: str):
        # causes the column to expand if the value's width is greater than the current width
        if column in self._const_width:
            return
        value_width = self._measure(value)
        curr_width = self._table.column(column, "width")
        if value_width > curr_width:
            self._table.column(column, width=value_width)
            self._redraw()

    def shrink(self):
        # causes the columns to shrink back after long values have been removed from it
        columns: tuple[str, ...] = self._table.cget("columns") or ()
        iids = self._table.get_children()
        for column in columns:
            if column in self._const_width:
                continue
            if iids:
                # table has at least one item
                # explicit column name makes Treeview.set return a single str value
                # instead of the dict[str, Any] overload used when column is omitted
                width = max(self._measure(self._table.set(i, str(column))) for i in iids)
                self._table.column(column, width=width)
            else:
                # no items - use minwidth
                minwidth = self._table.column(column, "minwidth")
                self._table.column(column, width=minwidth)
        self._redraw()

    def _set(self, iid: str, column: str, value: str):
        self._table.set(iid, column, value)
        self._adjust_width(column, value)

    def _insert(self, iid: str, values: dict[str, str]):
        to_insert: list[str] = []
        for cid in self._table.cget("columns"):
            value = values[cid]
            to_insert.append(value)
            self._adjust_width(cid, value)
        self._table.insert(parent='', index="end", iid=iid, values=to_insert)

    def clear_watching(self):
        for iid in self._table.tag_has("watching"):
            self._table.item(iid, tags='')

    def set_watching(self, channel: Channel):
        self.clear_watching()
        iid = channel.iid
        self._table.item(iid, tags="watching")
        self._table.see(iid)

    def get_selection(self) -> Channel | None:
        if not self._channel_map:
            return None
        selection = self._table.selection()
        if not selection:
            return None
        return self._channel_map[selection[0]]

    def clear_selection(self):
        self._table.selection_set('')

    def clear(self):
        iids = self._table.get_children()
        self._table.delete(*iids)
        self._channel_map.clear()
        self.shrink()

    def display(self, channel: Channel, *, add: bool = False):
        iid = channel.iid
        if not add and iid not in self._channel_map:
            # the channel isn't on the list and we're not supposed to add it
            return
        # ACL-based
        acl_based = "✔" if channel.acl_based else "❌"
        # status
        if channel.online:
            status = _("gui", "channels", "online")
        elif channel.pending_online:
            status = _("gui", "channels", "pending")
        else:
            status = _("gui", "channels", "offline")
        # game
        game = str(channel.game or '')
        # drops
        drops = "✔" if channel.drops_enabled else "❌"
        # viewers
        viewers = ''
        if channel.viewers is not None:
            viewers = str(channel.viewers)
        if iid in self._channel_map:
            self._set(iid, "game", game)
            self._set(iid, "drops", drops)
            self._set(iid, "status", status)
            self._set(iid, "viewers", viewers)
            self._set(iid, "acl_base", acl_based)
        elif add:
            self._channel_map[iid] = channel
            self._insert(
                iid,
                {
                    "game": game,
                    "drops": drops,
                    "status": status,
                    "viewers": viewers,
                    "acl_base": acl_based,
                    "channel": channel.name,
                },
            )

    def remove(self, channel: Channel):
        iid = channel.iid
        del self._channel_map[iid]
        self._table.delete(iid)


class TrayIcon:
    TITLE = "DropStream"

    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._manager = manager
        self.icon: pystray.Icon | None = None  # type: ignore[unused-ignore]
        self._icon_images: dict[str, Image_module.Image] = {
            "pickaxe": Image_module.open(resource_path("icons/pickaxe.ico")),
            "active": Image_module.open(resource_path("icons/active.ico")),
            "idle": Image_module.open(resource_path("icons/idle.ico")),
            "error": Image_module.open(resource_path("icons/error.ico")),
            "maint": Image_module.open(resource_path("icons/maint.ico")),
        }
        self._icon_state: str = "pickaxe"
        self._button = ttk.Button(master, command=self.minimize, text=_("gui", "tray", "minimize"))

        # Hides Tray button for macOS
        if sys.platform != "darwin":
            self._button.grid(column=0, row=0, sticky="ne")

    def __del__(self) -> None:
        self.stop()
        for icon_image in self._icon_images.values():
            icon_image.close()

    def _shorten(self, text: str, by_len: int, min_len: int) -> str:
        if (text_len := len(text)) <= min_len + 3 or by_len <= 0:
            # cannot shorten
            return text
        return text[:-min(by_len + 3, text_len - min_len)] + "..."

    def get_title(self, drop: TimedDrop | None) -> str:
        if drop is None:
            return self.TITLE
        campaign = drop.campaign
        title_parts: list[str] = [
            f"{self.TITLE}\n",
            f"{campaign.game.name}\n",
            drop.rewards_text(),
            f" {drop.progress:.1%} ({campaign.claimed_drops}/{campaign.total_drops})"
        ]
        min_len: int = 30
        max_len: int = 127
        missing_len = len(''.join(title_parts)) - max_len
        if missing_len > 0:
            # try shortening the reward text
            title_parts[2] = self._shorten(title_parts[2], missing_len, min_len)
            missing_len = len(''.join(title_parts)) - max_len
        if missing_len > 0:
            # try shortening the game name
            title_parts[1] = self._shorten(title_parts[1], missing_len, min_len)
            missing_len = len(''.join(title_parts)) - max_len
        if missing_len > 0:
            raise MinerException(f"Title couldn't be shortened: {''.join(title_parts)}")
        return ''.join(title_parts)

    def _start(self):
        loop = asyncio.get_running_loop()
        drop = self._manager.progress._drop

        # we need this because tray icon lives in a separate thread
        def bridge(func):
            return lambda: loop.call_soon_threadsafe(func)

        twitch = self._manager._twitch
        menu = pystray.Menu(
            pystray.MenuItem(_("gui", "tray", "show"), bridge(self.restore), default=True),
            # dynamic status line, disabled (display only)
            pystray.MenuItem(lambda item: self.get_title(self._manager.progress._drop), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda item: _("gui", "tray", "resume" if twitch.paused else "pause"),
                bridge(twitch.toggle_pause),
            ),
            pystray.MenuItem(
                _("gui", "tray", "switch_channel"),
                bridge(twitch.state_change(State.CHANNEL_SWITCH)),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(_("gui", "tray", "quit"), bridge(self.quit)),
        )
        icon = pystray.Icon(
            "twitch_miner", self._icon_images[self._icon_state], self.get_title(drop), menu
        )
        self.icon = icon
        # self.icon.run_detached()
        loop.run_in_executor(None, icon.run)

    def stop(self):
        if self.icon is not None:
            self.icon.stop()
            self.icon = None

    def quit(self):
        self._manager.close()

    def minimize(self):
        if sys.platform == "darwin":
            return
        if self.icon is None:
            self._start()
        else:
            self.icon.visible = True
        self._manager._root.withdraw()
        self._manager._minimized = True
        if self._manager._twitch.settings.low_power_tray_mode:
            # not visible anymore: drop the whole Inventory tab (widgets + images) and
            # nearly all cached images from RAM, they'll get rebuilt/redecoded once
            # restored. This is opt-in since it makes things noticeably slower right
            # after restoring, or when opening Inventory, while it reloads.
            if self._manager.inv is not None:
                # bug fix: this used to run unconditionally and crash with
                # AttributeError when show_inventory_tab is disabled (inv is None)
                self._manager.inv.clear()
            self._manager._cache.trim()
            self._manager._inventory_dirty = True
            gc.collect()

    def restore(self):
        if self.icon is not None:
            # self.stop()
            self.icon.visible = False
        self._manager._root.deiconify()
        self._manager._minimized = False

    def notify(
        self, message: str, title: str | None = None, duration: float = 10
    ) -> asyncio.Task[None] | None:
        # do nothing if the user disabled notifications
        if not self._manager._twitch.settings.tray_notifications:
            return None
        if self.icon is not None:
            icon = self.icon  # nonlocal scope bind

            async def notifier():
                icon.notify(message, title)
                await asyncio.sleep(duration)
                icon.remove_notification()

            return asyncio.create_task(notifier())
        return None

    def update_title(self, drop: TimedDrop | None):
        if self.icon is not None:
            self.icon.title = self.get_title(drop)

    def change_icon(self, state: str):
        if state not in self._icon_images:
            raise ValueError("Invalid icon state")
        self._icon_state = state
        if self.icon is not None:
            self.icon.icon = self._icon_images[state]


class Notebook:
    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._nb = ttk.Notebook(master)
        self._nb.grid(column=0, row=0, sticky="nsew")
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)
        # prevent entries from being selected after switching tabs
        self._nb.bind("<<NotebookTabChanged>>", lambda event: manager._root.focus_set())
        # tab widget -> icon key, so icons can be recolored/reapplied when the theme changes
        self._icon_keys: dict[ttk.Widget, str] = {}
        self._icons: dict[str, PhotoImage] = {}
        # per-tab wrapper canvases (see add_tab): plain tk.Canvas has no ttk theme awareness,
        # so its background must be set manually and kept in sync on theme changes, otherwise
        # it shows through as a plain white/gray rectangle whenever a tab's content is shorter
        # than the window (e.g. after resizing taller, or with filters hiding rows).
        self._page_canvases: list[tk.Canvas] = []
        # content widget -> wrapper page, so a tab can be identified by its content widget
        # instead of a fragile hardcoded index that breaks if tabs are added/removed
        self._content_to_page: dict[ttk.Widget, ttk.Widget] = {}
        # let the user cycle through tabs with the mouse wheel (while hovering the tab bar)
        # or Ctrl+PageUp/PageDown from anywhere, so tabs stay reachable even when the window
        # is too narrow to show every tab's label at once
        self._nb.bind("<MouseWheel>", self._on_mousewheel)  # Windows / macOS
        self._nb.bind("<Button-4>", lambda event: self._cycle_tab(-1))  # Linux scroll up
        self._nb.bind("<Button-5>", lambda event: self._cycle_tab(1))  # Linux scroll down
        manager._root.bind_all("<Control-Prior>", lambda event: self._cycle_tab(-1))
        manager._root.bind_all("<Control-Next>", lambda event: self._cycle_tab(1))

    def _on_mousewheel(self, event: tk.Event[ttk.Notebook]) -> None:
        # event.delta is positive when scrolling up, negative when scrolling down (Windows
        # gives multiples of 120, macOS gives smaller values - the sign is what matters here)
        self._cycle_tab(-1 if event.delta > 0 else 1)

    def _cycle_tab(self, direction: int) -> None:
        tabs = self._nb.tabs()
        if not tabs:
            return
        current = self._nb.index("current")
        new_index = (current + direction) % len(tabs)
        self._nb.select(new_index)

    def add_tab(
        self, widget: ttk.Widget, *, name: str, icon_key: str | None = None,
        fill_height: bool = False, **kwargs,
    ):
        kwargs.pop("text", None)
        if "sticky" not in kwargs:
            kwargs["sticky"] = "nsew"
        # Wrap the (already fully built) tab content in a scrollable page: if the window
        # ends up too small to show everything, the content scrolls vertically instead of
        # being clipped. The scrollbar only appears once the content actually doesn't fit.
        page = ttk.Frame(self._nb)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(0, weight=1)
        canvas = tk.Canvas(page, highlightthickness=0, borderwidth=0)
        canvas.grid(column=0, row=0, sticky="nsew")
        self._page_canvases.append(canvas)
        vbar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        window_id = canvas.create_window((0, 0), window=widget, anchor="nw")

        def sync_scrollregion(event: tk.Event[tk.Misc] | None = None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            if widget.winfo_reqheight() > canvas.winfo_height():
                vbar.grid(column=1, row=0, sticky="ns")
            else:
                vbar.grid_remove()

        def on_canvas_resize(event: tk.Event[tk.Misc]) -> None:
            canvas.itemconfigure(window_id, width=event.width)
            if fill_height:
                # this tab manages its own internal scrolling (e.g. Inventory), so stretch
                # it to the full page height instead of leaving it at its natural (small)
                # size anchored at the top - that's what left half the tab empty before
                canvas.itemconfigure(window_id, height=event.height)
            sync_scrollregion()

        widget.bind("<Configure>", sync_scrollregion)
        canvas.bind("<Configure>", on_canvas_resize)

        def on_wheel(event: tk.Event[tk.Misc]) -> None:
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        def on_wheel_up(event: tk.Event[tk.Misc]) -> None:
            canvas.yview_scroll(-1, "units")

        def on_wheel_down(event: tk.Event[tk.Misc]) -> None:
            canvas.yview_scroll(1, "units")

        def bind_wheel(event: tk.Event[tk.Misc]) -> None:
            # only scroll-on-wheel while the cursor is actually over this tab's content,
            # so wheeling elsewhere in the window (e.g. a listbox) isn't hijacked
            canvas.bind_all("<MouseWheel>", on_wheel)
            canvas.bind_all("<Button-4>", on_wheel_up)
            canvas.bind_all("<Button-5>", on_wheel_down)

        def unbind_wheel(event: tk.Event[tk.Misc]) -> None:
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        canvas.bind("<Enter>", bind_wheel)
        canvas.bind("<Leave>", unbind_wheel)

        # give the canvas a sensible initial size so the window's default/natural size is
        # unaffected by this wrapper - it can still be shrunk smaller by the user afterward
        widget.update_idletasks()
        canvas.configure(width=widget.winfo_reqwidth(), height=widget.winfo_reqheight())

        self._nb.add(page, text=name, **kwargs)
        self._content_to_page[widget] = page
        if icon_key is not None:
            self._icon_keys[page] = icon_key

    def configure_theme(self, *, bg: str) -> None:
        for canvas in self._page_canvases:
            canvas.configure(bg=bg)

    def set_icons(self, icons: dict[str, PhotoImage]) -> None:
        # NOTE: these are PIL.ImageTk.PhotoImage instances (imported as PhotoImage above),
        # not tkinter.PhotoImage - both work as Tk widget images, but aren't the same type
        self._icons = icons
        for widget, key in self._icon_keys.items():
            icon = icons.get(key)
            if icon is not None:
                self._nb.tab(widget, image=icon, compound="left")

    def current_tab(self) -> int:
        return self._nb.index("current")

    def is_current(self, content_widget: ttk.Widget) -> bool:
        # identifies a tab by its content widget rather than a hardcoded index, so it stays
        # correct even if tabs are conditionally added/removed (e.g. Inventory tab)
        page = self._content_to_page.get(content_widget)
        return page is not None and str(self._nb.select()) == str(page)

    def add_view_event(self, callback: abc.Callable[[tk.Event[ttk.Notebook]], Any]):
        self._nb.bind("<<NotebookTabChanged>>", callback, True)


class DashboardTab:
    """Simple stats view: weekly claim activity, drops per game, hours saved."""

    BAR_COLOR = "#9146FF"
    BG = ""

    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._manager = manager
        self._twitch = manager._twitch
        self._stats = manager._twitch.stats
        manager.tabs.add_view_event(self._on_tab_switched)
        master.columnconfigure(0, weight=1)
        master.columnconfigure(1, weight=1)

        # Status card: running/paused/idle, live-bound to the same status text as the
        # status bar, plus pause/resume controls (including an optional "resume at" time).
        status_frame = ttk.LabelFrame(
            master, text=_("gui", "dashboard", "status"), padding=(4, 4)
        )
        status_frame.grid(column=0, row=0, columnspan=2, sticky="nsew", pady=(0, 8))
        # row 0: live status indicator + pause/resume toggle
        top_row = ttk.Frame(status_frame)
        top_row.grid(column=0, row=0, sticky="w")
        self._status_dot = tk.Canvas(top_row, width=14, height=14, highlightthickness=0)
        self._status_dot.grid(column=0, row=0, padx=(2, 6))
        self._status_dot_id = self._status_dot.create_oval(2, 2, 12, 12, fill="#808080")
        ttk.Label(
            top_row, textvariable=manager.status.text_var
        ).grid(column=1, row=0, sticky="w")
        self._pause_btn = ttk.Button(
            top_row, text=_("gui", "dashboard", "pause"), command=self._toggle_pause
        )
        self._pause_btn.grid(column=2, row=0, padx=(16, 0))
        ttk.Button(
            top_row, text=_("gui", "dashboard", "reload"), command=self._reload_from_server
        ).grid(column=3, row=0, padx=(6, 0))
        # row 1: "pause until HH:MM" - on its own line, with a hint bubble + placeholder
        bottom_row = ttk.Frame(status_frame)
        bottom_row.grid(column=0, row=1, sticky="w", pady=(6, 0))
        ttk.Label(
            bottom_row, text=_("gui", "dashboard", "resume_at")
        ).grid(column=0, row=0, padx=(2, 4))
        resume_entry = PlaceholderEntry(bottom_row, placeholder="14:30", width=8)
        resume_entry.grid(column=1, row=0)
        self._resume_at_entry = resume_entry
        ttk.Button(
            bottom_row, text=_("gui", "dashboard", "pause_until"), command=self._pause_until
        ).grid(column=2, row=0, padx=(6, 4))
        InfoTooltip(
            bottom_row, text=_("gui", "dashboard", "resume_at_info")
        ).grid(column=3, row=0)
        self._update_status_indicator()
        self._manager._root.after(5000, self._poll_status)

        # "Currently mining" live card - reuses the same StringVars as the Details tab's
        # progress widget, so it updates live without any extra polling.
        progress_vars = manager.progress._vars
        now_frame = ttk.LabelFrame(
            master, text=_("gui", "dashboard", "now_mining"), padding=(4, 4)
        )
        now_frame.grid(column=0, row=1, columnspan=2, sticky="nsew", pady=(0, 8))
        now_frame.columnconfigure(0, weight=1)
        now_frame.columnconfigure(1, weight=1)
        ttk.Label(now_frame, text=_("gui", "progress", "game")).grid(column=0, row=0, sticky="w")
        ttk.Label(
            now_frame, textvariable=progress_vars["campaign"]["game"]
        ).grid(column=0, row=1, sticky="w")
        ttk.Label(
            now_frame, text=_("gui", "progress", "drop")
        ).grid(column=1, row=0, sticky="w")
        ttk.Label(
            now_frame, textvariable=progress_vars["drop"]["rewards"]
        ).grid(column=1, row=1, sticky="w")
        ttk.Label(
            now_frame, text=_("gui", "progress", "drop_progress")
        ).grid(column=0, row=2, sticky="w", pady=(6, 0))
        ttk.Label(
            now_frame, textvariable=progress_vars["drop"]["percentage"]
        ).grid(column=1, row=2, sticky="w", pady=(6, 0))
        ttk.Progressbar(
            now_frame,
            mode="determinate",
            length=300,
            maximum=1,
            variable=progress_vars["drop"]["progress"],
        ).grid(column=0, row=3, columnspan=2, sticky="ew", pady=(2, 0))
        ttk.Label(
            now_frame, text=_("gui", "dashboard", "drop_remaining")
        ).grid(column=0, row=4, sticky="w", pady=(6, 0))
        ttk.Label(
            now_frame, textvariable=progress_vars["drop"]["remaining"]
        ).grid(column=1, row=4, sticky="w", pady=(6, 0))
        ttk.Label(
            now_frame, text=_("gui", "dashboard", "campaign_remaining")
        ).grid(column=0, row=5, sticky="w")
        ttk.Label(
            now_frame, textvariable=progress_vars["campaign"]["remaining"]
        ).grid(column=1, row=5, sticky="w")

        # Drop campaign section: every item obtainable in the current campaign, with images,
        # highlighting the drop currently being mined and marking already-claimed ones.
        campaign_frame = ttk.LabelFrame(
            master, text=_("gui", "dashboard", "campaign"), padding=(4, 4)
        )
        campaign_frame.grid(column=0, row=2, columnspan=2, sticky="nsew", pady=(0, 8))
        self._campaign_frame = campaign_frame
        self._campaign_items_frame: ttk.Frame | None = None
        self._campaign_image_refs: list[PhotoImage] = []  # keep references alive
        self._campaign_no_data_label = ttk.Label(
            campaign_frame, text=_("gui", "dashboard", "no_data")
        )
        self._campaign_no_data_label.grid(column=0, row=0)
        self._campaign_refresh_task: asyncio.Task[None] | None = None

        # summary counters
        summary = ttk.Frame(master)
        summary.grid(column=0, row=3, columnspan=2, sticky="w", pady=(0, 8))
        self._total_var = StringVar(master, "")
        self._hours_var = StringVar(master, "")
        ttk.Label(summary, textvariable=self._total_var).grid(column=0, row=0, padx=(0, 24))
        ttk.Label(summary, textvariable=self._hours_var).grid(column=1, row=0)

        # weekly progress chart
        week_frame = ttk.LabelFrame(
            master, text=_("gui", "dashboard", "weekly"), padding=(4, 4)
        )
        week_frame.grid(column=0, row=4, sticky="nsew", padx=(0, 4))
        self._week_canvas = tk.Canvas(week_frame, width=280, height=160, highlightthickness=0)
        self._week_canvas.grid(column=0, row=0)

        # drops per game chart
        game_frame = ttk.LabelFrame(
            master, text=_("gui", "dashboard", "per_game"), padding=(4, 4)
        )
        game_frame.grid(column=1, row=4, sticky="nsew", padx=(4, 0))
        self._game_canvas = tk.Canvas(game_frame, width=280, height=160, highlightthickness=0)
        self._game_canvas.grid(column=0, row=0)

        self.refresh()

    def _on_tab_switched(self, event: tk.Event[ttk.Notebook]) -> None:
        if self._manager.tabs.current_tab() == 0:  # Dashboard is the 1st tab
            self.refresh()

    def _toggle_pause(self) -> None:
        self._twitch.toggle_pause()
        self._update_status_indicator()

    def _reload_from_server(self) -> None:
        self._twitch.force_reload()

    def _pause_until(self) -> None:
        if self._resume_at_entry._ph:
            return  # placeholder still showing, no real value entered
        raw = self._resume_at_entry.get().strip()
        parsed = parse_hhmm(raw)
        if parsed is None:
            return
        now = datetime.now()
        resume_at = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
        if resume_at <= now:
            resume_at += timedelta(days=1)  # time already passed today: resume tomorrow
        self._twitch.pause_until(resume_at)
        self._update_status_indicator()

    def _update_status_indicator(self) -> None:
        color = "#e0a800" if self._twitch.paused else "#2ecc71"
        self._status_dot.itemconfig(self._status_dot_id, fill=color)
        self._pause_btn.config(
            text=_("gui", "dashboard", "resume" if self._twitch.paused else "pause")
        )

    def _poll_status(self) -> None:
        self._update_status_indicator()
        # while minimized in eco mode nobody can see this widget refresh, so back off
        # to a much longer interval instead of waking up every 5s for nothing
        interval = self._eco_poll_interval()
        self._manager._root.after(interval, self._poll_status)

    def _eco_poll_interval(self) -> int:
        if self._manager._minimized and self._twitch.settings.low_power_tray_mode:
            return 60_000
        return 5_000

    def refresh_campaign(self, *, force: bool = False) -> None:
        # schedules the async campaign rebuild (fetches/caches drop images).
        # Skipped entirely when nothing actually changed (same current drop, same claim
        # states) - display() is called on every progress tick (~once/minute) while mining,
        # so rebuilding every widget and re-touching every image each time would be wasteful.
        drop = self._manager.progress._drop
        drop_id = drop.id if drop is not None else None
        claimed_ids = (
            frozenset(d.id for d in drop.campaign.drops if d.is_claimed)
            if drop is not None else frozenset()
        )
        state = (drop_id, claimed_ids)
        if not force and state == getattr(self, "_last_campaign_state", None):
            return
        self._last_campaign_state = state
        if self._campaign_refresh_task is not None and not self._campaign_refresh_task.done():
            self._campaign_refresh_task.cancel()
        self._campaign_refresh_task = asyncio.create_task(self._refresh_campaign_async())

    async def _refresh_campaign_async(self) -> None:
        drop = self._manager.progress._drop
        # clear the previous content
        if self._campaign_items_frame is not None:
            self._campaign_items_frame.destroy()
            self._campaign_items_frame = None
        self._campaign_image_refs.clear()
        if drop is None:
            self._campaign_no_data_label.grid(column=0, row=0)
            return
        self._campaign_no_data_label.grid_remove()
        campaign = drop.campaign
        cache = self._manager._cache
        items_frame = ttk.Frame(self._campaign_frame)
        items_frame.grid(column=0, row=1, sticky="nsew")
        self._campaign_items_frame = items_frame
        # campaign box art on the left
        campaign_image = await cache.get(campaign.image_url, size=(80, 106))
        self._campaign_image_refs.append(campaign_image)
        ttk.Label(
            items_frame, image=campaign_image, text=campaign.name, compound="top",
            justify="center", wraplength=90,
        ).grid(column=0, row=0, rowspan=2, padx=(0, 10), sticky="n")
        # every drop/item obtainable in this campaign
        drops_row = ttk.Frame(items_frame)
        drops_row.grid(column=1, row=0, sticky="nsew")
        for i, campaign_drop in enumerate(campaign.drops):
            is_current = campaign_drop.id == drop.id
            card = ttk.Frame(
                drops_row,
                relief="solid" if is_current else "ridge",
                borderwidth=2 if is_current else 1,
                padding=4,
            )
            card.grid(column=i, row=0, padx=4, sticky="n")
            benefit_images: list[PhotoImage] = await asyncio.gather(
                *(cache.get(b.image_url, (56, 56)) for b in campaign_drop.benefits)
            )
            self._campaign_image_refs.extend(benefit_images)
            imgs_row = ttk.Frame(card)
            imgs_row.grid(column=0, row=0)
            for j, image in enumerate(benefit_images):
                ttk.Label(imgs_row, image=image).grid(column=j, row=0, padx=2)
            status = (
                "✓ " if campaign_drop.is_claimed
                else "▶ " if is_current
                else ""
            )
            ttk.Label(
                card, text=f"{status}{campaign_drop.rewards_text()}",
                justify="center", wraplength=140,
                style="green.TLabel" if campaign_drop.is_claimed else "TLabel",
            ).grid(column=0, row=1, pady=(4, 0))

    def apply_theme(self, bg: str, fg: str) -> None:
        # tk.Canvas isn't a ttk widget, so it doesn't pick up theme colors automatically
        for canvas in (self._status_dot, self._week_canvas, self._game_canvas):
            canvas.config(bg=bg)
        self._chart_bg = bg
        self._chart_fg = fg
        self._draw_bars(self._week_canvas, self._stats.weekly_progress())
        self._draw_bars(self._game_canvas, self._stats.drops_per_game())

    def _draw_bars(
        self, canvas: tk.Canvas, data: list[tuple[str, int]], *, w: int = 280, h: int = 160
    ) -> None:
        canvas.delete("all")
        text_fg = getattr(self, "_chart_fg", "#000000")
        if not data:
            canvas.create_text(
                w / 2, h / 2, text=_("gui", "dashboard", "no_data"), fill=text_fg
            )
            return
        max_val = max((v for _n, v in data), default=0) or 1
        pad_bottom = 24
        n = len(data)
        slot_w = w / n
        bar_w = max(6, slot_w * 0.5)
        for i, (label, value) in enumerate(data):
            x_center = i * slot_w + slot_w / 2
            bar_h = (value / max_val) * (h - pad_bottom - 12)
            y0 = h - pad_bottom - bar_h
            y1 = h - pad_bottom
            canvas.create_rectangle(
                x_center - bar_w / 2, y0, x_center + bar_w / 2, y1,
                fill=self.BAR_COLOR, outline="",
            )
            if value:
                canvas.create_text(
                    x_center, y0 - 8, text=str(value), font=("", 8), fill=text_fg
                )
            # truncate long game names
            short_label = label if len(label) <= 10 else label[:9] + "…"
            canvas.create_text(
                x_center, h - pad_bottom + 10, text=short_label, font=("", 8), fill=text_fg
            )

    def refresh(self) -> None:
        self.refresh_campaign(force=True)
        self._total_var.set(
            _("gui", "dashboard", "total_drops").format(count=self._stats.total_drops_claimed())
        )
        self._hours_var.set(
            _("gui", "dashboard", "hours_saved").format(
                hours=f"{self._stats.total_hours_saved():.1f}"
            )
        )
        self._draw_bars(self._week_canvas, self._stats.weekly_progress())
        self._draw_bars(self._game_canvas, self._stats.drops_per_game())


class CampaignDisplay(TypedDict):
    frame: ttk.Frame
    status: ttk.Label


class InventoryOverview:
    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._manager = manager
        self._master = master
        self._cache: ImageCache = manager._cache
        self._settings: Settings = manager._twitch.settings
        self._filters = {
            "not_linked": IntVar(
                master, self._settings.priority_mode is PriorityMode.PRIORITY_ONLY
            ),
            "upcoming": IntVar(master, 1),
            "expired": IntVar(master, 0),
            "excluded": IntVar(master, 0),
            "finished": IntVar(master, 0),
        }
        manager.tabs.add_view_event(self._on_tab_switched)
        # Filtering options
        filter_frame = ttk.LabelFrame(
            master, text=_("gui", "inventory", "filter", "name"), padding=(4, 0, 4, 4)
        )
        LABEL_SPACING = 20
        filter_frame.grid(column=0, row=0, columnspan=2, sticky="nsew")
        ttk.Label(
            filter_frame, text=_("gui", "inventory", "filter", "show"), padding=(0, 0, 10, 0)
        ).grid(column=0, row=0)
        icolumn = 0
        ttk.Checkbutton(
            filter_frame, variable=self._filters["not_linked"]
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Label(
            filter_frame,
            text=_("gui", "inventory", "filter", "not_linked"),
            padding=(0, 0, LABEL_SPACING, 0),
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Checkbutton(
            filter_frame, variable=self._filters["upcoming"]
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Label(
            filter_frame,
            text=_("gui", "inventory", "filter", "upcoming"),
            padding=(0, 0, LABEL_SPACING, 0),
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Checkbutton(
            filter_frame, variable=self._filters["expired"]
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Label(
            filter_frame,
            text=_("gui", "inventory", "filter", "expired"),
            padding=(0, 0, LABEL_SPACING, 0),
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Checkbutton(
            filter_frame, variable=self._filters["excluded"]
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Label(
            filter_frame,
            text=_("gui", "inventory", "filter", "excluded"),
            padding=(0, 0, LABEL_SPACING, 0),
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Checkbutton(
            filter_frame, variable=self._filters["finished"]
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Label(
            filter_frame,
            text=_("gui", "inventory", "filter", "finished"),
            padding=(0, 0, LABEL_SPACING, 0),
        ).grid(column=(icolumn := icolumn + 1), row=0)
        ttk.Button(
            filter_frame, text=_("gui", "inventory", "filter", "refresh"),
            command=self.reload_from_server,
        ).grid(column=(icolumn := icolumn + 1), row=0)
        # server health check
        self._server_status_var = StringVar(master, _("gui", "inventory", "server", "unknown"))
        ttk.Label(
            filter_frame, textvariable=self._server_status_var
        ).grid(column=(icolumn := icolumn + 1), row=0, padx=(LABEL_SPACING, 4))
        ttk.Button(
            filter_frame, text=_("gui", "inventory", "server", "check"),
            command=self.check_server_status,
        ).grid(column=(icolumn := icolumn + 1), row=0)
        # Inventory view
        self._canvas = tk.Canvas(master, scrollregion=(0, 0, 0, 0), highlightthickness=0)
        self._redraw_after_id: str | None = None
        self._canvas.grid(column=0, row=1, sticky="nsew")
        master.rowconfigure(1, weight=1)
        master.columnconfigure(0, weight=1)
        # NOTE: scrollbar/mousewheel commands are wrapped to force a full canvas redraw
        # afterwards - on Windows, ttk widgets embedded in a Canvas can leave "ghost"
        # duplicate copies of themselves behind when scrolled without this.
        xscroll = ttk.Scrollbar(master, orient="horizontal", command=self._scroll_x)
        xscroll.grid(column=0, row=2, sticky="ew")
        yscroll = ttk.Scrollbar(master, orient="vertical", command=self._scroll_y)
        yscroll.grid(column=1, row=1, sticky="ns")
        self._canvas.configure(xscrollcommand=xscroll.set, yscrollcommand=yscroll.set)
        self._canvas.bind("<Configure>", self._canvas_update)
        self._main_frame = ttk.Frame(self._canvas)
        self._main_frame.columnconfigure(0, weight=1)
        self._canvas.bind(
            "<Enter>", lambda e: self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        )
        self._canvas.bind("<Leave>", lambda e: self._canvas.unbind_all("<MouseWheel>"))
        self._canvas.create_window(0, 0, anchor="nw", window=self._main_frame)
        self._main_frame_window = self._canvas.find_all()[-1]
        self._campaigns: dict[DropsCampaign, CampaignDisplay] = {}
        self._drops: dict[str, ttk.Label] = {}

    def configure_theme(self, *, bg: str):
        # Canvas background needs manual control
        self._canvas.configure(bg=bg)

    def _update_visibility(self, campaign: DropsCampaign):
        # True if the campaign is supposed to show, False makes it hidden.
        frame = self._campaigns[campaign]["frame"]
        not_linked = bool(self._filters["not_linked"].get())
        expired = bool(self._filters["expired"].get())
        excluded = bool(self._filters["excluded"].get())
        upcoming = bool(self._filters["upcoming"].get())
        finished = bool(self._filters["finished"].get())
        priority_only = self._settings.priority_mode is PriorityMode.PRIORITY_ONLY
        if (
            campaign.required_minutes > 0  # don't show sub-only campaigns
            and (not_linked or campaign.eligible)
            and (campaign.active or upcoming and campaign.upcoming or expired and campaign.expired)
            and (
                excluded or (
                    campaign.game.name not in self._settings.exclude
                    and not priority_only or campaign.game.name in self._settings.priority
                )
            )
            and (finished or not campaign.finished)
        ):
            frame.grid()
        else:
            frame.grid_remove()

    def _on_tab_switched(self, event: tk.Event[ttk.Notebook]) -> None:
        if self._manager.tabs.is_current(self._master):
            # if a low-power tray minimize cleared this tab, rebuild it before showing
            # anything, instead of showing a blank/half-broken tab
            self._manager.ensure_inventory_reloaded()
            # refresh only if we're switching to the tab
            self.refresh()

    def get_status(self, campaign: DropsCampaign) -> tuple[str, str]:
        if campaign.active:
            status_text: str = _("gui", "inventory", "status", "active")
            status_color: str = "green"
        elif campaign.upcoming:
            status_text = _("gui", "inventory", "status", "upcoming")
            status_color = "goldenrod"
        else:
            status_text = _("gui", "inventory", "status", "expired")
            status_color = "red"
        return (status_text, status_color)

    def refresh(self):
        for campaign in self._campaigns:
            # status
            status_label = self._campaigns[campaign]["status"]
            status_text, status_color = self.get_status(campaign)
            status_label.config(text=status_text, foreground=status_color)
            # visibility
            self._update_visibility(campaign)
        self._canvas_update()

    def reload_from_server(self) -> None:
        self._manager._twitch.force_reload()

    def check_server_status(self) -> None:
        asyncio.create_task(self._check_server_status_async())

    async def _check_server_status_async(self) -> None:
        self._server_status_var.set(_("gui", "inventory", "server", "checking"))
        ok, latency_ms = await self._manager._twitch.check_server_status()
        key = "ok" if ok else "down"
        self._server_status_var.set(
            _("gui", "inventory", "server", key).format(ms=latency_ms)
        )

    def _canvas_update(self, event: tk.Event[tk.Canvas] | None = None):
        # stretch the inner frame to the canvas's width, so campaign cards use the full
        # available width instead of staying at their minimal natural size
        if event is not None and event.width > 1:
            self._canvas.itemconfigure(self._main_frame_window, width=event.width)
        self._canvas.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_mousewheel(self, event: tk.Event[tk.Misc]):
        delta = -1 if event.delta > 0 else 1
        state: int = event.state if isinstance(event.state, int) else 0
        if state & 1:
            self._canvas.xview_scroll(delta, "units")
        else:
            self._canvas.yview_scroll(delta, "units")
        self._force_redraw()

    def _scroll_x(self, *args) -> None:
        self._canvas.xview(*args)
        self._force_redraw()

    def _scroll_y(self, *args) -> None:
        self._canvas.yview(*args)
        self._force_redraw()

    def _force_redraw(self) -> None:
        # Windows-only ttk-in-Canvas ghosting workaround: force everything to repaint
        # after scrolling stops, otherwise stale duplicate copies of the widgets can linger.
        # Debounced (rather than firing on every single wheel notch/drag tick) so fast
        # scrolling stays smooth instead of forcing a full idletasks pass on every event.
        if self._redraw_after_id is not None:
            self._canvas.after_cancel(self._redraw_after_id)
        self._redraw_after_id = self._canvas.after(80, self._do_redraw)

    def _do_redraw(self) -> None:
        self._redraw_after_id = None
        self._canvas.update_idletasks()

    async def add_campaign(self, campaign: DropsCampaign) -> None:
        campaign_frame = ttk.Frame(self._main_frame, relief="ridge", borderwidth=1, padding=4)
        campaign_frame.grid(column=0, row=len(self._campaigns), sticky="nsew", pady=3)
        campaign_frame.rowconfigure(4, weight=1)
        campaign_frame.columnconfigure(1, weight=1)
        campaign_frame.columnconfigure(3, weight=10000)
        # Name
        ttk.Label(
            campaign_frame, text=campaign.name, takefocus=False, width=45
        ).grid(column=0, row=0, columnspan=2, sticky="w")
        # Status
        status_text, status_color = self.get_status(campaign)
        status_label = ttk.Label(
            campaign_frame, text=status_text, takefocus=False, foreground=status_color
        )
        status_label.grid(column=1, row=1, sticky="w", padx=4)
        # NOTE: We have to save the campaign's frame and status before any awaits happen,
        # otherwise the len(self._campaigns) call may overwrite an existing frame,
        # if the campaigns are added concurrently.
        self._campaigns[campaign] = {
            "frame": campaign_frame,
            "status": status_label,
        }
        # Starts / Ends
        MouseOverLabel(
            campaign_frame,
            text=_("gui", "inventory", "ends").format(
                time=campaign.ends_at.astimezone().replace(microsecond=0, tzinfo=None)
            ),
            alt_text=_("gui", "inventory", "starts").format(
                time=campaign.starts_at.astimezone().replace(microsecond=0, tzinfo=None)
            ),
            reverse=campaign.upcoming,
            takefocus=False,
        ).grid(column=1, row=2, sticky="w", padx=4)
        # Linking status
        if campaign.eligible:
            link_kwargs = {
                "style": '',
                "text": _("gui", "inventory", "status", "linked"),
                "foreground": "green",
            }
        else:
            link_kwargs = {
                "text": _("gui", "inventory", "status", "not_linked"),
                "foreground": "red",
            }
        LinkLabel(
            campaign_frame,
            link=campaign.link_url,
            takefocus=False,
            padding=0,
            **link_kwargs,
        ).grid(column=1, row=3, sticky="w", padx=4)
        # ACL channels
        acl = campaign.allowed_channels
        if acl:
            if len(acl) <= 5:
                allowed_text: str = '\n'.join(ch.name for ch in acl)
            else:
                allowed_text = '\n'.join(ch.name for ch in acl[:4])
                allowed_text += (
                    f"\n{_('gui', 'inventory', 'and_more').format(amount=len(acl) - 4)}"
                )
        else:
            allowed_text = _("gui", "inventory", "all_channels")
        ttk.Label(
            campaign_frame,
            text=f"{_('gui', 'inventory', 'allowed_channels')}\n{allowed_text}",
            takefocus=False,
        ).grid(column=1, row=4, sticky="nw", padx=4)
        # Image
        campaign_image = await self._cache.get(campaign.image_url, size=(108, 144))
        ttk.Label(campaign_frame, image=campaign_image).grid(column=0, row=1, rowspan=4)
        # Drops separator
        ttk.Separator(
            campaign_frame, orient="vertical", takefocus=False
        ).grid(column=2, row=0, rowspan=5, sticky="ns")
        # Drops display: cards wrap onto new rows instead of just growing sideways forever,
        # so the campaign fits the available width instead of forcing a horizontal scrollbar
        drops_row = ttk.Frame(campaign_frame)
        drops_row.grid(column=3, row=0, rowspan=5, sticky="nsew", padx=4)
        drop_frames: list[ttk.Frame] = []
        for i, drop in enumerate(campaign.drops):
            drop_frame = ttk.Frame(drops_row, relief="ridge", borderwidth=1, padding=5)
            drop_frame.grid(column=i, row=0, padx=4, pady=4)
            drop_frames.append(drop_frame)
            benefits_frame = ttk.Frame(drop_frame)
            benefits_frame.grid(column=0, row=0)
            benefit_images: list[PhotoImage] = await asyncio.gather(
                *(self._cache.get(benefit.image_url, (80, 80)) for benefit in drop.benefits)
            )
            for i, benefit, image in zip(range(len(drop.benefits)), drop.benefits, benefit_images):
                ttk.Label(
                    benefits_frame,
                    text=benefit.name,
                    image=image,
                    compound="bottom",
                ).grid(column=i, row=0, padx=5)
            self._drops[drop.id] = label = ttk.Label(drop_frame, justify=tk.CENTER)
            self.update_progress(drop, label)
            label.grid(column=0, row=1)

        def rewrap(event: tk.Event[tk.Misc] | None = None) -> None:
            if not drop_frames:
                return
            drop_frames[0].update_idletasks()
            card_width = drop_frames[0].winfo_reqwidth() + 8
            available = drops_row.winfo_width()
            cols = max(1, available // card_width) if available > 1 else len(drop_frames)
            for idx, frame in enumerate(drop_frames):
                r, c = divmod(idx, cols)
                frame.grid_configure(row=r, column=c)
            self._canvas_update()

        drops_row.bind("<Configure>", rewrap)
        drops_row.after_idle(rewrap)
        if self._manager.tabs.is_current(self._master):
            self._update_visibility(campaign)
            self._canvas_update()

    def clear(self) -> None:
        for child in self._main_frame.winfo_children():
            child.destroy()
        self._drops.clear()
        self._campaigns.clear()

    def update_progress(self, drop: TimedDrop, label: ttk.Label) -> None:
        progress_text: str
        progress_color: str = ''
        if drop.is_claimed:
            progress_color = "green"
            progress_text = _("gui", "inventory", "status", "claimed")
        elif drop.can_claim:
            progress_color = "goldenrod"
            progress_text = _("gui", "inventory", "status", "ready_to_claim")
        elif drop.current_minutes or drop.can_earn():
            progress_text = _("gui", "inventory", "percent_progress").format(
                percent=f"{drop.progress:3.1%}",
                minutes=drop.required_minutes,
            )
            if drop.ends_at < drop.campaign.ends_at:
                # this drop becomes unavailable earlier than the campaign ends
                progress_text += '\n' + _("gui", "inventory", "ends").format(
                    time=drop.ends_at.astimezone().replace(microsecond=0, tzinfo=None)
                )
        else:
            if drop.required_minutes > 0:
                progress_text = _("gui", "inventory", "minutes_progress").format(
                    minutes=drop.required_minutes
                )
            else:
                # required_minutes is zero for subscription-based drops
                progress_text = ''
            if datetime.now(timezone.utc) < drop.starts_at > drop.campaign.starts_at:
                # this drop can only be earned later than the campaign start
                progress_text += '\n' + _("gui", "inventory", "starts").format(
                    time=drop.starts_at.astimezone().replace(microsecond=0, tzinfo=None)
                )
            elif drop.ends_at < drop.campaign.ends_at:
                # this drop becomes unavailable earlier than the campaign ends
                progress_text += '\n' + _("gui", "inventory", "ends").format(
                    time=drop.ends_at.astimezone().replace(microsecond=0, tzinfo=None)
                )
        label.config(text=progress_text, foreground=progress_color)

    def update_drop(self, drop: TimedDrop) -> None:
        label = self._drops.get(drop.id)
        if label is None:
            return
        self.update_progress(drop, label)


def proxy_validate(entry: PlaceholderEntry, settings: Settings) -> bool:
    raw_url = entry.get().strip()
    entry.replace(raw_url)
    url = URL(raw_url)
    valid = url.host is not None and url.port is not None
    if not valid:
        entry.clear()
        url = URL()
    settings.proxy = url
    return valid


class _SettingsVars(TypedDict):
    tray: IntVar
    proxy: StringVar
    autostart: IntVar
    theme: StringVar
    use_system_accent: IntVar
    language: StringVar
    priority_mode: StringVar
    tray_notifications: IntVar
    enable_badges_emotes: IntVar
    mine_unlinked_campaigns: IntVar
    discord_rpc_enabled: IntVar
    discord_client_id: StringVar
    available_drops_check: IntVar
    schedule_enabled: IntVar
    schedule_start: StringVar
    schedule_end: StringVar
    auto_action: StringVar
    auto_restart_enabled: IntVar
    auto_restart_minutes: StringVar
    show_inventory_tab: IntVar
    low_power_tray_mode: IntVar


class SettingsPanel:
    AUTOSTART_NAME: str = "DropStream"
    AUTOSTART_KEY: str = "HKCU/Software/Microsoft/Windows/CurrentVersion/Run"

    @cached_property
    def PRIORITY_MODES(self) -> dict[PriorityMode, str]:
        # NOTE: Translation calls have to be deferred here,
        # to allow changing the language before the settings panel is initialized.
        pm = "priority_modes"
        return {
            PriorityMode.PRIORITY_ONLY: _("gui", "settings", pm, "priority_only"),
            PriorityMode.PRIORITY_ONLY_CONTINUE: _("gui", "settings", pm, "priority_only_continue"),
            PriorityMode.ENDING_SOONEST: _("gui", "settings", pm, "ending_soonest"),
            PriorityMode.PRIORITY_ENDING_SOONEST: _("gui", "settings", pm, "priority_ending_soonest"),
            PriorityMode.LOW_AVBL_FIRST: _("gui", "settings", pm, "low_availability"),
            PriorityMode.PRIORITY_LOW_AVBL_FIRST: _(
                "gui", "settings", pm, "priority_low_availability"
            ),
        }

    @cached_property
    def POWER_ACTIONS(self) -> dict[PowerAction, str]:
        return {
            PowerAction.NONE: _("gui", "settings", "power_actions", "none"),
            PowerAction.SLEEP: _("gui", "settings", "power_actions", "sleep"),
            PowerAction.SHUTDOWN: _("gui", "settings", "power_actions", "shutdown"),
        }

    @cached_property
    def THEMES(self) -> dict[str, str]:
        # NOTE: order matters, it's the order shown in the dropdown
        return {
            "auto": _("gui", "settings", "themes", "auto"),
            "light": _("gui", "settings", "themes", "light"),
            "dark": _("gui", "settings", "themes", "dark"),
            "modern_auto": _("gui", "settings", "themes", "modern_auto"),
            "modern_light": _("gui", "settings", "themes", "modern_light"),
            "modern_dark": _("gui", "settings", "themes", "modern_dark"),
        }

    def _on_discord_rpc_toggle(self) -> None:
        enabled = bool(self._vars["discord_rpc_enabled"].get())
        self._settings.discord_rpc_enabled = enabled
        if enabled:
            self._twitch.discord_rpc.connect()
            watching = self._twitch.watching_channel.get_with_default(None)
            if watching is not None:
                self._twitch.watch(watching, update_status=False)
        else:
            self._twitch.discord_rpc.close()

    def __init__(self, manager: GUIManager, master: ttk.Widget, games_master: ttk.Widget):
        self._manager = manager
        self._twitch = manager._twitch
        self._settings: Settings = manager._twitch.settings
        priority_mode = self._settings.priority_mode
        if priority_mode not in self.PRIORITY_MODES:
            priority_mode = PriorityMode.PRIORITY_ONLY
            self._settings.priority_mode = priority_mode
        current_theme = self._settings.theme
        if current_theme not in self.THEMES:
            current_theme = "auto"
            self._settings.theme = current_theme
        self._vars: _SettingsVars = {
            "autostart": IntVar(master, 0),
            "language": StringVar(master, _.current),
            "proxy": StringVar(master, str(self._settings.proxy)),
            "tray": IntVar(master, self._settings.autostart_tray),
            "theme": StringVar(master, self.THEMES[current_theme]),
            "use_system_accent": IntVar(master, int(self._settings.use_system_accent)),
            "priority_mode": StringVar(master, self.PRIORITY_MODES[priority_mode]),
            "tray_notifications": IntVar(master, self._settings.tray_notifications),
            "enable_badges_emotes": IntVar(
                master, int(self._settings.enable_badges_emotes)
            ),
            "mine_unlinked_campaigns": IntVar(
                master, int(self._settings.mine_unlinked_campaigns)
            ),
            "discord_rpc_enabled": IntVar(
                master, int(self._settings.discord_rpc_enabled)
            ),
            "discord_client_id": StringVar(master, self._settings.discord_client_id),
            "available_drops_check": IntVar(
                master, int(self._settings.available_drops_check)
            ),
            "schedule_enabled": IntVar(master, int(self._settings.schedule_enabled)),
            "schedule_start": StringVar(master, self._settings.schedule_start),
            "schedule_end": StringVar(master, self._settings.schedule_end),
            "auto_action": StringVar(master, self.POWER_ACTIONS[self._settings.auto_action]),
            "auto_restart_enabled": IntVar(master, int(self._settings.auto_restart_enabled)),
            "auto_restart_minutes": StringVar(master, str(self._settings.auto_restart_minutes)),
            "low_power_tray_mode": IntVar(master, int(self._settings.low_power_tray_mode)),
            "show_inventory_tab": IntVar(master, int(self._settings.show_inventory_tab)),
        }
        self._game_names: set[str] = set()
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)
        # use a frame to center the content within the tab
        center_frame = ttk.Frame(master)
        center_frame.grid(column=0, row=0)

        # General section
        general_frame = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "settings", "general", "name")
        )
        general_frame.grid(column=0, row=0, sticky="nsew")
        # use another frame to center the options within the section
        # NOTE: this can be adjusted or removed later on if more options were to be added
        general_frame.rowconfigure(0, weight=1)
        general_frame.columnconfigure(0, weight=1)
        general_center = ttk.Frame(general_frame)
        general_center.grid(column=0, row=0)

        # language frame
        language_frame = ttk.Frame(general_center)
        language_frame.grid(column=0, row=0)
        ttk.Label(language_frame, text="Language 🌐 (requires restart): ").grid(column=0, row=0)
        SelectCombobox(
            language_frame,
            values=list(_.languages),
            textvariable=self._vars["language"],
            command=lambda e: setattr(self._settings, "language", self._vars["language"].get()),
        ).grid(column=1, row=0)

        # checkboxes frame
        checkboxes_frame = ttk.Frame(general_center)
        checkboxes_frame.grid(column=0, row=1)
        ttk.Label(
            checkboxes_frame, text=_("gui", "settings", "general", "autostart")
        ).grid(column=0, row=(irow := 0), sticky="e")
        ttk.Checkbutton(
            checkboxes_frame, variable=self._vars["autostart"], command=self.update_autostart
        ).grid(column=1, row=irow, sticky="w")
        self._vars["autostart"].set(self._query_autostart())
        if sys.platform != "darwin":
            ttk.Label(
                checkboxes_frame, text=_("gui", "settings", "general", "tray")
            ).grid(column=0, row=(irow := irow + 1), sticky="e")
            ttk.Checkbutton(
                checkboxes_frame, variable=self._vars["tray"], command=self.update_autostart
            ).grid(column=1, row=irow, sticky="w")
            ttk.Label(
                checkboxes_frame, text=_("gui", "settings", "general", "tray_notifications")
            ).grid(column=0, row=(irow := irow + 1), sticky="e")
            ttk.Checkbutton(
                checkboxes_frame,
                variable=self._vars["tray_notifications"],
                command=lambda: setattr(
                    self._settings,
                    "tray_notifications",
                    bool(self._vars["tray_notifications"].get()),
                ),
            ).grid(column=1, row=irow, sticky="w")
        ttk.Label(
            checkboxes_frame, text=_("gui", "settings", "general", "low_power_tray_mode")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Checkbutton(
            checkboxes_frame,
            variable=self._vars["low_power_tray_mode"],
            command=lambda: setattr(
                self._settings,
                "low_power_tray_mode",
                bool(self._vars["low_power_tray_mode"].get()),
            ),
        ).grid(column=1, row=irow, sticky="w")
        InfoTooltip(
            checkboxes_frame, text=_("gui", "settings", "general", "low_power_tray_mode_info")
        ).grid(column=2, row=irow, sticky="w", padx=(4, 0))
        ttk.Label(
            checkboxes_frame, text=_("gui", "settings", "general", "theme")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        SelectCombobox(
            checkboxes_frame,
            command=self.update_theme,
            textvariable=self._vars["theme"],
            values=list(self.THEMES.values()),
        ).grid(column=1, row=irow, sticky="w")
        ttk.Label(
            checkboxes_frame, text=_("gui", "settings", "general", "use_system_accent")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Checkbutton(
            checkboxes_frame,
            variable=self._vars["use_system_accent"],
            command=self.update_use_system_accent,
        ).grid(column=1, row=irow, sticky="w")
        ttk.Label(
            checkboxes_frame, text=_("gui", "settings", "general", "show_inventory_tab")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Checkbutton(
            checkboxes_frame,
            variable=self._vars["show_inventory_tab"],
            command=lambda: setattr(
                self._settings,
                "show_inventory_tab",
                bool(self._vars["show_inventory_tab"].get()),
            ),
        ).grid(column=1, row=irow, sticky="w")
        InfoTooltip(
            checkboxes_frame, text=_("gui", "settings", "general", "show_inventory_tab_info")
        ).grid(column=2, row=irow, sticky="w", padx=(4, 0))
        # proxy frame
        proxy_frame = ttk.Frame(general_center)
        proxy_frame.grid(column=0, row=2)
        ttk.Label(proxy_frame, text=_("gui", "settings", "general", "proxy")).grid(column=0, row=0)
        self._proxy = PlaceholderEntry(
            proxy_frame,
            width=37,
            validate="focusout",
            prefill="http://",
            textvariable=self._vars["proxy"],
            placeholder="http://username:password@address:port",
        )
        self._proxy.config(validatecommand=partial(proxy_validate, self._proxy, self._settings))
        self._proxy.grid(column=0, row=1)

        # Accounts section (multi-account profiles)
        accounts_frame = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "settings", "accounts", "name")
        )
        accounts_frame.grid(column=0, row=1, sticky="nsew")
        accounts_frame.columnconfigure(0, weight=1)
        accounts_frame.rowconfigure(0, weight=1)
        accounts_center = ttk.Frame(accounts_frame)
        accounts_center.grid(column=0, row=0)
        ttk.Label(
            accounts_center,
            text=_("gui", "settings", "accounts", "current").format(name=ACTIVE_PROFILE),
        ).grid(column=0, row=0, columnspan=2, sticky="w")
        self._profiles_list = tk.Listbox(accounts_center, height=4, width=30, exportselection=False)
        self._profiles_list.grid(column=0, row=1, columnspan=2, sticky="w", pady=(4, 4))
        self.refresh_profiles()
        new_profile_frame = ttk.Frame(accounts_center)
        new_profile_frame.grid(column=0, row=2, columnspan=2, sticky="w")
        self._new_profile_var = StringVar(master, "")
        ttk.Entry(
            new_profile_frame, width=20, textvariable=self._new_profile_var
        ).grid(column=0, row=0)
        ttk.Button(
            new_profile_frame,
            text=_("gui", "settings", "accounts", "create"),
            command=self.create_profile,
        ).grid(column=1, row=0, padx=(4, 0))
        buttons_frame = ttk.Frame(accounts_center)
        buttons_frame.grid(column=0, row=3, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(
            buttons_frame,
            text=_("gui", "settings", "accounts", "launch"),
            command=self.launch_profile,
        ).grid(column=0, row=0)
        ttk.Button(
            buttons_frame,
            text=_("gui", "settings", "accounts", "switch"),
            command=self.switch_profile,
        ).grid(column=1, row=0, padx=(4, 0))
        ttk.Button(
            buttons_frame,
            text=_("gui", "settings", "accounts", "delete"),
            command=self.delete_profile,
        ).grid(column=2, row=0, padx=(4, 0))

        # Scheduler section
        schedule_frame = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "settings", "scheduler", "name")
        )
        schedule_frame.grid(column=0, row=2, sticky="nsew")
        schedule_frame.columnconfigure(0, weight=1)
        schedule_frame.rowconfigure(0, weight=1)
        schedule_center = ttk.Frame(schedule_frame)
        schedule_center.grid(column=0, row=0)
        ttk.Label(
            schedule_center, text=_("gui", "settings", "scheduler", "enabled")
        ).grid(column=0, row=(irow := 0), sticky="e")
        ttk.Checkbutton(
            schedule_center, variable=self._vars["schedule_enabled"], command=self.update_schedule
        ).grid(column=1, row=irow, sticky="w")
        ttk.Label(
            schedule_center, text=_("gui", "settings", "scheduler", "start")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Entry(
            schedule_center, width=8, textvariable=self._vars["schedule_start"]
        ).grid(column=1, row=irow, sticky="w")
        self._vars["schedule_start"].trace_add("write", lambda *a: self.update_schedule())
        ttk.Label(
            schedule_center, text=_("gui", "settings", "scheduler", "end")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Entry(
            schedule_center, width=8, textvariable=self._vars["schedule_end"]
        ).grid(column=1, row=irow, sticky="w")
        self._vars["schedule_end"].trace_add("write", lambda *a: self.update_schedule())
        ttk.Label(
            schedule_center, text=_("gui", "settings", "scheduler", "auto_action")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        SelectCombobox(
            schedule_center,
            command=self.auto_action,
            textvariable=self._vars["auto_action"],
            values=list(self.POWER_ACTIONS.values()),
        ).grid(column=1, row=irow, sticky="w")

        # Reliability section (auto-retry is automatic and unconditional; this only covers
        # the last-resort case where the app gives up and shows a "Terminated" screen)
        reliability_frame = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "settings", "reliability", "name")
        )
        reliability_frame.grid(column=0, row=3, sticky="nsew")
        reliability_frame.columnconfigure(0, weight=1)
        reliability_frame.rowconfigure(0, weight=1)
        reliability_center = ttk.Frame(reliability_frame)
        reliability_center.grid(column=0, row=0)
        ttk.Label(
            reliability_center, text=_("gui", "settings", "reliability", "info"),
            foreground="goldenrod", wraplength=280, justify="left",
        ).grid(column=0, row=(irow := 0), columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(
            reliability_center, text=_("gui", "settings", "reliability", "enabled")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Checkbutton(
            reliability_center,
            variable=self._vars["auto_restart_enabled"],
            command=self.update_auto_restart,
        ).grid(column=1, row=irow, sticky="w")
        ttk.Label(
            reliability_center, text=_("gui", "settings", "reliability", "minutes")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        minutes_entry = ttk.Entry(
            reliability_center, width=6, textvariable=self._vars["auto_restart_minutes"]
        )
        minutes_entry.grid(column=1, row=irow, sticky="w")
        minutes_entry.bind("<FocusOut>", lambda e: self.update_auto_restart())

        # Advanced section
        advanced_frame = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "settings", "advanced", "name")
        )
        advanced_frame.grid(column=0, row=4, sticky="nsew")
        advanced_frame.columnconfigure(0, weight=1)
        advanced_frame.rowconfigure(0, weight=1)
        advanced_center = ttk.Frame(advanced_frame)
        advanced_center.grid(column=0, row=0)

        # Warning message
        ttk.Label(
            advanced_center, text=_("gui", "settings", "advanced", "warning"), foreground="red"
        ).grid(column=0, row=(irow := 0), columnspan=2)
        ttk.Label(
            advanced_center,
            text=_("gui", "settings", "advanced", "warning_text"),
            foreground="goldenrod",
        ).grid(column=0, row=(irow := irow + 1), columnspan=2)
        # Toggles for badges and emotes, and available drops check
        ttk.Label(
            advanced_center, text=_("gui", "settings", "advanced", "enable_badges_emotes")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Checkbutton(
            advanced_center,
            variable=self._vars["enable_badges_emotes"],
            command=lambda: setattr(
                self._settings,
                "enable_badges_emotes",
                bool(self._vars["enable_badges_emotes"].get()),
            ),
        ).grid(column=1, row=irow, sticky="w")
        ttk.Label(
            advanced_center, text=_("gui", "settings", "advanced", "mine_unlinked_campaigns")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Checkbutton(
            advanced_center,
            variable=self._vars["mine_unlinked_campaigns"],
            command=lambda: setattr(
                self._settings,
                "mine_unlinked_campaigns",
                bool(self._vars["mine_unlinked_campaigns"].get()),
            ),
        ).grid(column=1, row=irow, sticky="w")
        ttk.Label(
            advanced_center, text=_("gui", "settings", "advanced", "discord_rpc_enabled")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Checkbutton(
            advanced_center,
            variable=self._vars["discord_rpc_enabled"],
            command=self._on_discord_rpc_toggle,
        ).grid(column=1, row=irow, sticky="w")
        ttk.Label(
            advanced_center, text=_("gui", "settings", "advanced", "discord_client_id")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Entry(
            advanced_center, textvariable=self._vars["discord_client_id"], width=24
        ).grid(column=1, row=irow, sticky="w")
        self._vars["discord_client_id"].trace_add(
            "write",
            lambda *_a: (
                setattr(self._settings, "discord_client_id", self._vars["discord_client_id"].get()),
                setattr(
                    self._twitch.discord_rpc, "_client_id",
                    self._vars["discord_client_id"].get(),
                ),
            ),
        )
        ttk.Label(
            advanced_center, text=_("gui", "settings", "advanced", "available_drops_check")
        ).grid(column=0, row=(irow := irow + 1), sticky="e")
        ttk.Checkbutton(
            advanced_center,
            variable=self._vars["available_drops_check"],
            command=lambda: setattr(
                self._settings,
                "available_drops_check",
                bool(self._vars["available_drops_check"].get()),
            ),
        ).grid(column=1, row=irow, sticky="w")

        # Games tab: priority mode + priority list + exclude list
        games_master.rowconfigure(0, weight=1)
        games_master.columnconfigure(0, weight=1)
        games_center = ttk.Frame(games_master)
        games_center.grid(column=0, row=0, sticky="nsew")
        games_center.columnconfigure(0, weight=1)
        games_center.columnconfigure(1, weight=1)
        games_center.rowconfigure(1, weight=1)

        priority_mode_frame = ttk.Frame(games_center)
        priority_mode_frame.grid(column=0, row=0, columnspan=2, pady=(0, 4))
        ttk.Label(
            priority_mode_frame, text=_("gui", "settings", "general", "priority_mode")
        ).grid(column=0, row=0)
        SelectCombobox(
            priority_mode_frame,
            command=self.priority_mode,
            textvariable=self._vars["priority_mode"],
            values=list(self.PRIORITY_MODES.values()),
        ).grid(column=1, row=0)
        InfoTooltip(
            priority_mode_frame, text=_("gui", "settings", "general", "priority_mode_info")
        ).grid(column=2, row=0, sticky="w", padx=(4, 0))
        ttk.Button(
            priority_mode_frame, text=_("gui", "dashboard", "reload"),
            command=lambda: self._manager._twitch.change_state(State.GAMES_UPDATE),
        ).grid(column=3, row=0, padx=(10, 0))

        # Priority section
        priority_frame = ttk.LabelFrame(
            games_center, padding=(4, 0, 4, 4), text=_("gui", "settings", "priority")
        )
        priority_frame.grid(column=0, row=1, rowspan=5, sticky="nsew")
        self._priority_entry = PlaceholderCombobox(
            priority_frame, placeholder=_("gui", "settings", "game_name"), width=30
        )
        self._priority_entry.grid(column=0, row=0, sticky="ew")
        priority_frame.columnconfigure(0, weight=1)
        ttk.Button(
            priority_frame, text="➕", command=self.priority_add, width=3, style="Large.TButton"
        ).grid(column=1, row=0, sticky="nsew")
        self._priority_list = PaddedListbox(
            priority_frame,
            height=12,
            padding=(1, 0),
            activestyle="none",
            selectmode="single",
            highlightthickness=0,
            exportselection=False,
        )
        self._priority_list.grid(column=0, row=1, rowspan=5, sticky="nsew")
        self._priority_list.insert("end", *self._settings.priority)
        weight_scale: int = 5
        ttk.Button(  # Move to top
            priority_frame,
            width=2,
            text="⇈",
            style="Arrow.TButton",
            command=partial(self.priority_move, MAX_INT),
        ).grid(column=1, row=1, sticky="nsew")
        priority_frame.rowconfigure(1, weight=1)
        ttk.Button(  # Move up
            priority_frame,
            width=2,
            text="↑",
            style="Arrow.TButton",
            command=partial(self.priority_move, 1),
        ).grid(column=1, row=2, sticky="nsew")
        priority_frame.rowconfigure(2, weight=weight_scale)
        ttk.Button(  # Move down
            priority_frame,
            width=2,
            text="↓",
            style="Arrow.TButton",
            command=partial(self.priority_move, -1),
        ).grid(column=1, row=3, sticky="nsew")
        priority_frame.rowconfigure(3, weight=weight_scale)
        ttk.Button(  # Move to bottom
            priority_frame,
            width=2,
            text="⇊",
            style="Arrow.TButton",
            command=partial(self.priority_move, -MAX_INT),
        ).grid(column=1, row=4, sticky="nsew")
        priority_frame.rowconfigure(4, weight=1)
        ttk.Button(
            priority_frame, text="❌", command=self.priority_delete, width=3, style="Large.TButton"
        ).grid(column=1, row=5, sticky="nsew")
        priority_frame.rowconfigure(5, weight=1)

        # Exclude section
        exclude_frame = ttk.LabelFrame(
            games_center, padding=(4, 0, 4, 4), text=_("gui", "settings", "exclude")
        )
        exclude_frame.grid(column=1, row=1, rowspan=5, sticky="nsew")
        exclude_frame.columnconfigure(0, weight=1)
        self._exclude_entry = PlaceholderCombobox(
            exclude_frame, placeholder=_("gui", "settings", "game_name"), width=26
        )
        self._exclude_entry.grid(column=0, row=0, sticky="ew")
        ttk.Button(
            exclude_frame, text="➕", command=self.exclude_add, width=3, style="Large.TButton"
        ).grid(column=1, row=0)
        self._exclude_list = PaddedListbox(
            exclude_frame,
            height=12,
            padding=(1, 0),
            activestyle="none",
            selectmode="single",
            highlightthickness=0,
            exportselection=False,
        )
        self._exclude_list.grid(column=0, row=1, columnspan=2, sticky="nsew")
        exclude_frame.rowconfigure(1, weight=1)
        # insert them alphabetically
        self._exclude_list.insert("end", *sorted(self._settings.exclude))
        ttk.Button(
            exclude_frame, text="❌", command=self.exclude_delete, width=3, style="Large.TButton"
        ).grid(column=0, row=2, columnspan=2, sticky="nsew")

        # Reload button
        reload_frame = ttk.Frame(center_frame)
        reload_frame.grid(column=0, row=5, pady=4)
        ttk.Label(reload_frame, text=_("gui", "settings", "reload_text")).grid(column=0, row=0)
        ttk.Button(
            reload_frame,
            text=_("gui", "settings", "reload"),
            command=self._manager._twitch.force_reload,
        ).grid(column=1, row=0)

    def clear_selection(self) -> None:
        self._priority_list.selection_clear(0, "end")
        self._exclude_list.selection_clear(0, "end")

    def update_theme(self, event: tk.Event[ttk.Combobox] | None = None) -> None:
        theme_label: str = self._vars["theme"].get()
        for value, label in self.THEMES.items():
            if theme_label == label:
                self._settings.theme = value
                self._manager.apply_theme_choice(value)
                break

    def _get_self_path(self) -> str:
        # NOTE: we need double quotes in case the path contains spaces
        return f'"{SELF_PATH.resolve()!s}"'

    def _get_autostart_path(self) -> str:
        flags: list[str] = []
        # if applicable, include the current logging level as well
        for lvl_idx, lvl_value in LOGGING_LEVELS.items():
            if lvl_value == self._settings.logging_level:
                if lvl_idx > 0:
                    flags.append(f"-{'v' * lvl_idx}")
                break
        if self._vars["tray"].get():
            flags.append("--tray")
        if not IS_PACKAGED:
            # non-packaged autostart has to be done through the venv path pythonw
            return f"\"{SCRIPTS_PATH / 'pythonw'!s}\" {self._get_self_path()} {' '.join(flags)}"
        return f"{self._get_self_path()} {' '.join(flags)}"

    def _get_linux_autostart_filepath(self) -> Path:
        autostart_folder: Path = Path("~/.config/autostart").expanduser()
        if (config_home := os.environ.get("XDG_CONFIG_HOME")) is not None:
            config_autostart: Path = Path(config_home, "autostart").expanduser()
            if config_autostart.exists():
                autostart_folder = config_autostart
        return autostart_folder / f"{self.AUTOSTART_NAME}.desktop"

    def _get_mac_autostart_filepath(self) -> Path:
        return Path(
            Path.home(), f"Library/LaunchAgents/com.devilxd.{self.AUTOSTART_NAME.lower()}.plist"
        )

    def _query_autostart(self) -> bool:
        if sys.platform == "win32":
            with RegistryKey(self.AUTOSTART_KEY, read_only=True) as key:
                try:
                    value_type, value = key.get(self.AUTOSTART_NAME)
                except ValueNotFound:
                    return False
                # TODO: Consider deleting the old value to avoid autostart errors
                return (
                    value_type is ValueType.REG_SZ
                    and self._get_self_path() in value
                )
        elif sys.platform == "linux":
            autostart_file: Path = self._get_linux_autostart_filepath()
            if not autostart_file.exists():
                return False
            with autostart_file.open('r', encoding="utf8") as file:
                # TODO: Consider deleting the old file to avoid autostart errors
                return self._get_self_path() in file.read()
        elif sys.platform == "darwin":
            plist_file = self._get_mac_autostart_filepath()
            if not plist_file.exists():
                return False
            with plist_file.open('r', encoding="utf8") as file:
                return str(SELF_PATH.resolve()) in file.read()

    def update_autostart(self) -> None:
        enabled = bool(self._vars["autostart"].get())
        self._settings.autostart_tray = bool(self._vars["tray"].get())
        if sys.platform == "win32":
            if enabled:
                with RegistryKey(self.AUTOSTART_KEY) as key:
                    key.set(
                        self.AUTOSTART_NAME,
                        ValueType.REG_SZ,
                        self._get_autostart_path(),
                    )
            else:
                with RegistryKey(self.AUTOSTART_KEY) as key:
                    key.delete(self.AUTOSTART_NAME, silent=True)
        elif sys.platform == "linux":
            autostart_file: Path = self._get_linux_autostart_filepath()
            if enabled:
                file_contents: str = dedent(
                    f"""
                    [Desktop Entry]
                    Type=Application
                    Name=DropStream
                    Description=Mine timed drops on Twitch
                    Exec=sh -c '{self._get_autostart_path()}'
                    """
                )
                with autostart_file.open('w', encoding="utf8") as file:
                    file.write(file_contents)
            else:
                autostart_file.unlink(missing_ok=True)
        elif sys.platform == "darwin":
            plist_file = self._get_mac_autostart_filepath()

            if enabled:
                command_parts = shlex.split(self._get_autostart_path())
                plist_data = {
                    "Label": f"com.devilxd.{self.AUTOSTART_NAME.lower()}",
                    "ProgramArguments": command_parts,
                    "RunAtLoad": True,
                }
                plist_file.parent.mkdir(parents=True, exist_ok=True)
                with plist_file.open("wb") as file:
                    plistlib.dump(plist_data, file)
            else:
                plist_file.unlink(missing_ok=True)

    def update_excluded_choices(self) -> None:
        self._exclude_entry.config(
            values=sorted(self._game_names.difference(self._settings.exclude))
        )

    def update_priority_choices(self) -> None:
        self._priority_entry.config(
            values=sorted(self._game_names.difference(self._settings.priority))
        )

    def set_games(self, games: set[Game]) -> None:
        self._game_names.update(game.name for game in games)
        self.update_excluded_choices()
        self.update_priority_choices()

    def priority_add(self) -> None:
        game_name: str = self._priority_entry.get()
        if not game_name:
            # prevent adding empty strings
            return
        self._priority_entry.clear()
        # add it preventing duplicates
        try:
            existing_idx: int = self._settings.priority.index(game_name)
        except ValueError:
            # not there, add it
            self._priority_list.insert("end", game_name)
            self._priority_list.see("end")
            self._settings.priority.append(game_name)
            self._settings.alter()
            self.update_priority_choices()
            self._manager._twitch.change_state(State.GAMES_UPDATE)
        else:
            # already there, set the selection on it
            self._priority_list.selection_set(existing_idx)
            self._priority_list.see(existing_idx)

    def _priority_idx(self) -> int | None:
        selection: tuple[int, ...] = self._priority_list.curselection()
        if not selection:
            return None
        return selection[0]

    def priority_move(self, amount: int) -> None:
        # amount > 0 = up, amount < 0 = down
        idx: int | None = self._priority_idx()
        max_idx: int = self._priority_list.size() - 1
        if (
            idx is None
            or amount == 0
            or amount > 0 and idx == 0
            or amount < 0 and idx == max_idx
        ):
            return
        insert_idx: int = idx - amount
        if insert_idx <= 0:
            insert_idx = 0
        elif insert_idx >= max_idx:
            insert_idx = max_idx

        item: str = self._priority_list.get(idx)
        self._priority_list.delete(idx)
        self._priority_list.insert(insert_idx, item)
        # reselect the item and scroll the list if needed
        self._priority_list.selection_set(insert_idx)
        self._priority_list.see(insert_idx)
        # update the underlying settings list too
        self._settings.priority.pop(idx)
        self._settings.priority.insert(insert_idx, item)
        self._settings.alter()
        self._manager._twitch.change_state(State.GAMES_UPDATE)

    def priority_delete(self) -> None:
        idx: int | None = self._priority_idx()
        if idx is None:
            return
        self._priority_list.delete(idx)
        del self._settings.priority[idx]
        self._settings.alter()
        self.update_priority_choices()
        self._manager._twitch.change_state(State.GAMES_UPDATE)

    def priority_mode(self, event: tk.Event[ttk.Combobox]) -> None:
        mode_name: str = self._vars["priority_mode"].get()
        for value, name in self.PRIORITY_MODES.items():
            if mode_name == name:
                self._settings.priority_mode = value
                self._manager._twitch.change_state(State.GAMES_UPDATE)
                break

    def sync_from_settings(self) -> None:
        """
        Refreshes the priority list, exclude list, and priority mode combobox to match
        the current `self._settings` values. Call this after settings.priority,
        settings.exclude, or settings.priority_mode were changed from outside the GUI
        (e.g. via the web dashboard), so the desktop panel doesn't show stale data.
        """
        self._priority_list.delete(0, "end")
        self._priority_list.insert("end", *self._settings.priority)
        self._exclude_list.delete(0, "end")
        self._exclude_list.insert("end", *sorted(self._settings.exclude))
        priority_mode = self._settings.priority_mode
        if priority_mode in self.PRIORITY_MODES:
            self._vars["priority_mode"].set(self.PRIORITY_MODES[priority_mode])
        self.update_priority_choices()

    def refresh_profiles(self) -> None:
        self._profiles_list.delete(0, "end")
        for name in profiles_module.list_profiles():
            self._profiles_list.insert("end", name)

    def create_profile(self) -> None:
        name = self._new_profile_var.get()
        if profiles_module.create_profile(name):
            self._new_profile_var.set("")
            self.refresh_profiles()

    def _selected_profile(self) -> str | None:
        selection = self._profiles_list.curselection()
        if not selection:
            return None
        return self._profiles_list.get(selection[0])

    def launch_profile(self) -> None:
        # starts a second, fully independent instance for that account (parallel mining)
        name = self._selected_profile()
        if name:
            profiles_module.launch_profile(name)

    def switch_profile(self) -> None:
        # starts the other account, then closes this window (quick account switch)
        name = self._selected_profile()
        if name:
            profiles_module.launch_profile(name)
            self._manager.close()

    def delete_profile(self) -> None:
        # deletes the selected profile's folder (settings/cookies/cache/stats), after
        # a confirmation dialog since this is destructive and can't be undone
        name = self._selected_profile()
        if not name:
            return
        if not messagebox.askyesno(
            _("gui", "settings", "accounts", "delete"),
            _("gui", "settings", "accounts", "delete_confirm").format(name=name),
        ):
            return
        profiles_module.delete_profile(name)
        self.refresh_profiles()

    def update_use_system_accent(self) -> None:
        self._settings.use_system_accent = bool(self._vars["use_system_accent"].get())
        self._manager.apply_theme_choice(self._settings.theme)

    def update_schedule(self) -> None:
        self._settings.schedule_enabled = bool(self._vars["schedule_enabled"].get())
        self._settings.schedule_start = self._vars["schedule_start"].get()
        self._settings.schedule_end = self._vars["schedule_end"].get()

    def auto_action(self, event: tk.Event[ttk.Combobox]) -> None:
        action_name: str = self._vars["auto_action"].get()
        for value, name in self.POWER_ACTIONS.items():
            if action_name == name:
                self._settings.auto_action = value
                break

    def update_auto_restart(self) -> None:
        self._settings.auto_restart_enabled = bool(self._vars["auto_restart_enabled"].get())
        try:
            minutes = int(self._vars["auto_restart_minutes"].get())
            if minutes < 1:
                raise ValueError
        except ValueError:
            minutes = self._settings.auto_restart_minutes
            self._vars["auto_restart_minutes"].set(str(minutes))
        else:
            self._settings.auto_restart_minutes = minutes

    def exclude_add(self) -> None:
        game_name: str = self._exclude_entry.get()
        if not game_name:
            # prevent adding empty strings
            return
        self._exclude_entry.clear()
        if game_name not in self._settings.exclude:
            self._settings.exclude.add(game_name)
            self._settings.alter()
            self.update_excluded_choices()
            self._manager._twitch.change_state(State.GAMES_UPDATE)
            # insert it alphabetically
            for i, item in enumerate(self._exclude_list.get(0, "end")):
                if game_name < item:
                    self._exclude_list.insert(i, game_name)
                    self._exclude_list.see(i)
                    break
            else:
                self._exclude_list.insert("end", game_name)
                self._exclude_list.see("end")
        else:
            # it was already there, select it
            for i, item in enumerate(self._exclude_list.get(0, "end")):
                if item == game_name:
                    existing_idx = i
                    break
            else:
                # something went horribly wrong and it's not there after all - just return
                return
            self._exclude_list.selection_set(existing_idx)
            self._exclude_list.see(existing_idx)

    def exclude_delete(self) -> None:
        selection: tuple[int, ...] = self._exclude_list.curselection()
        if not selection:
            return None
        idx: int = selection[0]
        item: str = self._exclude_list.get(idx)
        if item in self._settings.exclude:
            self._exclude_list.delete(idx)
            self._settings.exclude.discard(item)
            self._settings.alter()
            self.update_excluded_choices()
            self._manager._twitch.change_state(State.GAMES_UPDATE)


class HelpTab:
    WIDTH = 800

    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._twitch = manager._twitch
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)
        # use a frame to center the content within the tab
        center_frame = ttk.Frame(master)
        center_frame.grid(column=0, row=0)
        # use a frame for the bottom row specifically
        bottom_frame = ttk.Frame(master)
        bottom_frame.grid(column=0, row=1, sticky="nsew")
        irow = 0
        # About
        about = ttk.LabelFrame(center_frame, padding=(4, 0, 4, 4), text="About")
        about.grid(column=0, row=(irow := irow + 1), sticky="nsew", padx=2)
        about.columnconfigure(2, weight=1)
        # About - version
        ttk.Label(about, text="DropStream version: ", anchor="e").grid(
            column=0, row=0, sticky="nsew"
        )
        ttk.Label(about, text=f"v{__version__}", anchor="w").grid(column=1, row=0, sticky="nsew")
        # About - based on
        ttk.Label(
            about, text="Based on: ", anchor="e"
        ).grid(column=0, row=1, sticky="nsew")
        LinkLabel(
            about,
            link="https://github.com/DevilXD/TwitchDropsMiner",
            text="Twitch Drops Miner, originally created by DevilXD",
        ).grid(column=1, row=1, sticky="nsew")
        ttk.Label(
            about,
            text=(
                "DropStream is an unofficial, community-made update/fork of DevilXD's "
                "Twitch Drops Miner, adding a dashboard, multi-account profiles, a scheduler, "
                "a redesigned theme system and more. All core mining functionality and the "
                "original design come from DevilXD's project."
            ),
            wraplength=self.WIDTH,
            justify="left",
        ).grid(column=0, row=2, columnspan=2, sticky="nsew", pady=(2, 4))
        # About - repo link
        ttk.Label(about, text="Original repository: ", anchor="e").grid(
            column=0, row=3, sticky="nsew"
        )
        LinkLabel(
            about,
            link="https://github.com/DevilXD/TwitchDropsMiner",
            text="https://github.com/DevilXD/TwitchDropsMiner",
        ).grid(column=1, row=3, sticky="nsew")
        # About - donate
        ttk.Separator(
            about, orient="horizontal"
        ).grid(column=0, row=4, columnspan=3, sticky="nsew")
        ttk.Label(about, text="Support the original project: ", anchor="e").grid(
            column=0, row=5, sticky="nsew"
        )
        LinkLabel(
            about,
            link="https://www.buymeacoffee.com/DevilXD",
            text=(
                "DropStream is built on top of DevilXD's Twitch Drops Miner. If you find this "
                "fork useful, please consider donating to DevilXD, the author of the base "
                "project, to support their work. Thank you!"
            ),
            wraplength=self.WIDTH,
        ).grid(column=1, row=3, sticky="nsew")
        # Useful links
        links = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "help", "links", "name")
        )
        links.grid(column=0, row=(irow := irow + 1), sticky="nsew", padx=2)
        LinkLabel(
            links,
            link="https://www.twitch.tv/drops/inventory",
            text=_("gui", "help", "links", "inventory"),
        ).grid(column=0, row=0, sticky="nsew")
        LinkLabel(
            links,
            link="https://www.twitch.tv/drops/campaigns",
            text=_("gui", "help", "links", "campaigns"),
        ).grid(column=0, row=1, sticky="nsew")
        # How It Works
        howitworks = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "help", "how_it_works")
        )
        howitworks.grid(column=0, row=(irow := irow + 1), sticky="nsew", padx=2)
        ttk.Label(
            howitworks, text=_("gui", "help", "how_it_works_text"), wraplength=self.WIDTH
        ).grid(sticky="nsew")
        getstarted = ttk.LabelFrame(
            center_frame, padding=(4, 0, 4, 4), text=_("gui", "help", "getting_started")
        )
        getstarted.grid(column=0, row=(irow := irow + 1), sticky="nsew", padx=2)
        ttk.Label(
            getstarted, text=_("gui", "help", "getting_started_text"), wraplength=self.WIDTH
        ).grid(sticky="nsew")

        # Invalidate button
        invalidate_frame = ttk.Frame(bottom_frame)
        bottom_frame.columnconfigure(0, weight=1)  # center within the column
        invalidate_frame.grid(column=0, row=0, sticky="nse")
        ttk.Label(
            invalidate_frame, text=_("gui", "help", "invalidate", "text")
        ).grid(column=0, row=0)
        self._invalidate_button: ttk.Button = ttk.Button(
            invalidate_frame,
            text=_("gui", "help", "invalidate", "button"),
            command=self.invalidate_token,
            state="disabled",
        )
        self._invalidate_button.grid(column=1, row=0)

    def invalidate_token(self) -> None:
        # sync to async bridge
        asyncio.create_task(task_wrapper(self._invalidate_token)())

    async def _invalidate_token(self) -> None:
        auth_state = await self._twitch.get_auth()
        async with self._twitch.request(
            "POST",
            "https://id.twitch.tv/oauth2/revoke",
            data={
                "client_id": self._twitch._client_type.CLIENT_ID,
                "token": auth_state.access_token,
            }
        ) as response:
            if response.status == 200:
                auth_state.invalidate(delete_cookies=True)
            else:
                logger.error(f"Failed to invalidate the auth token: {response.status}")
        self._twitch.change_state(State.RESTART)


class RemoteAccessTab:
    """
    Lets the user turn the built-in web dashboard on/off, pick between a read-only "view"
    link and a "view and control" link, optionally protect control actions with a password,
    and copy the resulting share link. Kept as its own tab (rather than buried in Settings)
    since it's meant to be glanced at and copied from, not configured once and forgotten.
    """

    def __init__(self, manager: GUIManager, master: ttk.Widget):
        self._manager = manager
        self._twitch = manager._twitch
        self._settings = manager._twitch.settings
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)
        center = ttk.Frame(master)
        center.grid(column=0, row=0)

        master_var = tk.IntVar(master, int(self._settings.web_server_enabled))
        control_var = tk.IntVar(master, int(self._settings.web_server_allow_control))
        port_var = tk.StringVar(master, str(self._settings.web_server_port))
        show_viewers_var = tk.IntVar(master, int(self._settings.web_server_show_viewers))
        link_var = tk.StringVar(master, "")
        self._vars: dict[str, tk.Variable] = {
            "enabled": master_var,
            "control": control_var,
            "port": port_var,
            "show_viewers": show_viewers_var,
            "link": link_var,
        }

        irow = 0
        ttk.Label(
            center,
            text=_("gui", "remote", "info"),
            foreground="goldenrod",
            wraplength=520,
            justify="left",
        ).grid(column=0, row=(irow := irow + 1), columnspan=2, sticky="w", pady=(0, 10))

        # Enable
        ttk.Label(center, text=_("gui", "remote", "enabled")).grid(
            column=0, row=(irow := irow + 1), sticky="e"
        )
        ttk.Checkbutton(
            center, variable=master_var, command=self.update_server
        ).grid(column=1, row=irow, sticky="w")

        # Port
        ttk.Label(center, text=_("gui", "remote", "port")).grid(
            column=0, row=(irow := irow + 1), sticky="e"
        )
        port_entry = ttk.Entry(center, width=8, textvariable=port_var)
        port_entry.grid(column=1, row=irow, sticky="w")
        port_entry.bind("<FocusOut>", lambda e: self.update_server())

        # Access mode: view only, or view and control
        ttk.Label(center, text=_("gui", "remote", "mode_label")).grid(
            column=0, row=(irow := irow + 1), sticky="e"
        )
        mode_frame = ttk.Frame(center)
        mode_frame.grid(column=1, row=irow, sticky="w")
        ttk.Radiobutton(
            mode_frame, text=_("gui", "remote", "mode_view"),
            variable=control_var, value=0, command=self.update_server,
        ).grid(column=0, row=0, sticky="w")
        ttk.Radiobutton(
            mode_frame, text=_("gui", "remote", "mode_control"),
            variable=control_var, value=1, command=self.update_server,
        ).grid(column=0, row=1, sticky="w")

        # Optional password, only meaningful (and only enabled) in control mode
        ttk.Label(center, text=_("gui", "remote", "password_label")).grid(
            column=0, row=(irow := irow + 1), sticky="e"
        )
        self._password_entry = PlaceholderEntry(
            center, placeholder=_("gui", "remote", "password_placeholder"),
            width=26, show="*",
        )
        if self._settings.web_server_password:
            self._password_entry.replace(self._settings.web_server_password)
        self._password_entry.grid(column=1, row=irow, sticky="w")
        self._password_entry.bind("<FocusOut>", lambda e: self.update_server())

        # Also show the live viewer count on the dashboard page itself, not just here
        ttk.Label(center, text=_("gui", "remote", "show_viewers_label")).grid(
            column=0, row=(irow := irow + 1), sticky="e"
        )
        ttk.Checkbutton(
            center, variable=show_viewers_var, command=self.update_server
        ).grid(column=1, row=irow, sticky="w")

        # Local share link
        ttk.Button(
            center, text=_("gui", "remote", "new_link"), command=self.regenerate_token
        ).grid(column=0, row=(irow := irow + 1), columnspan=2, pady=(10, 0))
        ttk.Label(center, text=_("gui", "remote", "link")).grid(
            column=0, row=(irow := irow + 1), columnspan=2, sticky="w", pady=(8, 0)
        )
        link_row = ttk.Frame(center)
        link_row.grid(column=0, row=(irow := irow + 1), columnspan=2, sticky="ew")
        link_row.columnconfigure(0, weight=1)
        link_entry = ttk.Entry(link_row, textvariable=link_var, state="readonly")
        link_entry.grid(column=0, row=0, sticky="ew")
        ttk.Button(
            link_row, text=_("gui", "remote", "copy_link"), command=self.copy_link, width=3
        ).grid(column=1, row=0, padx=(4, 0))
        ttk.Button(
            link_row, text=_("gui", "remote", "open_link"), command=self.open_link, width=3
        ).grid(column=2, row=0, padx=(4, 0))

        self._link_row = irow + 1

        self._update_link_display()
        self._update_password_state()

        # Live count of browsers currently viewing the web dashboard (moved here from the
        # web page itself - it's more useful to the streamer at the source than to whoever
        # is looking at the dashboard). Only meaningful while the server is running.
        self._viewer_label = ttk.Label(center, text="")
        self._viewer_label.grid(
            column=0, row=self._link_row + 2, columnspan=2, pady=(10, 0)
        )
        self._poll_viewers()

    def _poll_viewers(self) -> None:
        web_server = getattr(self._twitch, "web_server", None)
        if web_server is not None and self._settings.web_server_enabled and web_server.running:
            count = web_server._viewer_count()
            if count == 0:
                text = _("gui", "remote", "viewers_none")
            elif count == 1:
                text = _("gui", "remote", "viewers_one")
            else:
                text = _("gui", "remote", "viewers_many").format(count=count)
            self._viewer_label.configure(text=text)
        else:
            self._viewer_label.configure(text="")
        self._manager._root.after(self._eco_interval(), self._poll_viewers)

    def _eco_interval(self) -> int:
        if self._manager._minimized and self._twitch.settings.low_power_tray_mode:
            return 60_000
        return 5_000

    def _update_password_state(self) -> None:
        state = "normal" if self._vars["control"].get() else "disabled"
        self._password_entry.configure(state=state)

    def _update_link_display(self) -> None:
        from webserver import local_ip, new_token
        if not self._settings.web_server_enabled:
            self._vars["link"].set(_("gui", "remote", "link_disabled"))
            return
        if not self._settings.web_server_token:
            # previously the token was only ever generated inside the async server-start
            # coroutine, which runs *after* this method - so the very first link shown
            # after enabling was built with an empty token and was permanently broken.
            # Generate it here too, synchronously, so the displayed link is always correct
            # the instant the dashboard is turned on.
            self._settings.web_server_token = new_token()
        token = self._settings.web_server_token
        port = self._settings.web_server_port
        self._vars["link"].set(f"http://{local_ip()}:{port}/{token}")

    def update_server(self) -> None:
        self._settings.web_server_enabled = bool(self._vars["enabled"].get())
        self._settings.web_server_allow_control = bool(self._vars["control"].get())
        self._settings.web_server_password = self._password_entry.get()
        self._settings.web_server_show_viewers = bool(self._vars["show_viewers"].get())
        try:
            port = int(self._vars["port"].get())
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            port = self._settings.web_server_port
            self._vars["port"].set(str(port))
        else:
            self._settings.web_server_port = port
        self._update_password_state()
        self._update_link_display()
        self._twitch.apply_web_server_settings()

    def regenerate_token(self) -> None:
        from webserver import new_token
        self._settings.web_server_token = new_token()
        self._update_link_display()
        if self._settings.web_server_enabled:
            self._twitch.apply_web_server_settings()

    def copy_link(self) -> None:
        link = self._vars["link"].get()
        if not link.startswith("http"):
            return
        self._manager._root.clipboard_clear()
        self._manager._root.clipboard_append(link)

    def open_link(self) -> None:
        link = self._vars["link"].get()
        if not link.startswith("http"):
            return
        webbrowser.open_new_tab(link)


##########################################
# GUI DEFINITION END / GUI MANAGER START #
##########################################


class GUIManager:
    def __init__(self, twitch: Twitch):
        self._twitch: Twitch = twitch
        self._poll_task: asyncio.Task[None] | None = None
        self._minimized: bool = False
        self._close_requested = asyncio.Event()
        self._root = root = Tk(className=WINDOW_TITLE)
        # withdraw immediately to prevent the window from flashing
        self._root.withdraw()
        # root.resizable(False, True)
        set_root_icon(root, resource_path("icons/pickaxe.ico"))
        title = WINDOW_TITLE if ACTIVE_PROFILE == "default" else f"{WINDOW_TITLE} — {ACTIVE_PROFILE}"
        root.title(title)  # window title, shows the active account profile if not default
        root.bind_all("<KeyPress-Escape>", self.unfocus)  # pressing ESC unfocuses selection
        # Image cache for displaying images
        self._cache = ImageCache(self)

        # style adjustements
        self._style = style = ttk.Style(root)
        # theme
        theme = ''
        # theme = style.theme_names()[6]
        # style.theme_use(theme)
        # fix treeview's background color from tags not working (also see '_fixed_map')
        style.map(
            "Treeview",
            foreground=self._fixed_map("foreground"),
            background=self._fixed_map("background"),
        )
        # add padding to the tab names
        style.configure("TNotebook.Tab", padding=[8, 4])
        # Skip these for classic theme or macOS
        if theme != "classic" and sys.platform != "darwin":
            # remove Notebook.focus from the Notebook.Tab layout tree to avoid an ugly dotted line
            # on tab selection. We fold the Notebook.focus children into Notebook.padding children.
            # ttk's style.layout() returns a deeply nested, dynamically-shaped structure
            # (not the fixed _Layout TypedDict shape) - cast to Any here since the
            # "children" key access below is correct at runtime but too dynamic to type.
            original: Any = style.layout("TNotebook.Tab")
            sublayout = original[0][1]["children"][0][1]
            sublayout["children"] = sublayout["children"][0][1]["children"]
            style.layout("TNotebook.Tab", original)
            # remove Checkbutton.focus dotted line from checkbuttons
            style.configure("TCheckbutton", padding=0)
            original = style.layout("TCheckbutton")
            sublayout = original[0][1]["children"]
            sublayout[1] = sublayout[1][1]["children"][0]
            del original[0][1]["children"][1]
            style.layout("TCheckbutton", original)
        # label style - green, yellow and red text
        style.configure("green.TLabel", foreground="green")
        style.configure("yellow.TLabel", foreground="goldenrod")
        style.configure("red.TLabel", foreground="red")
        # fonts storage
        self._fonts: dict[str, Font] = {}
        # end of style changes

        root_frame = ttk.Frame(root, padding=8)
        root_frame.grid(column=0, row=0, sticky="nsew")
        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)
        # Loading overlay: covers the whole window, used when restoring from a low-power
        # tray minimize (rebuilding the Inventory tab/images takes a moment) so the user
        # sees real progress instead of a frozen-looking window
        self._loading_overlay = ttk.Frame(root)
        self._loading_label = ttk.Label(
            self._loading_overlay, text="", font=("TkDefaultFont", 11), anchor="center"
        )
        self._loading_label.pack(expand=True, pady=(0, 8))
        self._loading_bar = ttk.Progressbar(
            self._loading_overlay, mode="indeterminate", length=220
        )
        self._loading_bar.pack(pady=(0, 0))
        self._inventory_dirty = False
        self._inventory_reload_task: asyncio.Task[None] | None = None
        # Notebook
        self.tabs = Notebook(self, root_frame)
        # Tray icon - place after notebook so it draws on top of the tabs space
        self.tray = TrayIcon(self, root_frame)
        # Main ("Details") tab widgets are built first (Dashboard needs live progress vars),
        # but the tab itself is added to the notebook after Dashboard so Dashboard shows first.
        main_frame = ttk.Frame(root_frame, padding=8)
        self.status = StatusBar(self, main_frame)
        self.websockets = WebsocketStatus(self, main_frame)
        self.login = LoginForm(self, main_frame)
        self.progress = CampaignProgress(self, main_frame)
        self.output = ConsoleOutput(self, main_frame)
        self.channels = ChannelList(self, main_frame)
        # Dashboard tab (now the first/landing tab): stats + a live "currently mining" summary
        dash_frame = ttk.Frame(root_frame, padding=8)
        self.dashboard = DashboardTab(self, dash_frame)
        self.tabs.add_tab(dash_frame, name=_("gui", "tabs", "dashboard"), icon_key="dashboard")
        self.tabs.add_tab(main_frame, name=_("gui", "tabs", "main"), icon_key="details")
        # Games tab: priority mode, priority list, exclude list
        games_frame = ttk.Frame(root_frame, padding=8)
        # Settings tab (built together with the games tab, they share state)
        settings_frame = ttk.Frame(root_frame, padding=8)
        self.settings = SettingsPanel(self, settings_frame, games_frame)
        self.tabs.add_tab(games_frame, name=_("gui", "tabs", "games"), icon_key="games")
        # Inventory tab (optional - can be hidden in favor of the web dashboard's own
        # Inventory view, see settings.show_inventory_tab)
        self.inv: InventoryOverview | None = None
        if self._twitch.settings.show_inventory_tab:
            inv_frame = ttk.Frame(root_frame, padding=8)
            self.inv = InventoryOverview(self, inv_frame)
            self.tabs.add_tab(
                inv_frame, name=_("gui", "tabs", "inventory"), icon_key="inventory",
                fill_height=True,
            )

        # Remote access tab: optional web dashboard, view or view+control, share link
        remote_frame = ttk.Frame(root_frame, padding=8)
        self.remote = RemoteAccessTab(self, remote_frame)
        self.tabs.add_tab(remote_frame, name=_("gui", "tabs", "remote"), icon_key="remote")
        self.tabs.add_tab(settings_frame, name=_("gui", "tabs", "settings"), icon_key="settings")
        # Help tab
        help_frame = ttk.Frame(root_frame, padding=8)
        self.help = HelpTab(self, help_frame)
        self.tabs.add_tab(help_frame, name=_("gui", "tabs", "help"), icon_key="help")
        # clamp minimum window size (update geometry first)
        root.update_idletasks()
        # previously this pinned minsize to the full natural size of the widest tab, which
        # meant the window couldn't be shrunk below that at all. Tab content isn't reflowed
        # below its natural size, so it can get visually clipped once you shrink past it -
        # but the tab bar itself (with mouse-wheel/Ctrl+PageUp/PageDown cycling, see Notebook
        # above) stays fully usable, which is what actually matters at small sizes.
        min_w = min(420, root.winfo_reqwidth())
        min_h = min(320, root.winfo_reqheight())
        root.minsize(width=min_w, height=min_h)
        # register logging handler
        self._handler = _TKOutputHandler(self)
        self._handler.setFormatter(OUTPUT_FORMATTER)
        logger = logging.getLogger("TwitchDrops")
        logger.addHandler(self._handler)
        if (logging_level := logger.getEffectiveLevel()) < logging.ERROR:
            self.print(f"Logging level: {logging.getLevelName(logging_level)}")
        # gracefully handle Windows shutdown closing the application
        if sys.platform == "win32":
            # NOTE: this root.update() is required for the below to work - don't remove
            root.update()
            self._message_map = {
                # window close request
                win32con.WM_CLOSE: self.close,
                # shutdown request
                win32con.WM_QUERYENDSESSION: self.close,
            }
            # This hooks up the wnd_proc function as the message processor for the root window.
            self.old_wnd_proc = win32gui.SetWindowLong(
                self._handle, win32con.GWL_WNDPROC, self.wnd_proc
            )
            # This ensures all of this works when the application is withdrawn or iconified
            ctypes.windll.user32.ShutdownBlockReasonCreate(
                self._handle, ctypes.c_wchar_p(_("gui", "status", "exiting"))
            )
            # DEV NOTE: use this to remove the reason in the future
            # ctypes.windll.user32.ShutdownBlockReasonDestroy(self._handle)
        else:
            # use old-style window closing protocol for non-windows platforms
            root.protocol("WM_DELETE_WINDOW", self.close)
            root.protocol("WM_DESTROY_WINDOW", self.close)
        # Save current theme and apply palette after widgets are created
        try:
            self._orig_theme_name = self._style.theme_use()
        except Exception:
            self._orig_theme_name = ''
        self.apply_theme_choice(self._twitch.settings.theme)
        # if the theme tracks the OS setting, periodically re-check it (cheap, no-op if unchanged)
        self._theme_auto_check()
        # stay hidden in tray if needed, otherwise show the window when everything's ready
        if self._twitch.settings.tray and sys.platform != "darwin":
            # NOTE: this starts the tray icon thread
            self._root.after_idle(self.tray.minimize)
        else:
            self._root.after_idle(self._root.deiconify)

    # https://stackoverflow.com/questions/56329342/tkinter-treeview-background-tag-not-working
    def _fixed_map(self, option):
        # Fix for setting text colour for Tkinter 8.6.9
        # From: https://core.tcl.tk/tk/info/509cafafae
        #
        # Returns the style map for 'option' with any styles starting with
        # ('!disabled', '!selected', ...) filtered out.

        # style.map() returns an empty list for missing options, so this
        # should be future-safe.
        return [
            elm for elm in self._style.map("Treeview", query_opt=option)
            if elm[:2] != ("!disabled", "!selected")
        ]

    def wnd_proc(self, hwnd, msg, w_param, l_param):
        """
        This function serves as a message processor for all messages sent
        to the application by Windows.
        """
        if msg == win32con.WM_DESTROY:
            win32api.SetWindowLong(self._handle, win32con.GWL_WNDPROC, self.old_wnd_proc)
        if msg in self._message_map:
            return self._message_map[msg](w_param, l_param)
        return win32gui.CallWindowProc(self.old_wnd_proc, hwnd, msg, w_param, l_param)

    @cached_property
    def _handle(self) -> int:
        return int(self._root.wm_frame(), 16)

    @property
    def running(self) -> bool:
        return self._poll_task is not None

    @property
    def close_requested(self) -> bool:
        return self._close_requested.is_set()

    async def wait_until_closed(self):
        # wait until the user closes the window
        await self._close_requested.wait()

    async def coro_unless_closed(self, coro: abc.Awaitable[_T]) -> _T:
        # In Python 3.11, we need to explicitly wrap awaitables
        tasks = [asyncio.ensure_future(coro), asyncio.ensure_future(self._close_requested.wait())]
        done: set[asyncio.Task[Any]]
        pending: set[asyncio.Task[Any]]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if self._close_requested.is_set():
            raise ExitRequest()
        return await next(iter(done))

    def prevent_close(self):
        self._close_requested.clear()

    def start(self):
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll())
        # self.progress.start_timer()

    def stop(self):
        self.progress.stop_timer()
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll(self):
        """
        This runs the Tkinter event loop via asyncio instead of calling mainloop.

        Uses an adaptive sleep instead of a fixed 0.05s tick: a fixed 20Hz wakeup runs
        forever even while the window is idle or minimized, which burns CPU (and, on a
        laptop, battery) for no visible benefit. Instead we poll fast only right after
        real Tk activity (so animations/typing stay smooth), and back off up to a much
        coarser interval once nothing has happened for a while - falling straight back
        to fast polling the moment a new event shows up.

        Uses TKINTER_DONT_WAIT to prevent Tcl/Tk from hanging inside native
        system calls (e.g. X11/Wayland input contexts) during heavy UI redraws.
        """
        do_one_event = self._root.dooneevent
        # TKINTER_DONT_WAIT (1 << 1) tells Tcl to return immediately 
        # if no events are ready in the queue.
        DONT_WAIT = 1 << 1

        FAST_INTERVAL = 0.05   # matches the previous fixed behavior while there's activity
        IDLE_INTERVAL = 0.35   # ~3x fewer wakeups/sec once the UI has been quiet for a bit
        MINIMIZED_INTERVAL = 1.0  # window is hidden in the tray, no need to poll fast at all
        IDLE_AFTER = 20        # consecutive empty ticks before backing off (~1s of quiet)
        idle_streak = 0

        while True:
            try:
                # Drain pending Tk events non-blockingly
                had_events = False
                while do_one_event(DONT_WAIT):
                    had_events = True
            except tk.TclError:
                # Root window was destroyed
                break
            if had_events:
                idle_streak = 0
            else:
                idle_streak += 1
            if not self._minimized:
                interval = IDLE_INTERVAL if idle_streak >= IDLE_AFTER else FAST_INTERVAL
            elif self._twitch.settings.low_power_tray_mode:
                interval = MINIMIZED_INTERVAL
            else:
                interval = IDLE_INTERVAL if idle_streak >= IDLE_AFTER else FAST_INTERVAL
            await asyncio.sleep(interval)

        self._poll_task = None

    def show_loading(self, message: str) -> None:
        self._loading_label.configure(text=message)
        self._loading_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._loading_overlay.lift()
        self._loading_bar.start(12)

    def hide_loading(self) -> None:
        self._loading_bar.stop()
        self._loading_overlay.place_forget()

    async def reload_inventory_after_low_power(self) -> None:
        # rebuilds the Inventory tab (and re-fetches/re-decodes the images that were
        # dropped from RAM) after a low-power tray minimize. Real, awaited work behind a
        # loading screen - not a fake delay - since this can take a moment.
        if not self._inventory_dirty or self.inv is None:
            return
        self.show_loading(_("gui", "inventory", "reloading"))
        try:
            self.inv.clear()
            tasks = [
                asyncio.create_task(self.inv.add_campaign(campaign))
                for campaign in self._twitch.inventory
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._inventory_dirty = False
            self._inventory_reload_task = None
            self.hide_loading()

    def ensure_inventory_reloaded(self) -> None:
        # call before showing the Inventory tab, or right after restoring from the tray:
        # kicks off the rebuild (if needed) and makes sure it only runs once at a time
        if self._inventory_dirty and self._inventory_reload_task is None:
            self._inventory_reload_task = asyncio.create_task(
                self.reload_inventory_after_low_power()
            )

    def close(self, *args) -> int:
        """
        Requests the GUI application to close.
        The window itself will be closed in the closing sequence later.
        """
        self._close_requested.set()
        # notify client we're supposed to close
        self._twitch.close()
        return 0

    def close_window(self):
        """
        Closes the window. Invalidates the logger.
        """
        self.tray.stop()
        logging.getLogger("TwitchDrops").removeHandler(self._handler)
        self._root.destroy()

    def unfocus(self, event):
        # support pressing ESC to unfocus
        self._root.focus_set()
        self.channels.clear_selection()
        self.settings.clear_selection()

    # these are here to interface with underlaying GUI components
    def save(self, *, force: bool = False) -> None:
        self._cache.save(force=force)

    def grab_attention(self, *, sound: bool = True):
        self.tray.restore()
        self._root.focus_set()
        if sound:
            self._root.bell()

    def set_games(self, games: set[Game]) -> None:
        self.settings.set_games(games)

    def display_drop(
        self, drop: TimedDrop, *, countdown: bool = True, subone: bool = False
    ) -> None:
        self.progress.display(drop, countdown=countdown, subone=subone)  # main tab
        # inventory overview is updated from within drops themselves via change events
        self.tray.update_title(drop)  # tray

    def clear_drop(self):
        self.progress.display(None)
        self.tray.update_title(None)

    def print(self, message: str):
        # print to our custom output
        self.output.print(message)

    def _set_title_bar_color(self, color: int) -> None:
        """
        Set the Windows title bar color to match the theme.
        Only works on Windows with DWM enabled.

        Args:
            color: COLORREF value (0x00BBGGRR format).
        """
        if sys.platform != "win32":
            return
        # DWMWA_CAPTION_COLOR = 35
        DWMWA_CAPTION_COLOR = 35
        hwnd = self._root.winfo_id()
        frame_hwnd = ctypes.windll.user32.GetParent(hwnd)
        color_value = ctypes.c_int(color)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            frame_hwnd,
            DWMWA_CAPTION_COLOR,
            ctypes.byref(color_value),
            ctypes.sizeof(ctypes.c_int),
        )

    @staticmethod
    def _hex_to_colorref(hex_color: str) -> int:
        # Windows COLORREF is 0x00BBGGRR, the reverse byte order of a "#RRGGBB" hex string
        hex_color = hex_color.lstrip("#")
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
        return (b << 16) | (g << 8) | r

    def apply_theme_choice(self, theme: str) -> None:
        # resolves a settings.theme value ("auto", "modern_dark", ...) and applies it
        dark, style = resolve_theme(theme)
        self._current_theme_dark = dark
        self.apply_theme(dark, style)

    def _theme_auto_check(self) -> None:
        # every 60s, re-resolve the theme in case it's set to follow the OS and the OS changed.
        # Cheap either way (a dict lookup unless the OS setting actually changed), but only
        # worth checking at all when a theme is actually set to "auto".
        theme = self._twitch.settings.theme
        if theme.endswith("auto"):
            dark, _style = resolve_theme(theme)
            if dark != getattr(self, "_current_theme_dark", None):
                self.apply_theme_choice(theme)
        self._root.after(60_000, self._theme_auto_check)

    def apply_theme(self, dark: bool, style: str = "classic") -> None:
        """
        Apply the selected palette (classic/modern, light/dark) to ttk styles
        and Tk widgets in a minimal, non-invasive way.
        """
        palette = PALETTES[style]["dark" if dark else "light"]
        bg = palette["bg"]
        fg = palette["fg"]
        sel_bg = palette["sel_bg"]
        sel_fg = palette["sel_fg"]
        link = palette["link"]
        surface = palette["surface"]
        header = palette["header"]
        fieldbg = palette["fieldbg"]
        border = palette["border"]
        muted = palette["muted"]
        accent = palette["accent"]
        if self._twitch.settings.use_system_accent:
            from theme import system_accent_color
            detected = system_accent_color()
            if detected is not None:
                accent = detected
        if dark:
            # Switch to a configurable ttk theme for better color control
            if sys.platform != "darwin" and self._style.theme_use() != "clam":
                self._style.theme_use("clam")
        elif (
            style == "modern"
            and sys.platform == "win32"
            and not self._twitch.settings.use_system_accent
        ):
            # "Modern Light" uses Windows' own native Fluent-rendered controls (buttons,
            # checkboxes, comboboxes) via the "vista" ttk theme, instead of hand-drawn ones -
            # this is the most authentic possible Windows 11 look. Native rendering ignores
            # our custom colors though, so skip it whenever the accent color needs to show
            # through (progress bar, focus outlines, etc.) and use "clam" instead.
            if "vista" in self._style.theme_names() and self._style.theme_use() != "vista":
                self._style.theme_use("vista")
        else:
            # "clam" lets every widget (progress bar, focus outlines, tabs...) actually
            # reflect the accent color - needed for classic light and for any accent-aware mode
            if sys.platform != "darwin" and self._style.theme_use() != "clam":
                self._style.theme_use("clam")

        # Setting theme for macOS
        if sys.platform == "darwin":
            app = AppKit.NSApplication.sharedApplication()
            if dark:
                appearance = AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua)
            else:
                appearance = AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameAqua)
            app.setAppearance_(appearance)

        s = self._style
        # Fonts
        default_font = nametofont("TkDefaultFont")
        self._fonts["default"] = default_font
        if not hasattr(self, "_orig_font_family"):
            self._orig_font_family = default_font.cget("family")
            self._orig_font_size = default_font.cget("size")
        if style == "modern" and sys.platform == "win32":
            # Segoe UI Variable is Windows 11's system font; fall back to Segoe UI (Win10/11)
            available = set(tkfont.families())
            family = next(
                (f for f in ("Segoe UI Variable Display", "Segoe UI Variable", "Segoe UI")
                 if f in available),
                self._orig_font_family,
            )
            default_font.config(family=family, size=self._orig_font_size + 1)
        else:
            default_font.config(family=self._orig_font_family, size=self._orig_font_size)
        # Font - button style with a larger font
        self._fonts["large"] = default_font.copy()
        self._fonts["large"].config(size=10)
        s.configure("Large.TButton", font=self._fonts["large"])
        # Font - button style for sorting arrows
        self._fonts["arrow"] = default_font.copy()
        self._fonts["arrow"].config(size=16)
        s.configure("Arrow.TButton", font=self._fonts["arrow"])
        s.configure("Arrow.TButton", padding=-4)  # reduce padding on arrow buttons
        # Font - label style that mimics links
        self._fonts["underlined"] = default_font.copy()
        self._fonts["underlined"].config(underline=True)
        s.configure("Link.TLabel", font=self._fonts["underlined"], foreground=link)
        # Font - label style with a monospace font
        self._fonts["monospaced"] = default_font.copy()
        self._fonts["monospaced"].config(family="Courier New", size=10)
        s.configure("MS.TLabel", font=self._fonts["monospaced"])

        # Base containers and labels
        s.configure("TFrame", background=bg, foreground=fg)
        # Root window background: any window area not covered by a themed ttk widget
        # (e.g. leftover space below a tab's content when the window is resized taller
        # than that tab needs) falls back to the raw Tk window background, which is a
        # plain white/gray by default on Windows regardless of the ttk theme in use.
        self._root.configure(bg=bg)
        s.configure("TLabel", background=bg, foreground=fg)
        s.configure("TLabelframe", background=bg, foreground=fg)
        s.configure("TLabelframe.Label", background=bg, foreground=fg)
        if style == "modern":
            # card-like grouping: a slightly raised surface tone + generous padding + accent border
            s.configure(
                "TLabelframe",
                background=surface,
                bordercolor=border,
                relief="flat",
                padding=(10, 8),
            )
            s.configure("TLabelframe.Label", background=surface, foreground=accent)
        # Buttons and checks
        if style == "modern":
            # flat, borderless buttons with an accent-tinted hover, closer to Windows 11 controls
            s.configure(
                "TButton",
                background=surface,
                foreground=fg,
                bordercolor=surface,
                relief="flat",
                padding=(10, 6),
            )
            s.map(
                "TButton",
                background=[("active", header), ("pressed", header)],
                bordercolor=[("focus", accent), ("!focus", surface)],
                foreground=[("disabled", muted)],
            )
        else:
            s.configure("TButton", background=surface, foreground=fg, bordercolor=border)
            s.map(
                "TButton",
                background=[("active", header), ("pressed", border)],
                bordercolor=[("focus", accent), ("!focus", border)],
                foreground=[("disabled", muted)],
            )
        s.configure(
            "TCheckbutton",
            background=bg,
            foreground=fg,
            focuscolor=bg,
            bordercolor=border,
        )
        s.map(
            "TCheckbutton",
            # Remove hover visuals by mapping active/pressed to the base background
            background=[
                ("active", bg),
                ("pressed", bg),
            ],
            foreground=[("disabled", muted)],
            indicatorcolor=[
                ("selected", accent),
                ("!selected", border),
            ],
        )
        # Notebook
        s.configure("TNotebook", background=bg, bordercolor=border)
        tab_font = self._fonts.get("tab")
        if tab_font is None:
            tab_font = default_font.copy()
            self._fonts["tab"] = tab_font
        if style == "modern":
            # Windows 11-ish: no visible tab border, more breathing room, medium-weight label
            tab_font.config(size=default_font.cget("size") + 1, weight="normal")
            s.configure(
                "TNotebook.Tab",
                background=bg,
                foreground=fg,
                bordercolor=bg,
                padding=(16, 8),
                font=tab_font,
            )
            s.map(
                "TNotebook.Tab",
                background=[("selected", header), ("active", header)],
                foreground=[("selected", accent), ("disabled", muted)],
            )
        else:
            tab_font.config(size=default_font.cget("size"), weight="normal")
            s.configure(
                "TNotebook.Tab",
                background=surface,
                foreground=fg,
                bordercolor=border,
                padding=(8, 4),
                font=tab_font,
            )
            s.map(
                "TNotebook.Tab",
                background=[("selected", header), ("active", header)],
                foreground=[("selected", accent), ("disabled", muted)],
            )
        # Tab bar icons: small flat glyphs instead of plain text, recolored to match the theme
        try:
            self.tabs.set_icons(build_tab_icons(fg))
        except Exception:
            logger.debug("Failed to build tab icons", exc_info=True)
        # Dashboard's tk.Canvas charts aren't ttk widgets, so sync their colors manually
        if hasattr(self, "dashboard"):
            self.dashboard.apply_theme(surface, fg)
        # Entries/Combos
        s.configure(
            "TEntry", fieldbackground=fieldbg, background=fieldbg, foreground=fg, insertcolor=fg
        )
        s.configure(
            "TCombobox", fieldbackground=fieldbg, background=fieldbg, foreground=fg, arrowcolor=fg
        )
        # Ensure readability for readonly comboboxes (Language, Priority mode)
        s.map(
            "TCombobox",
            foreground=[("readonly", fg), ("disabled", muted)],
            fieldbackground=[("readonly", fieldbg)],
            background=[("readonly", fieldbg)],
            arrowcolor=[("readonly", fg)],
        )
        if style == "modern":
            # accent-colored focus outline, Windows 11-style, instead of the plain system border
            s.map(
                "TEntry",
                bordercolor=[("focus", accent), ("!focus", border)],
                lightcolor=[("focus", accent)],
                foreground=[("disabled", muted)],
            )
            s.map(
                "TCombobox",
                bordercolor=[("focus", accent), ("!focus", border)],
            )
        else:
            s.map(
                "TEntry",
                bordercolor=[("focus", accent), ("!focus", border)],
                foreground=[("disabled", muted)],
            )
            s.map("TCombobox", bordercolor=[("focus", accent), ("!focus", border)])
        # Treeview
        s.configure(
            "Treeview",
            background=surface,
            fieldbackground=surface,
            foreground=fg,
            bordercolor=border,
            rowheight=26 if style == "modern" else 20,
        )
        s.map(
            "Treeview",
            background=[("selected", sel_bg)],
            foreground=[("selected", sel_fg)],
        )
        s.configure("Treeview.Heading", background=header, foreground=fg, bordercolor=border)
        # Progressbar
        s.configure(
            "TProgressbar",
            background=accent,
            troughcolor=surface,
            thickness=8 if style == "modern" else 12,
        )
        # Scrollbars
        s.configure(
            "Vertical.TScrollbar",
            background=surface,
            troughcolor=bg,
            arrowcolor=fg,
            bordercolor=border,
        )
        s.configure(
            "Horizontal.TScrollbar",
            background=surface,
            troughcolor=bg,
            arrowcolor=fg,
            bordercolor=border,
        )

        # Pure Tk widgets
        # Console text
        self.output.configure_theme(bg=surface, fg=fg, sel_bg=sel_bg, sel_fg=sel_fg)
        # Listboxes
        self.settings._priority_list.configure_theme(
            bg=surface, fg=fg, sel_bg=sel_bg, sel_fg=sel_fg
        )
        self.settings._exclude_list.configure_theme(
            bg=surface, fg=fg, sel_bg=sel_bg, sel_fg=sel_fg
        )
        # Inventory canvas
        if self.inv is not None:
            self.inv.configure_theme(bg=bg)
        # Notebook tab wrapper canvases (scroll-on-overflow background, see Notebook.add_tab)
        self.tabs.configure_theme(bg=bg)

        # Tk option database for selection/popup list readability (affects Tk-backed widgets)
        # Global selection colors and listbox defaults (covers Combobox dropdown)
        self._root.option_add("*selectBackground", sel_bg)
        self._root.option_add("*selectForeground", sel_fg)
        # Combobox dropdown list (Tk Listbox)
        for key in (
            "*TCombobox*Listbox.background",
            "*TCombobox*Listbox.Background",
            "*Listbox.background",
        ):
            self._root.option_add(key, surface)
        for key in (
            "*TCombobox*Listbox.foreground",
            "*TCombobox*Listbox.Foreground",
            "*Listbox.foreground",
        ):
            self._root.option_add(key, fg)
        for key in (
            "*TCombobox*Listbox.selectBackground",
            "*Listbox.selectBackground",
        ):
            self._root.option_add(key, sel_bg)
        for key in (
            "*TCombobox*Listbox.selectForeground",
            "*Listbox.selectForeground",
        ):
            self._root.option_add(key, sel_fg)

        # Set the Windows title bar color to match the theme.
        # "modern" gets an accent-tinted titlebar (Fluent-style); "classic" just follows bg.
        titlebar_color = accent if style == "modern" else bg
        self._set_title_bar_color(self._hex_to_colorref(titlebar_color))


###################
# GUI MANAGER END #
###################


if __name__ == "__main__":
    # Everything below is for debug purposes only
    import aiohttp
    from types import SimpleNamespace

    class StrNamespace(SimpleNamespace):
        __hash__ = object.__hash__  # type: ignore

        def __str__(self):
            if hasattr(self, "_str__"):
                return self._str__(self)
            return super().__str__()

    class HashNamespace(SimpleNamespace):
        __hash__ = object.__hash__  # type: ignore

    def create_game(id: int, name: str) -> Game:
        # StrNamespace duck-types Game for debug purposes; cast so callers below
        # (set_games, etc.) match the real Game type without runtime changes
        return cast("Game", StrNamespace(name=name, id=id, _str__=lambda s: s.name))

    iid = 0

    def create_channel(
        name: str,
        status: int,
        game: str | None,
        drops: bool,
        viewers: int,
        acl_based: bool,
    ):
        # status: 0 -> OFFLINE, 1 -> PENDING_ONLINE, 2 -> ONLINE
        if status == 1:
            status = False
            pending = True
        else:
            pending = False
        if game is not None:
            game_obj: Game | None = create_game(0, game)
        else:
            game_obj = None
        global iid
        # SimpleNamespace duck-types Channel for debug purposes; cast so callers below
        # (display, set_watching, etc.) match the real Channel type without runtime changes
        return cast("Channel", SimpleNamespace(
            name=name,
            iid=(iid := iid + 1),
            online=bool(status),
            pending_online=pending,
            game=game_obj,
            drops_enabled=drops,
            viewers=viewers,
            acl_based=acl_based,
        ))

    def create_drop(
        campaign_name: str,
        game_name: str,
        rewards: list[str],
        claimed_drops: int,
        total_drops: int,
        current_minutes: int,
        total_minutes: int,
    ):
        cd = claimed_drops
        td = total_drops
        cm = current_minutes
        tm = total_minutes
        ref_stamp = datetime.now(timezone.utc)
        drop_image_url = (
            "https://static-cdn.jtvnw.net/twitch-quests-assets/"
            "REWARD/e0ede26e-b071-47f0-af5f-b80b26fa9fb4.png"
        )
        campaign_image_url = "https://static-cdn.jtvnw.net/ttv-boxart/515025-120x160.jpg"
        benefits = [SimpleNamespace(name=name, image_url=drop_image_url) for name in rewards]
        mock = SimpleNamespace(
            id="0",
            campaign=HashNamespace(
                name=campaign_name,
                id="campaign",
                game=create_game(0, game_name),
                expired=False,
                active=False,
                upcoming=True,
                eligible=False,
                finished=False,
                link_url="https://google.com",
                image_url=campaign_image_url,
                allowed_channels=[],
                starts_at=ref_stamp,
                ends_at=ref_stamp + timedelta(days=7),
                timed_drops={},
                claimed_drops=cd,
                total_drops=td,
                required_minutes=tm,
                remaining_drops=td - cd,
                progress=(cd * tm + cm) / (td * tm),
                remaining_minutes=(td - cd) * tm - cm,
            ),
            image_url=drop_image_url,
            can_claim=False,
            can_earn=lambda: False,
            is_claimed=False,
            preconditions=True,
            benefits=benefits,
            rewards_text=lambda: ', '.join(b.name for b in benefits),
            starts_at=ref_stamp + timedelta(seconds=2),
            ends_at=ref_stamp + timedelta(days=7) - timedelta(seconds=2),
            progress=cm/tm,
            current_minutes=cm,
            required_minutes=tm,
            remaining_minutes=tm-cm,
        )
        mock.campaign.timed_drops["0"] = mock
        mock.campaign.drops = mock.campaign.timed_drops.values()
        return mock

    async def main(exit_event: asyncio.Event):
        # Initialize GUI debug
        mock = SimpleNamespace(
            settings=SimpleNamespace(
                tray=False,
                priority=[],
                proxy=URL(),
                dark_mode=False,
                alter=lambda: None,
                language="English",
                autostart_tray=False,
                exclude={"Lit Game"},
                tray_notifications=True,
                enable_badges_emotes=False,
                available_drops_check=False,
                logging_level=LOGGING_LEVELS[0],
                priority_mode=PriorityMode.PRIORITY_ONLY,
            )
        )
        mock.change_state = lambda state: mock.gui.print(f"State change: {state.value}")
        mock.state_change = lambda state: partial(mock.change_state, state)
        mock.request = aiohttp.request
        # _.set_language("Русский")
        gui = GUIManager(mock)  # type: ignore
        mock.gui = gui
        mock.close = gui.stop
        gui.start()
        assert gui._poll_task is not None
        gui._poll_task.add_done_callback(lambda t: exit_event.set())
        # Login form
        gui.login.update("Login required", None)
        # Game selector and settings panel games
        gui.set_games(set([
            create_game(420690, "Lit Game"),
            create_game(123456, "Best Game"),
            create_game(654321, "My Game Very Long Name"),
        ]))
        # Channel list
        gui.channels.display(
            create_channel(
                name="Thomus",
                status=0,
                game=None,
                drops=False,
                viewers=0,
                acl_based=True,
            ),
            add=True,
        )
        channel = create_channel(
            name="Traitus", status=1, game=None, drops=False, viewers=0, acl_based=True
        )
        gui.channels.display(channel, add=True)
        gui.channels.set_watching(channel)
        gui.channels.display(
            create_channel(
                name="Testus",
                status=2,
                game="Best Game",
                drops=True,
                viewers=42,
                acl_based=False,
            ),
            add=True,
        )
        gui.channels.display(
            create_channel(
                name="Livus",
                status=2,
                game="Best Game",
                drops=True,
                viewers=69,
                acl_based=False,
            ),
            add=True,
        )
        gui._root.update()
        gui.channels.get_selection()
        # Inventory overview (this manual test always runs with the tab enabled)
        assert gui.inv is not None
        drop = create_drop(
            "Wardrobe Cleaning", "Cleaning Masters", ["Fancy Pants"], 2, 7, 0, 240
        )
        campaign = drop.campaign
        await gui.inv.add_campaign(cast("DropsCampaign", campaign))

        gui.print("Single-line test message")
        await asyncio.sleep(1)
        gui.print("Multi-line\ntest\nmessage")

        # Tray
        # gui.tray.minimize()
        await asyncio.sleep(2)
        claim_text = (
            f"{campaign.game.name}\n"
            f"{drop.rewards_text()} ({campaign.claimed_drops}/{campaign.total_drops})"
        )
        gui.tray.notify(claim_text, "Mined Drop")

        # Drop progress
        gui.display_drop(cast("TimedDrop", drop), countdown=False)
        await asyncio.sleep(3)

        gui.progress.start_timer()
        await asyncio.sleep(5)

        gui.clear_drop()
        await asyncio.sleep(5)

        campaign.can_earn = lambda: True
        gui.inv.update_drop(cast("TimedDrop", drop))
        gui.display_drop(cast("TimedDrop", drop))
        await asyncio.sleep(10)

        drop.current_minutes = 239
        drop.remaining_minutes = 1
        drop.progress = 239/240
        campaign.remaining_minutes -= 1
        gui.inv.update_drop(cast("TimedDrop", drop))
        gui.display_drop(cast("TimedDrop", drop))
        await asyncio.sleep(63)

        drop.current_minutes = 240
        drop.remaining_minutes = 0
        drop.progress = 1.0
        campaign.remaining_minutes -= 1
        campaign.progress = 3/7
        campaign.claimed_drops = 3
        campaign.remaining_drops = 4
        gui.inv.update_drop(cast("TimedDrop", drop))
        gui.display_drop(cast("TimedDrop", drop))

    def main_exit(task: asyncio.Task[None]) -> None:
        if task.exception() is not None:
            exit_event.set()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    exit_event = asyncio.Event()
    main_task = loop.create_task(main(exit_event))
    main_task.add_done_callback(main_exit)
    loop.run_until_complete(exit_event.wait())
    if main_task.done():
        loop.run_until_complete(main_task)
