import os
import sys

# Ensure process attaches to the user's interactive Windows Desktop ("Default")
# so GUI windows and global hotkeys are visible and active on the real physical screen.
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

# Add workspace directory to path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from assistant.ui.app import run_siri_app

if __name__ == "__main__":
    run_siri_app()

