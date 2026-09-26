"""
Native Windows Desktop Notifications & Audio Subsystem for Jarvis.
Dispatches native Windows 10/11 toast notifications via non-blocking PowerShell
execution and subtle sound cues for background scheduler reminders and alerts.
"""

import abc
import asyncio
import logging
import os
import subprocess
import sys

logger = logging.getLogger("jarvis.notifications")

try:
    from assistant.tools import Tool
except ModuleNotFoundError:
    from tools import Tool


class NotificationService(abc.ABC):
    @abc.abstractmethod
    async def notify(self, title: str, message: str, level: str = "info", sound: bool = True) -> bool:
        """Pushes a native desktop notification and optional audio cue."""
        pass


class WindowsNotificationService(NotificationService):
    def __init__(self):
        self._loop = None

    def _play_sound(self, level: str):
        try:
            if sys.platform == "win32":
                import winsound
                if level == "error":
                    winsound.MessageBeep(winsound.MB_ICONHAND)
                elif level == "warning":
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                else:
                    winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception as e:
            logger.debug(f"Audio cue failed: {e}")

    async def notify(self, title: str, message: str, level: str = "info", sound: bool = True) -> bool:
        logger.info(f"Dispatching notification: [{title}] {message} (level={level})")
        if sound:
            try:
                self._play_sound(level)
            except Exception:
                pass

        # Clean strings for PowerShell execution
        safe_title = title.replace('"', '`"').replace("'", "''")
        safe_msg = message.replace('"', '`"').replace("'", "''")

        # PowerShell script using Windows Runtime Toast API or BurntToast fallback
        ps_script = (
            f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
            f"$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
            f"$textNodes = $template.GetElementsByTagName('text'); "
            f"$textNodes.Item(0).AppendChild($template.CreateTextNode('{safe_title}')) > $null; "
            f"$textNodes.Item(1).AppendChild($template.CreateTextNode('{safe_msg}')) > $null; "
            f"$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Jarvis Assistant'); "
            f"$notification = [Windows.UI.Notifications.ToastNotification]::new($template); "
            f"$notifier.Show($notification);"
        )

        try:
            # Fire and forget non-blocking subprocess
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            return True
        except Exception as e:
            logger.error(f"Failed to trigger PowerShell toast notification: {e}")
            return False


class SendNotificationTool(Tool):
    def __init__(self, notification_service: NotificationService = None):
        declaration = {
            "inputs": {
                "title": {"type": "string", "default": "Jarvis Reminder"},
                "message": {"type": "string"},
                "level": {"type": "string", "default": "info"}
            },
            "side_effects": "Triggers native Windows desktop toast notification and audio cue",
            "timeout_ms": 3000,
            "memory_limit_mb": 20
        }
        super().__init__("send_notification", "reversible", declaration)
        self.notification_service = notification_service or WindowsNotificationService()

    async def execute(self, executor, **kwargs) -> dict:
        title = kwargs.get("title", "Jarvis Assistant")
        message = kwargs.get("message") or kwargs.get("text", "")
        level = kwargs.get("level", "info")

        if not message:
            raise ValueError("Parameter 'message' is required for send_notification.")

        success = await self.notification_service.notify(title, message, level=level)
        return {
            "status": "success" if success else "failure",
            "response": f"Notification '{title}' dispatched to Windows desktop."
        }
