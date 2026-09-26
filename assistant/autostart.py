r"""
Windows Startup Manager for Kate Assistant.
Enables or disables silent background launch on Windows boot.
Places a silent VBScript launcher in %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup.
"""

import os
import sys
import logging

logger = logging.getLogger("kate.autostart")

STARTUP_FOLDER = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup")
LAUNCHER_NAME = "KateAssistant.vbs"
LAUNCHER_PATH = os.path.join(STARTUP_FOLDER, LAUNCHER_NAME)


def is_autostart_enabled() -> bool:
    """Check if Kate is registered in Windows Startup folder."""
    return os.path.exists(LAUNCHER_PATH)


def enable_autostart() -> bool:
    """Create a silent VBS launcher in Windows Startup folder."""
    try:
        os.makedirs(STARTUP_FOLDER, exist_ok=True)
        
        if getattr(sys, "frozen", False):
            target_exe = sys.executable
            vbs_content = (
                f'Set WshShell = CreateObject("WScript.Shell")\n'
                f'WshShell.Run chr(34) & "{target_exe}" & chr(34), 0\n'
                f'Set WshShell = Nothing\n'
            )
        else:
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            launch_script = os.path.join(repo_root, "launch_siri.py")
            pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(pythonw):
                pythonw = sys.executable

            vbs_content = (
                f'Set WshShell = CreateObject("WScript.Shell")\n'
                f'WshShell.CurrentDirectory = "{repo_root}"\n'
                f'WshShell.Run chr(34) & "{pythonw}" & chr(34) & " " & chr(34) & "{launch_script}" & chr(34), 0\n'
                f'Set WshShell = Nothing\n'
            )

        with open(LAUNCHER_PATH, "w", encoding="utf-8") as f:
            f.write(vbs_content)

        logger.info(f"Kate autostart successfully enabled at: {LAUNCHER_PATH}")
        return True
    except Exception as e:
        logger.error(f"Failed to enable autostart: {e}")
        return False


def disable_autostart() -> bool:
    """Remove Kate launcher from Windows Startup folder."""
    try:
        if os.path.exists(LAUNCHER_PATH):
            os.remove(LAUNCHER_PATH)
            logger.info("Kate autostart disabled.")
        return True
    except Exception as e:
        logger.error(f"Failed to disable autostart: {e}")
        return False
