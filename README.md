# DropStream

> **DropStream is an unofficial, community-made update/fork of [Twitch Drops Miner (TDM), created by DevilXD](https://github.com/DevilXD/TwitchDropsMiner).**
> The core drop-mining engine and the vast majority of the original design and codebase come directly from
> DevilXD's project — full credit goes to them. This fork keeps that engine intact and adds a dashboard,
> multi-account profiles, a scheduler, a remote web dashboard, and a redesigned theme system on top of it
> (see below). If you find this fork useful, please consider
> [supporting DevilXD, the original author](https://www.buymeacoffee.com/DevilXD), whose work this is built on.

This application lets you AFK-mine timed Twitch drops, without having to worry about switching channels
when the one you're watching goes offline, claiming the drops yourself, or even receiving the actual
stream data. This saves you bandwidth and hassle.

## How It Works

Every few seconds, the application simulates watching a stream by requesting its metadata, which is enough
to make progress on active drops. This approach avoids downloading any actual video or audio data. A
persistent (sharded) websocket connection keeps every channel's status (ONLINE/OFFLINE) and live viewer
count up to date in real time.

## What DropStream adds on top of the original TDM

### Dashboard

![Dashboard](screenshots/dashboard.png)

A dedicated overview tab: current pause/resume state, the drop and campaign currently being mined with
its progress bar and time remaining, the full campaign artwork with every reward item, a "last 7 days"
mining-activity chart, a "drops per game" breakdown chart, and running totals for drops claimed and watch
hours saved.

### Games (priority & exclude lists)

![Games](screenshots/games.png)

A clearer, dedicated tab for the game **Priority** and **Exclude** lists that used to live buried in
Settings. Reorder priorities with one click (move up/down, top/bottom), pick a priority mode from the
dropdown, and press **Reload** to apply changes.

### Details

![Details](screenshots/details.png)

Everything about what's currently going on, in one place: the currently-watched channel, per-shard
websocket connection status, the login/connection form, the full list of matching channels with their
live status/game/viewer count, current campaign and drop progress, and the raw application log/output.

### Remote web dashboard

![Remote](screenshots/remote.png)

Its own **Distant/Remote** tab: turn on a small built-in web server and generate a private link (a random
token embedded in the URL, e.g. `http://192.168.1.42:21000/8f3a.../`), with an "Open" button to launch it
directly in your default browser. Anyone on the same network with that link gets a page mirroring most of
the desktop app (current drop/campaign with reward art pulled straight from Twitch, a drops-per-game
leaderboard, and the full campaign list).

You choose the **access mode**:

- **View only** (default): visitors can only watch progress.
- **View and control**: visitors can also pause/resume mining and change the priority mode. You can set an
  optional **control password** — without one, anyone with the link can control the app; with one, they
  additionally need the password for control actions (viewing never requires it).

A few things worth knowing:

- The link is the only thing standing between a stranger and "just viewing" your instance — treat it like
  a password. Use **Generate a new link** any time you want to revoke a previously shared one.
- By default it's only reachable on your local network. Reaching it from the internet needs port
  forwarding on your router (this exposes the link publicly, including to scanners) or a private
  tunnel/VPN (Tailscale, WireGuard, etc.) instead.
- The port (`21000` by default) can be changed if it conflicts with something else on your machine.
- The dashboard shares the same event loop as the mining logic but is built to stay lightweight: requests
  are rate-limited per visitor, the tracked-IP table is bounded, and reward/box-art images are never
  proxied or cached by the app — the page links straight to Twitch's own CDN.
- This dashboard is view/control only — it can never be used to log into your Twitch account or claim
  drops directly; it only reflects and steers what the desktop app is already doing.

### Settings

![Settings](screenshots/settings.png)

- **General**: language, autostart (with "start minimized to tray"), tray notifications, a
  RAM/battery-saving mode once minimized, a Light/Dark/Auto theme (with an option to follow your OS accent
  color), an optional inventory tab, and a proxy field.
- **Accounts**: multi-account **profiles** — isolated settings/cookies/cache per account, with buttons to
  create a profile, launch several accounts in parallel, switch the active profile, or delete one.
- **Scheduler**: restrict mining to a daily time window (start/end), with a configurable action once all of
  today's drops have been claimed.
- **Reliability**: automatic restart after a crash, after a configurable delay.
- **Advanced**: lower-level tuning options for troubleshooting (may affect stability — leave alone unless
  you know what you're doing).

### Help

![Help](screenshots/help.png)

An in-app **Aide/Help** tab: version and fork info with a direct credit and link back to DevilXD's original
project, quick links to your Twitch inventory and campaigns page, a "How It Works" explanation, a
step-by-step "Getting Started" guide, and a button to invalidate your saved authentication token (useful
when switching accounts).

### Other additions

- Automatic OAuth token re-validation, to catch and recover from an expired session before it breaks
  mining.
- A resizable, scrollable main window: every tab now scrolls (mouse wheel, or the scrollbar that appears
  only when needed) instead of clipping content when the window is small. Ctrl+PageUp/PageDown switches
  tabs from anywhere.

## Features (inherited from the original engine)

- Stream-less drop mining — no video/audio ever downloaded.
- Game priority and exclusion lists, to focus on what you want, in the order you want, and ignore the rest.
- Sharded websocket connections, tracking up to `199` channels at the same time.
- Automatic drop-campaign discovery based on your linked accounts (you still need to
  [link accounts](https://www.twitch.tv/drops/campaigns) yourself).
- Stream tag and drop-campaign validation, so you don't end up watching a stream that can't earn the drop.
- Automatic channel switching, when the current channel goes offline or a higher-priority game's stream
  comes online.
- Login session saved to a cookies file — no need to log in every run.
- Mining starts automatically as new campaigns appear and stops once all available drops are mined.

## Usage

1. Download and unzip [the latest release](../../releases) — it's recommended to keep it in the folder it
   comes in.
2. Run it and log in / connect the miner to your Twitch account using the in-app login form.
3. After logging in, the app fetches every campaign and game you can mine drops for. Add the games you
   care about to the **Priority** list (Games tab), then press **Reload** to start processing.
4. If you'd rather have the miner grab anything it can beyond your Priority list, set **Priority mode** to
   anything other than "Priority list only".
5. Make sure your Twitch account is linked to the relevant games on the
   [campaigns page](https://www.twitch.tv/drops/campaigns), to unlock more of them for mining.

## Small window / compact layout

The main window can be resized noticeably smaller than before. Instead of clipping a tab's content once it
no longer fits, every tab scrolls vertically — a scrollbar appears automatically only when needed, and the
mouse wheel scrolls the content under the cursor. The tab bar itself also supports the mouse wheel (while
hovering the row of tab labels) and Ctrl+PageUp / Ctrl+PageDown from anywhere.

## Notes

> [!WARNING]
> Due to how Twitch handles drop progression on their end, watching a stream in the browser (or by any
> other means) on the same account actively used by the miner will usually cause the miner to misbehave,
> reporting false progress and getting stuck on the current drop. Avoid watching other streams on that
> account while it's mining.

> [!CAUTION]
> Persistent cookies are stored in `cookies.jar`, from which login information is restored on every run.
> Keep that file safe — whoever has it can access your Twitch account without knowing your password.

> [!IMPORTANT]
> Logging in successfully may trigger a "New Login" notification email from Twitch. This is expected — you
> can verify it comes from your own IP. The detected browser will show as "Chrome", since that's what the
> miner presents itself as to Twitch's servers.

> [!NOTE]
> The remaining-time countdown always ticks down one minute at a time and then pauses, restarting once the
> application re-determines the actual remaining time from Twitch (at most 20 seconds after reaching zero).
> It's only an approximation and doesn't reflect or affect actual mining speed.

> [!NOTE]
> Running from source requires Python 3.10 or higher.

### Windows build

- The app is packaged with PyInstaller into a portable `EXE`. Some antivirus engines (including Windows
  Defender) may flag it as a trojan, because PyInstaller has historically been abused to package malicious
  code by others — these reports can be safely ignored. If you don't trust the executable, install Python
  yourself and run from source instead.
- The executable uses `%TEMP%` for temporary runtime files; persistent data is stored next to the
  executable.
- Autostart is implemented as a registry entry under the current user's (`HKCU`) autostart key. Relocating
  the app afterwards breaks it until you toggle the option off and back on.
- A native Windows installer (built with Inno Setup) is also provided alongside the portable ZIP.
- A native **Windows on ARM** build is also published, for ARM-based Windows devices (e.g. Snapdragon
  laptops).

### Linux build

- Distributed as both an [AppImage](https://appimage.org/) and a PyInstaller portable build — if unsure,
  use the AppImage.
- Both are built for `x86_64` **and `aarch64` (ARM64)**.
- Requires `glibc>=2.35` and a working display server.
- The Linux app is noticeably larger than the Windows one due to bundling `gtk3` (and its dependencies),
  required for proper system-tray/notification support.
- As an alternative, the Windows build also runs well under [Wine](https://www.winehq.org/).

### macOS build

- Packaged with PyInstaller as a standalone `.app` bundle, distributed as a ZIP, and built **natively for
  Apple Silicon (ARM64)**.
- Since it isn't signed with a paid Apple Developer certificate, **Gatekeeper will block the first run**
  ("The application is damaged and can't be opened").
  - **Fix**: open a Terminal in the folder containing the app and run `xattr -cr DropStream.app` (or type
    `xattr -cr ` with a trailing space, then drag the `.app` into the terminal window to auto-fill the
    path, and press Enter).
- Persistent files (`cookies.jar`, `settings.json`, `lock.file`, `cache/`) live inside the bundle, under
  `DropStream.app/Contents/MacOS` (right-click the app → "Show Package Contents" to access them).

## Advanced usage

To run from the latest source, or build your own executable, see DevilXD's original wiki page (the build
process is unchanged by this fork):
https://github.com/DevilXD/TwitchDropsMiner/wiki/Setting-up-the-environment,-building-and-running

## Support

If you run into an issue:

- Check DevilXD's [troubleshooting page](https://github.com/DevilXD/TwitchDropsMiner/wiki/Troubleshooting)
  for common issues (still applicable, since the mining engine is unchanged).
- [Search this repository's issues](../../issues) to see if it's already been reported.
- If not, feel free to open a new one describing your problem.

If you find DropStream useful, please also consider supporting DevilXD, whose original project this is
built on:

<div align="center">

[![Buy me a coffee](https://i.imgur.com/cL95gzE.png)](https://www.buymeacoffee.com/DevilXD)
[![Support me on Patreon](https://i.imgur.com/Mdkb9jq.png)](https://www.patreon.com/bePatron?u=26937862)

</div>

## Credits

@guihkx - For the CI script, CI maintenance, and everything related to Linux builds.
@kWAYTV - For the implementation of the dark mode theme.
@crocchetto - For the macOS port.

@Bamboozul - For the entirety of the Arabic (العربية) translation.
@Suz1e - For the entirety of the Chinese (简体中文) translation and revisions.
@wwj010, @zhangminghao1989, @Self4215 - For the Chinese (简体中文) translation corrections and revisions.
@Ricky103403 - For the entirety of the Traditional Chinese (繁體中文) translation.
@LusTerCsI - For the Traditional Chinese (繁體中文) translation corrections and revisions.
@nwvh - For the entirety of the Czech (Čeština) translation.
@Kjerne - For the entirety of the Danish (Dansk) translation.
@lmdpocus - For the entirety of the Dutch (Nederlandse) translation.
@Rensoraa - For the Dutch (Nederlandse) translation corrections and revisions.
@roobini-gamer - For the entirety of the French (Français) translation.
@Calvineries - For the French (Français) translation revisions.
@ThisIsCyreX - For the entirety of the German (Deutsch) translation.
@Nagyhoho1234 - For the entirety of the Hungarian (Magyar) translation.
@Eriza-Z - For the entirety of the Indonesian translation.
@casungo - For the entirety of the Italian (Italiano) translation.
@ShimadaNanaki - For the entirety of the Japanese (日本語) translation.
@biroman - For the entirety of the Norwegian (Norsk) translation.
@Patriot99 - For the Polish (Polski) translation and revisions (co-authored with @DevilXD).
@zarigata - For the entirety of the Portuguese (Português) translation.
@Sergo1217 - For the entirety of the Russian (Русский) translation.
@kilroy98, @flamesv - For the Russian (Русский) translation corrections and revisions.
@Shofuu - For the entirety of the Spanish (Español) translation and revisions.
@Forero-0 - For the Spanish (Español) translation revisions.
@alikdb - For the entirety of the Turkish (Türkçe) translation.
@DogancanYr, @Elderly-Emre, @Hweord - For the Turkish (Türkçe) translation corrections and revisions.
@Nollasko - For the entirety of the Ukrainian (Українська) translation and revisions.
@kilroy98 - For the Ukrainian (Українська) translation corrections and revisions.
