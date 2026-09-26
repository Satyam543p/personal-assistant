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
        Launches an application by name.
        Sequence:
          1. Check apps.json -> shell:AppsFolder / exe / URL.
          2. Check system PATH or common paths.
          3. Fallback: Open web app in browser.
        """
        app_name_clean = app_name.strip()
        name_lower = app_name_clean.lower()

        # 1. Try apps.json
        entry = self.find_app(name_lower)
        if entry:
            appid = entry["appid"]
            display_name = entry["display_name"]

            # If AppID is a web URL
            if appid.startswith("http://") or appid.startswith("https://"):
                self._open_url(appid, browser)
                return {
                    "status": "success",
                    "mode": "url",
                    "message": f"Opened {display_name} in your browser, Satyam.",
                    "target": appid
                }

            # If AppID is an explicit .exe file path on disk
            if os.path.isabs(appid) and os.path.exists(appid):
                try:
                    subprocess.Popen([appid], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
                    return {
                        "status": "success",
                        "mode": "exe",
                        "message": f"Launching {display_name}, Satyam.",
                        "target": appid
                    }
                except Exception as e:
                    logger.warning(f"Direct launch failed for {appid}: {e}")

            # Standard Windows AppID (UWP or Start Menu shortcut)
            try:
                cmd = ["explorer.exe", f"shell:AppsFolder\\{appid}"]
                subprocess.Popen(cmd)
                return {
                    "status": "success",
                    "mode": "shell_apps_folder",
                    "message": f"Launching {display_name}, Satyam.",
                    "target": appid
                }
            except Exception as e:
                logger.error(f"Failed to launch via shell:AppsFolder: {e}")

        # 2. Try standard system executable
        exe_path = shutil.which(name_lower) or shutil.which(f"{name_lower}.exe")
        if exe_path:
            try:
                subprocess.Popen([exe_path], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
                return {
                    "status": "success",
                    "mode": "system_path",
                    "message": f"Launching {app_name_clean}, Satyam.",
                    "target": exe_path
                }
            except Exception as e:
                logger.warning(f"PATH execution failed for {exe_path}: {e}")

        # 3. Graceful Fallback: Open Web Application in Browser
        web_url = WEB_APP_FALLBACKS.get(name_lower)
        if not web_url:
            web_url = f"https://www.google.com/search?q={app_name_clean}+web+app"

        self._open_url(web_url, browser)
        return {
            "status": "success",
            "mode": "web_fallback",
            "message": f"I couldn't find the desktop app for {app_name_clean}, so I've opened its web version in your browser, Satyam.",
            "target": web_url
        }

    def _open_url(self, url: str, browser: str = None):
        """Opens URL in specified browser (e.g. Brave) or system default."""
        if browser:
            b_lower = browser.lower()
            brave_path = os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe")
            if "brave" in b_lower and os.path.exists(brave_path):
                subprocess.Popen([brave_path, url])
                return

        webbrowser.open(url)


# Global singleton instance
app_registry = AppRegistry()
