import logging
import os
import threading
import time
from PyQt6.QtCore import QObject, pyqtSignal, QMetaObject, Qt

logger = logging.getLogger("jarvis.ui.hotkey")


class HotkeyService(QObject):
    """
    Robust triple-redundancy global hotkey listener that emits a Qt signal
    whenever the user presses a Siri activation hotkey:
      - Ctrl + Space
      - Alt + Space
      - Ctrl + Alt + J
      - Ctrl + Shift + J
      - Ctrl + Shift + Space
      - F9 (Instant single-key toggle)

    Layers:
      1. Direct Windows keyboard hook (keyboard module)
      2. Hardware-level GetAsyncKeyState polling loop (pywin32)
      3. Secondary pynput GlobalHotKeys listener
    """
    triggered = pyqtSignal()

    def __init__(self, hotkey_str: str = "ctrl+space", parent=None):
        super().__init__(parent)
        self.hotkey_str = hotkey_str
        self._running = False
        self._target_window = None
        self._hotkey_listener = None
        self._poll_thread = None
        self._last_trigger_time = 0.0

    def set_window(self, window):
        """Store a direct reference to SiriWindow for safe cross-thread invocation."""
        self._target_window = window

    def start(self):
        if self._running:
            return
        self._running = True

        # Layer 1: Windows low-level keyboard hook (keyboard module)
        try:
            import keyboard
            for hk in ['ctrl+space', 'alt+space', 'ctrl+shift+space', 'ctrl+alt+j', 'ctrl+shift+j', 'f9']:
                try:
                    keyboard.add_hotkey(hk, self._on_hotkey, suppress=False)
                except Exception:
                    pass
            logger.info("Layer 1 keyboard hooks registered.")
        except Exception as e:
            logger.debug(f"Layer 1 hook init: {e}")

        # Layer 2: Hardware GetAsyncKeyState poller (always runs, zero hook conflicts)
        self._poll_thread = threading.Thread(
            target=self._async_keystate_worker,
            daemon=True,
            name="KateHardwareHotkeyPoller"
        )
        self._poll_thread.start()
        logger.info("Layer 2 Hardware hotkey poller started [Ctrl+Space, Alt+Space, Ctrl+Alt+J, Ctrl+Shift+J, F9].")

        # Layer 3: Secondary pynput GlobalHotKeys
        try:
            from pynput import keyboard as pynput_kb

            hotkey_map = {
                '<ctrl>+<space>': self._on_hotkey,
                '<alt>+<space>': self._on_hotkey,
                '<ctrl>+<shift>+<space>': self._on_hotkey,
                '<ctrl>+<alt>+j': self._on_hotkey,
                '<ctrl>+<shift>+j': self._on_hotkey,
            }
            self._hotkey_listener = pynput_kb.GlobalHotKeys(hotkey_map)
            self._hotkey_listener.daemon = True
            self._hotkey_listener.start()
            logger.info("Layer 3 secondary pynput GlobalHotKeys active.")
        except Exception as e:
            logger.debug(f"Layer 3 pynput listener: {e}")

    def stop(self):
        self._running = False
        try:
            import keyboard
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        if self._hotkey_listener:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass

    def _async_keystate_worker(self):
        """Hardware-level keystate polling attached to the interactive Windows Desktop."""
        if os.name == "nt":
            try:
                import win32service
                import win32con
                import ctypes
                hdesk = win32service.OpenDesktop("Default", 0, False, win32con.MAXIMUM_ALLOWED)
                if hdesk:
                    ctypes.windll.user32.SetThreadDesktop(int(hdesk))
            except Exception:
                pass

        try:
            import win32api
            import win32con

            was_pressed = False
            while self._running:
                ctrl = (win32api.GetAsyncKeyState(win32con.VK_CONTROL) & 0x8000) != 0
                alt = (win32api.GetAsyncKeyState(win32con.VK_MENU) & 0x8000) != 0
                shift = (win32api.GetAsyncKeyState(win32con.VK_SHIFT) & 0x8000) != 0
                space = (win32api.GetAsyncKeyState(win32con.VK_SPACE) & 0x8000) != 0
                j_key = (win32api.GetAsyncKeyState(0x4A) & 0x8000) != 0       # 'J'
                f9_key = (win32api.GetAsyncKeyState(win32con.VK_F9) & 0x8000) != 0  # F9

                combo_active = (
                    (ctrl and space) or
                    (alt and space) or
                    (ctrl and alt and j_key) or
                    (ctrl and shift and j_key) or
                    (ctrl and shift and space) or
                    f9_key
                )

                if combo_active and not was_pressed:
                    was_pressed = True
                    self._on_hotkey()
                    time.sleep(0.15)
                elif not combo_active:
                    was_pressed = False

                time.sleep(0.025)
        except Exception as e:
            logger.error(f"Error in hardware hotkey poller: {e}")

    def _on_hotkey(self):
        """Thread-safe dispatch to Qt main event loop with 500ms debounce."""
        now = time.time()
        if (now - self._last_trigger_time) < 0.5:
            return
        self._last_trigger_time = now

        logger.info("Global hotkey triggered (debounced).")
        self.triggered.emit()
