#!/usr/bin/env python3
"""
Web browser handler adapter for GBA OS.

The shell's own browser is a small text renderer: it reads pages and shows
words and pictures, but it cannot run a page that needs a real engine, and
it has no pointer of its own. This adapter runs a *real* browser - NetSurf
or Dillo - in the session's X display, where the mouse is a mouse.

Rules it obeys:

* the browser is a foreground program of this session and nothing else runs
  while it is up (Rule S2, S3);
* its profile lives in the handler's own configuration directory, never in the
  user's home, so an update cannot carry a stale profile and the session
  cannot write outside its data (Rule H5);
* the browser is terminated when the handler is, so no window is left behind
  and no process is orphaned (Rule S8).
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import setup_logging

logger = setup_logging("gba.web")

# Browsers this handler knows how to drive, best first. NetSurf renders CSS
# and runs a small amount of JavaScript; Dillo is the lightest of the two.
BROWSERS: List[Tuple[str, List[str]]] = [
    ("netsurf", []),
    ("netsurf-3", []),
    ("dillo", []),
]

DEFAULT_URL = "https://en.wikipedia.org/wiki/Game_Boy_Advance"
INSTALL_HINT = "sudo apt install netsurf-3 dillo"


def find_browser(preferred: str = "") -> Optional[Tuple[str, List[str]]]:
    """The browser to run: the requested one, else the first that is installed.

    ``auto`` means "whatever is there", which is what the settings ask for
    when the user has not made a choice.
    """
    preferred = (preferred or "").strip().lower()
    if preferred and preferred != "auto":
        for name, args in BROWSERS:
            if name == preferred:
                path = shutil.which(name)
                if path:
                    return path, args
                return None
        path = shutil.which(preferred)
        if path:
            return path, []
        return None
    for name, args in BROWSERS:
        path = shutil.which(name)
        if path:
            return path, args
    return None


def build_command(browser: Tuple[str, List[str]], url: str) -> List[str]:
    """The command line for the browser.

    The profile is not a flag: both browsers keep it under their home
    directory, and the home directory is the handler's own (see
    build_environment).
    """
    path, args = browser
    command = [path] + list(args)
    if url:
        command.append(url)
    return command


def build_environment(profile_dir: str) -> dict:
    """The environment the browser runs in.

    ``HOME`` points at the handler's own directory, so the browser keeps its
    cookies and cache there instead of in the user's home.
    """
    env = os.environ.copy()
    env["HOME"] = profile_dir
    # These are set, not defaulted: an inherited XDG path would put the
    # browser's profile outside the handler's own directory.
    env["XDG_CONFIG_HOME"] = os.path.join(profile_dir, ".config")
    env["XDG_CACHE_HOME"] = os.path.join(profile_dir, ".cache")
    env["XDG_DATA_HOME"] = os.path.join(profile_dir, ".local", "share")
    # The X authority is kept: a session that runs its X server with a cookie
    # refuses every client that does not present it.
    return env


def main() -> int:
    config_dir = os.environ.get("SYS_CONFIG_DIR") or os.path.join(
        os.environ.get("SYS_DATA_DIR", "/tmp"), "web-profile")
    data_dir = os.environ.get("SYS_DATA_DIR") or config_dir
    for path in (config_dir, data_dir):
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as exc:
            logger.error("Cannot create %s: %s", path, exc)
            return 2

    preferred = os.environ.get("GBA_WEB_BROWSER", "").strip()
    url = os.environ.get("GBA_WEB_URL", "").strip() or DEFAULT_URL

    if not os.environ.get("DISPLAY"):
        logger.error("No DISPLAY: a full browser needs the session's X display. "
                     "Use the built-in browser in the BROWSER tab instead.")
        return 3

    browser = find_browser(preferred)
    if browser is None:
        logger.error("No supported browser found (looked for %s). Install one with: %s",
                     ", ".join(name for name, _ in BROWSERS), INSTALL_HINT)
        return 4

    command = build_command(browser, url)
    env = build_environment(config_dir)
    logger.info("Starting %s in the session display (url: %s)", browser[0], url)

    try:
        proc = subprocess.Popen(command, env=env, cwd=config_dir)
    except Exception as exc:
        logger.error("Could not start %s: %s", browser[0], exc)
        return 5

    def terminate(signum, frame):  # pragma: no cover - signal path
        logger.info("Web adapter asked to stop; closing the browser.")
        try:
            proc.terminate()
        except Exception:
            pass

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)

    try:
        code = proc.wait()
    except Exception as exc:
        logger.error("Browser exited badly: %s", exc)
        code = 1
    logger.info("Browser closed with status %d", code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
