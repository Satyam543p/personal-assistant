import os
import sys
import time
import logging
import subprocess

# Ensure workspace root is in sys.path
_ws_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ws_root not in sys.path:
    sys.path.insert(0, _ws_root)

# Force UTF-8 output on Windows to avoid UnicodeEncodeError with emoji
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor
from PyQt6.QtCore import Qt

try:
    from assistant.ui.siri_window import SiriWindow
    from assistant.ui.hotkey_service import HotkeyService
    from assistant.ui.settings_window import SettingsDialog
    from assistant.voice_wake import WakeWordListener
    from assistant.ipc import HTTPIPCClient
    import assistant.config as config
except ModuleNotFoundError:
    from ui.siri_window import SiriWindow
    from ui.hotkey_service import HotkeyService
    from ui.settings_window import SettingsDialog
    from voice_wake import WakeWordListener
    from ipc import HTTPIPCClient
    import config

logger = logging.getLogger("jarvis.ui.app")


def create_tray_icon() -> QIcon:
    """Generates a sleek glowing circular tray icon programmatically."""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    painter.setBrush(QColor(0, 245, 212))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(2, 2, 28, 28)

    painter.setBrush(QColor(142, 68, 245))
    painter.drawEllipse(7, 7, 18, 18)

    painter.end()
    return QIcon(pixmap)


def ensure_daemon_running():
    """Checks if Kate daemon is responding; if not, starts it."""
    client = HTTPIPCClient()
    code, _ = client.get_health()
    if code == 200:
        print("[Kate] Connected to existing daemon.")
        return

    # If running as frozen standalone .exe, spin up daemon in a background thread
    if getattr(sys, "frozen", False):
        import threading
        print("[Kate] Starting embedded daemon thread in background...")
        def _bg_daemon():
            try:
                import asyncio
                from assistant.daemon import JarvisDaemon
                daemon = JarvisDaemon()
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(daemon.start())
                loop.run_forever()
            except Exception as e:
                logger.error(f"Embedded daemon error: {e}")

        t = threading.Thread(target=_bg_daemon, daemon=True, name="KateEmbeddedDaemon")
        t.start()

        for _ in range(25):
            time.sleep(0.2)
            code, _ = client.get_health()
            if code == 200:
                print("[Kate] Embedded daemon successfully initialized & online.")
                return
        return

    print("[Kate] Starting daemon in background...")
    daemon_script = os.path.join(config.BASE_DIR, "daemon.py")
    subprocess.Popen(
        [sys.executable, daemon_script],
        cwd=config.WORKSPACE_ROOT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    )

    # Wait up to 5 seconds for daemon to initialize
    for _ in range(25):
        time.sleep(0.2)
        code, _ = client.get_health()
        if code == 200:
            print("[Kate] Daemon successfully started & online.")
            return

    print("[Kate] Warning: Daemon did not respond within 5s. Running in standalone mode.")


_single_instance_mutex = None


def run_siri_app():
    global _single_instance_mutex
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Prevent duplicate UI instances from running concurrently
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            ERROR_ALREADY_EXISTS = 183
            _single_instance_mutex = kernel32.CreateMutexW(None, False, "Local\\KateAssistantSingleInstanceMutex")
            if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                print("[Kate] Another Kate Assistant UI instance is already running. Exiting cleanly.")
                sys.exit(0)
        except Exception as e:
            logger.warning(f"Failed to acquire single-instance mutex: {e}")

    # Attach thread to interactive user desktop ("Default")
    if os.name == "nt":
        try:
            import ctypes
            import win32service
            import win32con
            hdesk = win32service.OpenDesktop("Default", 0, False, win32con.MAXIMUM_ALLOWED)
            if hdesk:
                ctypes.windll.user32.SetThreadDesktop(int(hdesk))
        except Exception:
            pass

    # 1. Ensure Daemon is alive
    ensure_daemon_running()

    # 2. Qt Application setup
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # 3. Create Siri Floating Window
    siri_window = SiriWindow()

    # 4. Global Hotkey Service
    hotkey = getattr(config, "SIRI_HOTKEY", "ctrl+space")
    hotkey_service = HotkeyService(hotkey_str=hotkey)
    hotkey_service.set_window(siri_window)          # thread-safe dispatch target
    hotkey_service.triggered.connect(siri_window.toggle_visibility)
    hotkey_service.start()

    # 5. Continuous Wake-Word Listener ("Hey Kate" / "Hey Jarvis")
    wake_listener = WakeWordListener(target_window=siri_window)
    wake_listener.wake_word_detected.connect(siri_window.summon_voice)
    wake_listener.wake_command_detected.connect(siri_window.summon_with_command)
    siri_window.set_wake_listener(wake_listener)
    wake_listener.start()

    # 6. System Tray Icon
    tray_icon = QSystemTrayIcon(create_tray_icon(), app)
    tray_icon.setToolTip(f"Kate Assistant ({hotkey.upper()} or 'Hey Kate')")

    tray_menu = QMenu()
    act_summon = tray_menu.addAction(f"✨ Summon Kate ({hotkey.upper()})")
    act_summon.triggered.connect(siri_window.summon)

    act_settings = tray_menu.addAction("⚙️ Kate Settings...")
    def _open_settings():
        dlg = SettingsDialog()
        dlg.exec()
    act_settings.triggered.connect(_open_settings)

    tray_menu.addSeparator()
    act_status = tray_menu.addAction("● Daemon: Connected")
    act_status.setEnabled(False)

    tray_menu.addSeparator()
    act_exit = tray_menu.addAction("Quit Kate Assistant")
    act_exit.triggered.connect(lambda: (wake_listener.stop(), hotkey_service.stop(), app.quit()))

    tray_icon.setContextMenu(tray_menu)
    tray_icon.activated.connect(
        lambda reason: siri_window.toggle_visibility()
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick)
        else None
    )
    tray_icon.show()

    print(f"\n==========================================================")
    print(f"  ✨ Kate Assistant Interface Active!")
    print(f"  Summon with ANY of:")
    print(f"    • Say 'Hey Kate' or 'Kate'")
    print(f"    • Ctrl + Space / Alt + Space")
    print(f"    • Click the glowing tray icon near Windows clock")
    print(f"  Press [Esc] to dismiss.")
    print(f"==========================================================\n", flush=True)

    # Show initial welcome capsule ready on screen
    siri_window.summon(start_voice=False)

    sys.exit(app.exec())


if __name__ == "__main__":
    run_siri_app()
