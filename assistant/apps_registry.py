"""
App Registry Subsystem for Kate.
Resolves and launches local Windows applications from Desktop apps.json (131+ apps),
system PATH, and gracefully falls back to launching the web application in browser.
"""

import os
import sys
import json
import logging
import subprocess
import shutil
import webbrowser
import re

logger = logging.getLogger("kate.apps")

DEFAULT_APPS_JSON = os.path.expanduser(r"~\Desktop\apps.json")
FALLBACK_APPS_JSON = os.path.join(os.path.dirname(__file__), "apps.json")

# Common web application URLs for instant browser fallback
WEB_APP_FALLBACKS = {
    "figma": "https://www.figma.com",
    "notion": "https://www.notion.so",
    "canva": "https://www.canva.com",
    "discord": "https://discord.com/app",
    "spotify": "https://open.spotify.com",
    "slack": "https://app.slack.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "instagram": "https://www.instagram.com",
    "reddit": "https://www.reddit.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "gemini": "https://gemini.google.com",
    "github": "https://github.com",
    "youtube": "https://www.youtube.com",
    "whatsapp": "https://web.whatsapp.com",
    "telegram": "https://web.telegram.org",
    "gmail": "https://mail.google.com",
    "maps": "https://maps.google.com",
    "linkedin": "https://www.linkedin.com"
}

KNOWN_APP_ALIASES = {
    "vs code": ["visual studio code", "code"],
    "vscode": ["visual studio code", "code"],
    "code": ["visual studio code", "vs code"],
    "yt": ["youtube"],
    "browser": ["brave", "chrome", "edge"],
    "calc": ["calculator"],
    "cmd": ["command prompt", "terminal"],
    "terminal": ["powershell", "cmd", "windows terminal"]
}


class AppRegistry:
    """Manages application discovery, AppID launching, and web fallback."""

    def __init__(self, json_path: str = None):
        self.json_path = json_path or DEFAULT_APPS_JSON
        self.apps_by_name = {}
        self._load_registry()

    def _load_registry(self):
        target = self.json_path if os.path.exists(self.json_path) else FALLBACK_APPS_JSON
        if not os.path.exists(target):
            logger.warning(f"apps.json not found at {target}")
            return

        try:
            # Handle UTF-8 with BOM (utf-8-sig) commonly output by PowerShell
            with open(target, "r", encoding="utf-8-sig") as f:
                data = json.load(f)

            if isinstance(data, list):
                for item in data:
                    name = item.get("Name", "").strip().lower()
                    appid = item.get("AppID", "").strip()
                    if name and appid:
                        self.apps_by_name[name] = {
                            "display_name": item.get("Name"),
                            "appid": appid
                        }
            logger.info(f"Loaded {len(self.apps_by_name)} apps from {target}")
        except Exception as e:
            logger.error(f"Failed to load apps.json: {e}")

    def find_installed_shortcut(self, name: str) -> tuple[str | None, str | None]:
        """Scans Desktop and Start Menu for real .lnk application shortcuts."""
        clean = re.sub(r'[^a-zA-Z0-9]', '', name).lower()
        targets = [clean]
        for k, aliases in KNOWN_APP_ALIASES.items():
            if clean == re.sub(r'[^a-zA-Z0-9]', '', k).lower():
                targets.extend([re.sub(r'[^a-zA-Z0-9]', '', a).lower() for a in aliases])

        search_dirs = [
            os.path.expanduser('~/Desktop'),
            os.path.expandvars(r'%APPDATA%\Microsoft\Windows\Start Menu\Programs\Chrome Apps'),
            os.path.expandvars(r'%APPDATA%\Microsoft\Windows\Start Menu\Programs'),
            os.path.expandvars(r'%ALLUSERSPROFILE%\Microsoft\Windows\Start Menu\Programs')
        ]
        for d in search_dirs:
            if not os.path.exists(d):
                continue
            try:
                entries = os.listdir(d)
            except Exception:
                continue
            for f in entries:
                full_p = os.path.join(d, f)
                if os.path.isfile(full_p) and f.lower().endswith('.lnk'):
                    base = os.path.splitext(f)[0]
                    base_clean = re.sub(r'\s*-\s*copy.*$', '', base, flags=re.IGNORECASE).strip()
                    norm = re.sub(r'[^a-zA-Z0-9]', '', base_clean).lower()
                    for t in targets:
                        if t == norm or t in norm or norm in t:
                            return full_p, base_clean
        return None, None

    def find_app(self, app_name: str) -> dict | None:
        """Finds app entry by exact or fuzzy substring match."""
        name_clean = app_name.strip().lower()

        # 1. Exact match
        if name_clean in self.apps_by_name:
            return self.apps_by_name[name_clean]

        # 2. Substring match
        for k, v in self.apps_by_name.items():
            if name_clean in k or k in name_clean:
                return v

        return None

    def launch(self, app_name: str, browser: str = None) -> dict:
        """
        Launches an application by name using the universal WindowsAppLauncher.
        Handles registry scanning, shortcut resolution, phonetic corrections,
        and foreground process detachment.
        """
        try:
            from assistant.launchers import app_launcher
            res = app_launcher.launch_by_name(app_name, browser=browser)
            return res.to_dict()
        except Exception as e:
            logger.warning(f"Universal launcher failed, attempting legacy fallback: {e}")
            return {
                "status": "error",
                "mode": "error",
                "message": f"Failed to launch {app_name}: {e}",
                "target": app_name
            }

    def _open_url(self, url: str, browser: str = None):
        """Opens URL in specified browser or system default."""
        try:
            if hasattr(os, "startfile"):
                os.startfile(url)
                return
        except Exception:
            pass
        webbrowser.open(url)


# Global singleton instance
app_registry = AppRegistry()

