"""
Windows Application Resolver & Launcher Implementation.
Features:
- Windows Registry 'App Paths' indexing (scans all registered software on the PC)
- Start Menu & Desktop shortcut scanner with normalized target extraction
- Phonetic & fuzzy alias mapping (handles STT typos like 'visual course' -> VS Code, 'rape' -> Brave)
- Direct detached GUI process launching so applications reliably pop up in the foreground
"""
import os
import sys
import re
import difflib
import logging
import subprocess
import webbrowser
from typing import Optional, Dict, List, Tuple

try:
    import winreg
except ImportError:
    winreg = None

from .base import IAppResolver, IAppLauncher, AppTarget, LaunchResult

logger = logging.getLogger("kate.launcher")


# Common phonetic & STT typo corrections
PHONETIC_CORRECTIONS = {
    "visual course": "vs code",
    "visual studio": "vs code",
    "visual code": "vs code",
    "code": "vs code",
    "rape": "brave",
    "brave browser": "brave",
    "chrome browser": "chrome",
    "google": "chrome",
    "not pad": "notepad",
    "not paid": "notepad",
    "note pad": "notepad",
    "word": "winword",
    "powerpoint": "powerpnt",
    "ppt": "powerpnt",
    "excel": "excel",
    "calc": "calculator",
    "terminal": "powershell",
    "cmd": "cmd",
    "command prompt": "cmd",
    "task manager": "taskmgr",
    "taskmanager": "taskmgr",
    "file explorer": "explorer",
    "files": "explorer",
    "my computer": "explorer",
}

# Devanagari common transliterations
DEVANAGARI_APP_MAP = {
    "यूट्यूब": "youtube",
    "गूगल": "google",
    "क्रोम": "chrome",
    "ब्रेव": "brave",
    "स्पॉटिफ़ाई": "spotify",
    "स्पॉटिफाई": "spotify",
    "नोटपैड": "notepad",
    "कैलकुलेटर": "calculator"
}

# Web application fallbacks if desktop app is not installed
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


class WindowsAppResolver(IAppResolver):
    """Discovers and resolves application names across Windows Registry, Shortcuts, and PATH."""

    def __init__(self):
        self._registry_apps: Dict[str, str] = {}  # alias -> exe_path
        self._shortcut_apps: Dict[str, Tuple[str, str]] = {}  # alias -> (lnk_path, display_name)
        self._initialized = False

    def refresh_cache(self) -> None:
        self._registry_apps.clear()
        self._shortcut_apps.clear()
        self._index_registry_apps()
        self._index_shortcuts()
        self._initialized = True

    def _ensure_indexed(self) -> None:
        if not self._initialized:
            self.refresh_cache()

    def _index_registry_apps(self) -> None:
        """Indexes all executables registered in Windows App Paths (HKLM & HKCU)."""
        if not winreg:
            return

        hives = [
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths")
        ]

        for hive, subkey in hives:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    num_subkeys = winreg.QueryInfoKey(key)[0]
                    for i in range(num_subkeys):
                        try:
                            app_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, app_name) as app_key:
                                exe_path, _ = winreg.QueryValueEx(app_key, "")
                                if exe_path and os.path.exists(exe_path.strip('"')):
                                    clean_path = os.path.normpath(exe_path.strip('"'))
                                    base = os.path.splitext(app_name)[0].lower()
                                    self._registry_apps[base] = clean_path
                                    clean_base = re.sub(r'[^a-zA-Z0-9]', '', base)
                                    if clean_base:
                                        self._registry_apps[clean_base] = clean_path
                        except Exception:
                            continue
            except Exception:
                continue

        # Add well-known default locations
        known_defaults = {
            "brave": [
                os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
                os.path.expandvars(r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe")
            ],
            "chrome": [
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")
            ],
            "vs code": [
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft VS Code\Code.exe")
            ],
            "vscode": [
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft VS Code\Code.exe")
            ],
            "notepad": ["notepad.exe"],
            "calc": ["calc.exe"],
            "calculator": ["calc.exe"],
            "paint": ["mspaint.exe"],
            "explorer": ["explorer.exe"],
            "terminal": ["powershell.exe", "wt.exe"]
        }
        for alias, paths in known_defaults.items():
            for p in paths:
                norm = os.path.normpath(p)
                if not os.path.isabs(norm) or os.path.exists(norm):
                    self._registry_apps[alias] = norm
                    break

    def _index_shortcuts(self) -> None:
        """Indexes Start Menu and Desktop shortcuts with a safe, shallow scan."""
        search_dirs = [
            (os.path.normpath(os.path.expanduser('~/Desktop')), 1),
            (os.path.normpath(os.path.expandvars(r'%PUBLIC%\Desktop')), 1),
            (os.path.normpath(os.path.expandvars(r'%APPDATA%\Microsoft\Windows\Start Menu\Programs')), 2),
            (os.path.normpath(os.path.expandvars(r'%ALLUSERSPROFILE%\Microsoft\Windows\Start Menu\Programs')), 2)
        ]

        for base_dir, max_depth in search_dirs:
            if not os.path.exists(base_dir):
                continue
            base_depth = base_dir.rstrip(os.path.sep).count(os.path.sep)
            for root, dirs, files in os.walk(base_dir):
                cur_depth = root.rstrip(os.path.sep).count(os.path.sep) - base_depth
                if cur_depth >= max_depth:
                    dirs.clear()  # Do not recurse further
                for f in files:
                    if f.lower().endswith('.lnk'):
                        full_p = os.path.normpath(os.path.join(root, f))
                        base = os.path.splitext(f)[0]
                        base_clean = re.sub(r'\s*-\s*copy.*$', '', base, flags=re.IGNORECASE).strip()
                        key = base_clean.lower()
                        clean_key = re.sub(r'[^a-zA-Z0-9]', '', key)
                        self._shortcut_apps[key] = (full_p, base_clean)
                        if clean_key:
                            self._shortcut_apps[clean_key] = (full_p, base_clean)

    def _resolve_shortcut_target(self, lnk_path: str) -> Optional[str]:
        """Uses WScript.Shell to extract the real target .exe from a .lnk shortcut."""
        try:
            import win32com.client
            shell = win32com.client.Dispatch("WScript.Shell")
            shortcut = shell.CreateShortcut(lnk_path)
            target = shortcut.TargetPath
            if target and os.path.exists(target):
                return os.path.normpath(target)
        except Exception:
            pass
        return None

    def resolve(self, query: str) -> Optional[AppTarget]:
        self._ensure_indexed()
        if not query:
            return None

        raw = query.strip()
        norm = raw.lower()

        # 1. Translate Devanagari
        for dev, en in DEVANAGARI_APP_MAP.items():
            if dev in norm:
                norm = norm.replace(dev, en)

        # 2. Apply phonetic / STT corrections
        if norm in PHONETIC_CORRECTIONS:
            norm = PHONETIC_CORRECTIONS[norm]
        else:
            for typo, correction in PHONETIC_CORRECTIONS.items():
                if norm == typo or norm.startswith(typo + " ") or norm.endswith(" " + typo):
                    norm = correction
                    break

        clean_norm = re.sub(r'[^a-zA-Z0-9]', '', norm)

        # 3. Direct match in registry / known apps
        if norm in self._registry_apps:
            exe = self._registry_apps[norm]
            return AppTarget(name=norm, display_name=raw.title(), target_path=exe, launch_mode="executable")
        if clean_norm in self._registry_apps:
            exe = self._registry_apps[clean_norm]
            return AppTarget(name=norm, display_name=raw.title(), target_path=exe, launch_mode="executable")

        # 4. Direct match in shortcuts (.lnk)
        if norm in self._shortcut_apps:
            lnk, display = self._shortcut_apps[norm]
            real_target = self._resolve_shortcut_target(lnk)
            if real_target:
                return AppTarget(name=norm, display_name=display, target_path=real_target, launch_mode="executable")
            return AppTarget(name=norm, display_name=display, target_path=lnk, launch_mode="shortcut")
        if clean_norm in self._shortcut_apps:
            lnk, display = self._shortcut_apps[clean_norm]
            real_target = self._resolve_shortcut_target(lnk)
            if real_target:
                return AppTarget(name=norm, display_name=display, target_path=real_target, launch_mode="executable")
            return AppTarget(name=norm, display_name=display, target_path=lnk, launch_mode="shortcut")

        # 5. Fuzzy match against registered & shortcut app names
        all_keys = list(self._registry_apps.keys()) + list(self._shortcut_apps.keys())
        matches = difflib.get_close_matches(norm, all_keys, n=1, cutoff=0.55)
        if not matches and clean_norm:
            matches = difflib.get_close_matches(clean_norm, all_keys, n=1, cutoff=0.55)

        if matches:
            best = matches[0]
            if best in self._registry_apps:
                exe = self._registry_apps[best]
                return AppTarget(name=best, display_name=best.title(), target_path=exe, launch_mode="executable", confidence=0.85)
            elif best in self._shortcut_apps:
                lnk, display = self._shortcut_apps[best]
                real_target = self._resolve_shortcut_target(lnk)
                if real_target:
                    return AppTarget(name=best, display_name=display, target_path=real_target, launch_mode="executable", confidence=0.85)
                return AppTarget(name=best, display_name=display, target_path=lnk, launch_mode="shortcut", confidence=0.85)

        # 6. Check web app fallbacks
        if norm in WEB_APP_FALLBACKS:
            url = WEB_APP_FALLBACKS[norm]
            return AppTarget(name=norm, display_name=raw.title(), target_path=url, launch_mode="url", confidence=0.9)

        # 7. Substring matching in shortcuts
        for k, (lnk, display) in self._shortcut_apps.items():
            if norm in k or k in norm:
                real_target = self._resolve_shortcut_target(lnk)
                if real_target:
                    return AppTarget(name=k, display_name=display, target_path=real_target, launch_mode="executable", confidence=0.75)
                return AppTarget(name=k, display_name=display, target_path=lnk, launch_mode="shortcut", confidence=0.75)

        return None


def _bring_to_foreground_async(name_or_title: str):
    """Brings newly launched window to top foreground after process init."""
    def _worker():
        import time
        time.sleep(0.6)
        try:
            import win32gui
            import win32con
            target = name_or_title.lower()
            def enum_cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    txt = win32gui.GetWindowText(hwnd).lower()
                    if target in txt:
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                        win32gui.SetForegroundWindow(hwnd)
                        return False
                return True
            try:
                win32gui.EnumWindows(enum_cb, None)
            except Exception:
                pass
        except Exception:
            pass
    import threading
    threading.Thread(target=_worker, daemon=True, name="ForegroundElevator").start()


class WindowsAppLauncher(IAppLauncher):
    """Executes resolved AppTargets with robust Windows process detachment and foreground focus."""

    def __init__(self, resolver: Optional[IAppResolver] = None):
        self.resolver = resolver or WindowsAppResolver()

    def launch(self, target: AppTarget) -> LaunchResult:
        path = target.target_path
        display = target.display_name or target.name

        try:
            if target.launch_mode == "url":
                webbrowser.open(path)
                return LaunchResult(
                    status="success",
                    mode="url",
                    message=f"Opening {display} in browser.",
                    target=path
                )

            norm_path = os.path.normpath(path)

            # If it's a direct executable on disk
            if target.launch_mode == "executable" or (os.path.isabs(norm_path) and norm_path.lower().endswith(".exe")):
                if os.path.exists(norm_path):
                    flags = 0
                    if os.name == "nt":
                        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                    subprocess.Popen(
                        [norm_path] + (target.args or []),
                        creationflags=flags,
                        close_fds=True
                    )
                    _bring_to_foreground_async(display)
                    return LaunchResult(
                        status="success",
                        mode="executable",
                        message=f"Opening {display}, Satyam.",
                        target=norm_path
                    )
                elif not os.path.isabs(norm_path):
                    # System PATH executable like notepad.exe or calc.exe
                    os.startfile(norm_path)
                    _bring_to_foreground_async(display)
                    return LaunchResult(
                        status="success",
                        mode="executable",
                        message=f"Opening {display}, Satyam.",
                        target=norm_path
                    )

            # If it's a shortcut (.lnk)
            if target.launch_mode == "shortcut" or norm_path.lower().endswith(".lnk"):
                if os.path.exists(norm_path):
                    os.startfile(norm_path)
                    _bring_to_foreground_async(display)
                    return LaunchResult(
                        status="success",
                        mode="shortcut",
                        message=f"Opening {display}, Satyam.",
                        target=norm_path
                    )

            # Try os.startfile as fallback
            os.startfile(norm_path)
            _bring_to_foreground_async(display)
            return LaunchResult(
                status="success",
                mode="startfile",
                message=f"Opening {display}, Satyam.",
                target=norm_path
            )

        except Exception as e:
            logger.error(f"Failed to launch app '{display}' at '{path}': {e}")
            return LaunchResult(
                status="error",
                mode=target.launch_mode,
                message=f"Failed to open {display}: {str(e)}",
                target=path,
                error=str(e)
            )

    def launch_by_name(self, app_name: str, browser: Optional[str] = None) -> LaunchResult:
        clean = app_name.strip()
        target = self.resolver.resolve(clean)

        if target:
            return self.launch(target)

        # Fallback: Search on Google if no application could be found
        search_url = f"https://www.google.com/search?q={clean}"
        webbrowser.open(search_url)
        return LaunchResult(
            status="success",
            mode="web_fallback",
            message=f"Could not locate '{clean}' locally; searching on web.",
            target=search_url
        )
