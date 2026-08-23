from __future__ import annotations

import sys
import subprocess
from pathlib import Path

from constants import PROFILES_DIR, ACTIVE_PROFILE, SELF_PATH, IS_PACKAGED


def list_profiles() -> list[str]:
    if not PROFILES_DIR.exists():
        return []
    return sorted(p.name for p in PROFILES_DIR.iterdir() if p.is_dir())


def create_profile(name: str) -> bool:
    name = name.strip()
    if not name or any(c in name for c in '\\/:*?"<>|'):
        return False
    (PROFILES_DIR / name).mkdir(parents=True, exist_ok=True)
    return True


def launch_profile(name: str) -> None:
    # spawns a fully independent process for that account, so multiple accounts
    # can mine drops at the same time, each with its own session/cookies/proxy
    if IS_PACKAGED:
        cmd = [str(SELF_PATH), "--profile", name]
    else:
        cmd = [sys.executable, str(Path(SELF_PATH)), "--profile", name]
    subprocess.Popen(cmd, cwd=str(SELF_PATH.parent))
