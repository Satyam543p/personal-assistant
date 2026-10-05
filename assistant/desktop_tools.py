"""
Desktop Capabilities Suite for Kate.
Provides:
  - Window & Screen Control (switch window, minimize all, maximize, close window)
  - Full-screen & Active-window Screenshot capture
  - Master Audio Volume & Media Playback Control (play/pause/stop)
  - Workstation Lock & Power Safety Guards
  - Smart Folder Organizer (Downloads / Desktop sorting)
  - Automated YouTube Song Downloader (yt-dlp + ffmpeg)
  - PDF Document Generation (ReportLab)
"""

import os
import sys
import time
import logging
import asyncio
import subprocess
import shutil

try:
    from assistant.tools import Tool
except ModuleNotFoundError:
    from tools import Tool

logger = logging.getLogger("kate.desktop")


# =====================================================================
# 1. Window Control Suite
# =====================================================================

class WindowControl:
    @staticmethod
    def switch_to_window(title_query: str) -> dict:
        """Finds window by title and brings it to the absolute foreground."""
        try:
            import win32gui
            import win32con
            import win32process

            found_hwnd = None
            title_query_lower = title_query.lower()

            def enum_cb(hwnd, _):
                nonlocal found_hwnd
                if win32gui.IsWindowVisible(hwnd):
                    txt = win32gui.GetWindowText(hwnd)
                    if txt and title_query_lower in txt.lower():
                        found_hwnd = hwnd
                        return False
                return True

            try:
                win32gui.EnumWindows(enum_cb, None)
            except Exception:
                pass  # EnumWindows throws when returning False to stop

            if found_hwnd:
                win32gui.ShowWindow(found_hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(found_hwnd)
                window_title = win32gui.GetWindowText(found_hwnd)
                return {
                    "status": "success",
                    "message": f"Switched to '{window_title}', Satyam.",
                    "hwnd": found_hwnd
                }

            return {
                "status": "failure",
                "message": f"I couldn't find an open window matching '{title_query}', Satyam."
            }
        except Exception as e:
            return {"status": "error", "message": f"Window switch error: {e}"}

    @staticmethod
    def minimize_all() -> dict:
        """Minimizes all windows to reveal the clean desktop."""
        try:
            import win32gui
            import win32con
            # Shell Minimize All via COM / Win32
            import ctypes
            # Win+D shortcut equivalent
            user32 = ctypes.windll.user32
            user32.keybd_event(0x5B, 0, 0, 0)        # Win down
            user32.keybd_event(0x44, 0, 0, 0)        # D down
            user32.keybd_event(0x44, 0, 2, 0)        # D up
            user32.keybd_event(0x5B, 0, 2, 0)        # Win up
            return {"status": "success", "message": "Showing your desktop, Satyam."}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @staticmethod
    def close_active_window() -> dict:
        """Sends WM_CLOSE to the currently active foreground window."""
        try:
            import win32gui
            import win32con
            hwnd = win32gui.GetForegroundWindow()
            if hwnd:
                title = win32gui.GetWindowText(hwnd)
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                return {"status": "success", "message": f"Closed '{title}', Satyam."}
            return {"status": "failure", "message": "No active window found."}
        except Exception as e:
            return {"status": "error", "message": str(e)}


# =====================================================================
# 2. Screenshot Tool
# =====================================================================

class ScreenshotTool:
    @staticmethod
    def capture() -> dict:
        """Captures full screen, saves PNG to Desktop/Screenshots, and copies to clipboard."""
        try:
            try:
                import ctypes
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

            from PIL import ImageGrab
            import io

            desktop = os.path.expanduser(r"~\Desktop")
            shot_dir = os.path.join(desktop, "Screenshots")
            os.makedirs(shot_dir, exist_ok=True)

            filename = f"screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png"
            file_path = os.path.join(shot_dir, filename)

            img = None
            try:
                img = ImageGrab.grab(all_screens=True)
            except Exception:
                try:
                    img = ImageGrab.grab()
                except Exception:
                    pass

            if img is None:
                # Direct Win32 GDI screen grab fallback
                try:
                    import win32gui, win32ui, win32con
                    from PIL import Image
                    hwin = win32gui.GetDesktopWindow()
                    w = win32gui.GetSystemMetrics(win32con.SM_CXSCREEN)
                    h = win32gui.GetSystemMetrics(win32con.SM_CYSCREEN)
                    hwindc = win32gui.GetWindowDC(hwin)
                    srcdc = win32ui.CreateDCFromHandle(hwindc)
                    memdc = srcdc.CreateCompatibleDC()
                    bmp = win32ui.CreateBitmap()
                    bmp.CreateCompatibleBitmap(srcdc, w, h)
                    memdc.SelectObject(bmp)
                    memdc.BitBlt((0, 0), (w, h), srcdc, (0, 0), win32con.SRCCOPY)
                    bmpinfo = bmp.GetInfo()
                    bmpstr = bmp.GetBitmapBits(True)
                    img = Image.frombuffer('RGB', (bmpinfo['bmWidth'], bmpinfo['bmHeight']), bmpstr, 'raw', 'BGRX', 0, 1)
                    win32gui.DeleteObject(bmp.GetHandle())
                    memdc.DeleteDC()
                    srcdc.DeleteDC()
                    win32gui.ReleaseDC(hwin, hwindc)
                except Exception as ex2:
                    logger.debug(f"DC capture fallback error: {ex2}")

            if img is None:
                # PowerShell .NET CopyFromScreen fallback
                try:
                    escaped_path = file_path.replace("\\", "\\\\")
                    ps_script = (
                        "Add-Type -AssemblyName System.Windows.Forms; "
                        "Add-Type -AssemblyName System.Drawing; "
                        "$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
                        "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
                        "$g = [System.Drawing.Graphics]::FromImage($bmp); "
                        "$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size); "
                        f"$bmp.Save('{escaped_path}', [System.Drawing.Imaging.ImageFormat]::Png); "
                        "$g.Dispose(); $bmp.Dispose();"
                    )
                    subprocess.run(
                        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                        check=True,
                        capture_output=True,
                        timeout=5
                    )
                    if os.path.exists(file_path) and os.path.getsize(file_path) > 100:
                        msg = "Screenshot captured and saved to your Desktop/Screenshots folder, Satyam."
                        return {
                            "status": "success",
                            "file_path": file_path,
                            "message": msg,
                            "response": msg
                        }
                except Exception as ps_err:
                    logger.debug(f"PowerShell screenshot fallback error: {ps_err}")

            if img is None:
                raise RuntimeError("Could not capture desktop screen via PIL, Win32 GDI, or PowerShell.")

            img.save(file_path, "PNG")

            # Copy to Windows clipboard
            try:
                import win32clipboard
                output = io.BytesIO()
                img.convert("RGB").save(output, "BMP")
                data = output.getvalue()[14:]  # Skip BMP header
                output.close()
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
                win32clipboard.CloseClipboard()
            except Exception:
                pass

            msg = "Screenshot captured and saved to your Desktop/Screenshots folder, Satyam."
            return {
                "status": "success",
                "file_path": file_path,
                "message": msg,
                "response": msg
            }
        except Exception as e:
            return {"status": "error", "message": f"Failed to take screenshot: {e}", "response": f"Failed to take screenshot: {e}"}


# =====================================================================
# 3. Audio & Media Controls
# =====================================================================

class AudioMediaControl:
    VK_MEDIA_PLAY_PAUSE = 0xCD
    VK_MEDIA_STOP = 0xB2
    VK_MEDIA_NEXT_TRACK = 0xB0
    VK_MEDIA_PREV_TRACK = 0xB1
    VK_VOLUME_MUTE = 0xAD
    VK_VOLUME_DOWN = 0xAE
    VK_VOLUME_UP = 0xAF

    @classmethod
    def send_media_key(cls, key_code: int):
        import win32api
        import win32con
        win32api.keybd_event(key_code, 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(key_code, 0, win32con.KEYEVENTF_KEYUP, 0)

    @classmethod
    def play_pause(cls) -> dict:
        cls.send_media_key(cls.VK_MEDIA_PLAY_PAUSE)
        return {"status": "success", "message": "Toggled playback, Satyam."}

    @classmethod
    def stop(cls) -> dict:
        cls.send_media_key(cls.VK_MEDIA_STOP)
        return {"status": "success", "message": "Stopped media playback, Satyam."}

    @classmethod
    def toggle_mute(cls) -> dict:
        cls.send_media_key(cls.VK_VOLUME_MUTE)
        return {"status": "success", "message": "Toggled mute, Satyam."}

    @classmethod
    def volume_step(cls, direction: str = "up", steps: int = 5) -> dict:
        key = cls.VK_VOLUME_UP if direction == "up" else cls.VK_VOLUME_DOWN
        for _ in range(steps):
            cls.send_media_key(key)
            time.sleep(0.02)
        return {"status": "success", "message": f"Turned volume {direction}, Satyam."}


# =====================================================================
# 4. System & Hardware Controls
# =====================================================================

class SystemControl:
    @staticmethod
    def lock_pc() -> dict:
        """Locks the workstation instantly."""
        try:
            import ctypes
            ctypes.windll.user32.LockWorkStation()
            return {"status": "success", "message": "Locking your PC, Satyam."}
        except Exception as e:
            return {"status": "error", "message": f"Lock failed: {e}"}

    @staticmethod
    def sleep_pc() -> dict:
        """Puts Windows into sleep state."""
        try:
            import ctypes
            # SetSuspendState(0, 0, 0) -> Standby/Sleep
            ctypes.windll.PowrProf.SetSuspendState(0, 0, 0)
            return {"status": "success", "message": "Putting your laptop to sleep, Satyam."}
        except Exception as e:
            return {"status": "error", "message": f"Sleep failed: {e}"}


# =====================================================================
# 5. Smart Folder Organizer
# =====================================================================

class FolderOrganizer:
    CATEGORIES = {
        "Images": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"],
        "Documents": [".pdf", ".docx", ".doc", ".txt", ".xlsx", ".pptx", ".csv"],
        "Archives": [".zip", ".rar", ".7z", ".tar", ".gz"],
        "Installers": [".exe", ".msi", ".bat"],
        "Code": [".py", ".js", ".ts", ".html", ".css", ".json", ".sql", ".cpp"]
    }

    @classmethod
    def organize(cls, folder_path: str = None) -> dict:
        """Sorts unorganized files in Downloads or Desktop into neat category subfolders."""
        target = folder_path or os.path.expanduser(r"~\Downloads")
        if not os.path.exists(target):
            return {"status": "failure", "message": f"Folder {target} does not exist."}

        moved_count = 0
        try:
            for item in os.listdir(target):
                item_path = os.path.join(target, item)
                if os.path.isfile(item_path):
                    _, ext = os.path.splitext(item)
                    ext_lower = ext.lower()

                    for cat, ext_list in cls.CATEGORIES.items():
                        if ext_lower in ext_list:
                            cat_dir = os.path.join(target, cat)
                            os.makedirs(cat_dir, exist_ok=True)
                            dest_path = os.path.join(cat_dir, item)
                            # Avoid overwrite
                            if not os.path.exists(dest_path):
                                shutil.move(item_path, dest_path)
                                moved_count += 1
                            break

            return {
                "status": "success",
                "moved_files": moved_count,
                "message": f"Organized {moved_count} files in your Downloads folder into neat categories, Satyam."
            }
        except Exception as e:
            return {"status": "error", "message": f"Organization error: {e}"}


# =====================================================================
# 6. Automated YouTube Music Downloader
# =====================================================================

class SongDownloader:
    @staticmethod
    async def download_song(song_query: str) -> dict:
        """
        Searches YouTube for song query, extracts 320kbps MP3 via yt-dlp + ffmpeg,
        and saves it to user's Music or Downloads folder.
        """
        try:
            from assistant.youtube import ScrapeOrFallbackYouTubeProvider
            from assistant.media_tools import JarvisMediaExtractor

            # 1. Search YouTube
            provider = ScrapeOrFallbackYouTubeProvider()
            results = await provider.search(song_query, max_results=1)
            if not results:
                return {
                    "status": "failure",
                    "message": f"I couldn't locate '{song_query}' on YouTube, Satyam."
                }

            top_video = results[0]
            video_url = top_video.url
            video_title = top_video.title

            # 2. Extract audio via yt-dlp
            extractor = JarvisMediaExtractor()
            music_dir = os.path.expanduser(r"~\Music")
            if not os.path.exists(music_dir):
                music_dir = os.path.expanduser(r"~\Downloads")

            res = await extractor.extract_audio(video_url, output_format="mp3")
            if res.get("status") == "success":
                out_path = res.get("audio_path")
                # Move to Music if not already there
                if out_path and os.path.exists(out_path):
                    final_dest = os.path.join(music_dir, os.path.basename(out_path))
                    if out_path != final_dest and not os.path.exists(final_dest):
                        try:
                            shutil.move(out_path, final_dest)
                            out_path = final_dest
                        except Exception:
                            pass

                return {
                    "status": "success",
                    "title": video_title,
                    "audio_path": out_path,
                    "message": f"I've downloaded '{video_title}' to your Music folder, Satyam."
                }
            else:
                return {
                    "status": "failure",
                    "message": f"Download encountered an issue: {res.get('message', 'unknown error')}"
                }
        except Exception as e:
            return {"status": "error", "message": f"Song download error: {e}"}


# =====================================================================
# 7. ReportLab PDF Document Generator
# =====================================================================

class PDFReportGenerator:
    @staticmethod
    def create_pdf(title: str, content: str, filename: str = None) -> dict:
        """Compiles formatted PDF document saved to Desktop."""
        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.pdfgen import canvas

            desktop = os.path.expanduser(r"~\Desktop")
            fname = filename or f"{title.replace(' ', '_').lower()[:30]}_{int(time.time())}.pdf"
            file_path = os.path.join(desktop, fname)

            c = canvas.Canvas(file_path, pagesize=letter)
            width, height = letter

            # Header
            c.setFont("Helvetica-Bold", 18)
            c.drawString(54, height - 54, title)

            c.setStrokeColorRGB(0.5, 0.5, 0.5)
            c.setLineWidth(1)
            c.line(54, height - 64, width - 54, height - 64)

            # Body text
            c.setFont("Helvetica", 11)
            y = height - 90
            lines = content.split("\n")
            for line in lines:
                if y < 54:
                    c.showPage()
                    c.setFont("Helvetica", 11)
                    y = height - 54
                c.drawString(54, y, line[:100])
                y -= 16

            c.save()
            return {
                "status": "success",
                "file_path": file_path,
                "message": f"I've compiled your research PDF '{fname}' and placed it on your Desktop, Satyam."
            }
        except Exception as e:
            return {"status": "error", "message": f"PDF creation failed: {e}"}


# =====================================================================
# 8. Media Launcher (User-Directed Music Platform & YouTube Default)
# =====================================================================

class MediaLauncher:
    @staticmethod
    def play_media(title: str, platform: str = "youtube", browser: str = "brave") -> dict:
        """
        Plays requested song, music, or video on user-directed platform.
        Defaults directly to YouTube (in Brave browser) if platform is unspecified.
        Supports Spotify, Soundcloud, Apple Music, and YouTube.
        """
        import urllib.parse
        import webbrowser
        title_clean = title.strip()
        platform_lower = (platform or "youtube").lower().strip()
        browser_lower = (browser or "brave").lower().strip()

        # Fallback browser location check (Brave on Windows)
        brave_path = os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe")
        has_brave = os.path.exists(brave_path)

        def _launch_url(url: str) -> str:
            if ("brave" in browser_lower or (not browser and has_brave)) and has_brave:
                try:
                    subprocess.Popen([brave_path, url])
                    return "Brave"
                except Exception:
                    pass
            webbrowser.open(url)
            return "your browser"

        # Platform 1: Spotify
        if "spotify" in platform_lower:
            try:
                encoded = urllib.parse.quote(title_clean)
                spotify_uri = f"spotify:search:{encoded}"
                subprocess.Popen(["cmd", "/c", "start", spotify_uri], shell=True)
                return {
                    "status": "success",
                    "mode": "spotify_app",
                    "message": f"Playing '{title_clean.title()}' on Spotify, Satyam!",
                    "target": spotify_uri
                }
            except Exception:
                web_url = f"https://open.spotify.com/search/{urllib.parse.quote(title_clean)}"
                used_browser = _launch_url(web_url)
                return {
                    "status": "success",
                    "mode": "spotify_web",
                    "message": f"Playing '{title_clean.title()}' on Spotify in {used_browser}, Satyam!",
                    "target": web_url
                }

        # Platform 2: Soundcloud
        elif "soundcloud" in platform_lower:
            web_url = f"https://soundcloud.com/search?q={urllib.parse.quote(title_clean)}"
            used_browser = _launch_url(web_url)
            return {
                "status": "success",
                "mode": "soundcloud",
                "message": f"Playing '{title_clean.title()}' on SoundCloud in {used_browser}, Satyam!",
                "target": web_url
            }

        # Platform 3: Default directly to YouTube
        else:
            yt_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(title_clean)}"
            used_browser = _launch_url(yt_url)
            return {
                "status": "success",
                "mode": "youtube",
                "message": f"Playing '{title_clean.title()}' on YouTube in {used_browser}, Satyam!",
                "response": f"Playing '{title_clean.title()}' on YouTube in {used_browser}, Satyam!",
                "target": yt_url
            }


# =====================================================================
# Formal Tool Subclasses for JarvisToolExecutor
# =====================================================================

class TakeScreenshotTool(Tool):
    def __init__(self):
        declaration = {
            "inputs": {},
            "side_effects": "Captures screenshot to Desktop/Screenshots and clipboard",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("take_screenshot", "reversible", declaration)

    async def execute(self, executor, **kwargs) -> dict:
        return ScreenshotTool.capture()


class ControlVolumeTool(Tool):
    def __init__(self):
        declaration = {
            "inputs": {
                "direction": {"type": "string", "default": "up", "description": "up, down, or mute"},
                "steps": {"type": "integer", "default": 5}
            },
            "side_effects": "Adjusts Windows master volume",
            "timeout_ms": 3000,
            "memory_limit_mb": 20
        }
        super().__init__("control_volume", "reversible", declaration)

    async def execute(self, executor, **kwargs) -> dict:
        direction = kwargs.get("direction", "up").lower()
        steps = int(kwargs.get("steps", 5))
        if direction == "mute":
            res = AudioMediaControl.toggle_mute()
        else:
            res = AudioMediaControl.volume_step(direction, steps)
        res["response"] = res.get("message", "")
        return res


class ShowDesktopTool(Tool):
    def __init__(self):
        declaration = {
            "inputs": {},
            "side_effects": "Minimizes open windows to show the desktop",
            "timeout_ms": 3000,
            "memory_limit_mb": 20
        }
        super().__init__("show_desktop", "reversible", declaration)

    async def execute(self, executor, **kwargs) -> dict:
        res = WindowControl.minimize_all()
        res["response"] = res.get("message", "")
        return res


class SwitchWindowTool(Tool):
    def __init__(self):
        declaration = {
            "inputs": {
                "title_query": {"type": "string", "description": "Title or app name of the target window"}
            },
            "side_effects": "Activates and focuses the matching window",
            "timeout_ms": 3000,
            "memory_limit_mb": 20
        }
        super().__init__("switch_window", "reversible", declaration)

    async def execute(self, executor, **kwargs) -> dict:
        title_query = kwargs.get("title_query", "")
        res = WindowControl.switch_to_window(title_query)
        res["response"] = res.get("message", "")
        return res


class CloseWindowTool(Tool):
    def __init__(self):
        declaration = {
            "inputs": {},
            "side_effects": "Closes the current active window",
            "timeout_ms": 3000,
            "memory_limit_mb": 20
        }
        super().__init__("close_window", "reversible", declaration)

    async def execute(self, executor, **kwargs) -> dict:
        res = WindowControl.close_active_window()
        res["response"] = res.get("message", "")
        return res


class PlayMusicTool(Tool):
    def __init__(self):
        declaration = {
            "inputs": {
                "title": {"type": "string", "description": "Song title or artist to play"},
                "platform": {"type": "string", "default": "youtube", "description": "youtube, spotify, soundcloud"}
            },
            "side_effects": "Plays requested track in browser or app",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("play_music", "reversible", declaration)

    async def execute(self, executor, **kwargs) -> dict:
        title = kwargs.get("title", "")
        platform = kwargs.get("platform", "youtube")
        res = SongPlayer.play_song(title, platform)
        res["response"] = res.get("message", "")
        return res


class WebSearchBrowserTool(Tool):
    def __init__(self):
        declaration = {
            "inputs": {
                "query": {"type": "string", "description": "Search query keywords"},
                "browser": {"type": "string", "default": "brave"}
            },
            "side_effects": "Opens browser searching Google for the given query",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("web_search_browser", "reversible", declaration)

    async def execute(self, executor, **kwargs) -> dict:
        import urllib.parse
        import webbrowser
        query = kwargs.get("query", "").strip()
        browser = kwargs.get("browser", "brave").lower()
        if not query:
            raise ValueError("Query is required for web_search_browser.")

        search_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}"
        brave_path = os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe")
        if "brave" in browser and os.path.exists(brave_path):
            try:
                subprocess.Popen([brave_path, search_url])
                try:
                    from assistant.launchers.windows import _bring_to_foreground_async
                    _bring_to_foreground_async("brave")
                except Exception:
                    pass
                msg = f"Searching for '{query}' in Brave, Satyam."
                return {"status": "success", "response": msg, "message": msg, "url": search_url}
            except Exception:
                pass
        webbrowser.open(search_url)
        msg = f"Searching for '{query}', Satyam."
        return {"status": "success", "response": msg, "message": msg, "url": search_url}


