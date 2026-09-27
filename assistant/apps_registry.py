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
        Launches an application by name.
        Sequence:
          1. Check physical Desktop & Start Menu shortcuts (.lnk) -> launch via os.startfile.
          2. Check known executable paths (VS Code, Brave, Chrome, etc.).
          3. Check apps.json -> shell:AppsFolder / exe / URL.
          4. Check system PATH.
          5. Fallback: Open web app in browser.
        """
        import re
        app_name_clean = app_name.strip()
        name_lower = app_name_clean.lower()

        # 1. First priority: Check physical Desktop & Start Menu shortcuts (.lnk)
        lnk_path, display = self.find_installed_shortcut(name_lower)
        if lnk_path and os.path.exists(lnk_path):
            try:
                os.startfile(lnk_path)
                return {
                    "status": "success",
                    "mode": "shortcut",
                    "message": f"Opening {display}, Satyam.",
                    "target": lnk_path
                }
            except Exception as e:
                logger.warning(f"Failed to startfile shortcut {lnk_path}: {e}")

        # 2. Second priority: Known local executable paths
        known_executables = {
            "vs code": os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
            "vscode": os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
            "code": os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
            "visual studio code": os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
            "brave": os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            "chrome": os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            "google chrome": os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            "notepad": "notepad.exe",
            "calculator": "calc.exe",
            "calc": "calc.exe",
            "paint": "mspaint.exe"
        }
        for alias, exe in known_executables.items():
            if name_lower == alias:
                if os.path.isabs(exe) and os.path.exists(exe):
                    try:
                        os.startfile(exe)
                        return {
                            "status": "success",
                            "mode": "executable",
                            "message": f"Opening {app_name_clean.title()}, Satyam.",
                            "target": exe
                        }
                    except Exception as e:
                        logger.warning(f"os.startfile failed for {exe}: {e}")
                elif not os.path.isabs(exe):
                    try:
                        os.startfile(exe)
                        return {
                            "status": "success",
                            "mode": "executable",
                            "message": f"Opening {app_name_clean.title()}, Satyam.",
                            "target": exe
                        }
                    except Exception as e:
                        logger.warning(f"os.startfile failed for {exe}: {e}")

        # 3. Try apps.json
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
                    os.startfile(appid)
                    return {
                        "status": "success",
                        "mode": "exe",
                        "message": f"Opening {display_name}, Satyam.",
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
                    "message": f"Opening {display_name}, Satyam.",
                    "target": appid
                }
            except Exception as e:
                logger.error(f"Failed to launch via shell:AppsFolder: {e}")

        # 4. Try standard system executable via PATH
        exe_path = shutil.which(name_lower) or shutil.which(f"{name_lower}.exe")
        if exe_path:
            try:
                os.startfile(exe_path)
                return {
                    "status": "success",
                    "mode": "system_path",
                    "message": f"Opening {app_name_clean.title()}, Satyam.",
                    "target": exe_path
                }
            except Exception as e:
                logger.warning(f"PATH execution failed for {exe_path}: {e}")

        # 5. Graceful Fallback: Open Web Application in Browser
        web_url = WEB_APP_FALLBACKS.get(name_lower)
        if not web_url:
            web_url = f"https://www.google.com/search?q={app_name_clean}+web+app"

        self._open_url(web_url, browser)
        return {
            "status": "success",
            "mode": "web_fallback",
            "message": f"Opening {app_name_clean.title()} in your browser, Satyam.",
            "target": web_url
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

