"""
Application Launcher Package.
"""
from .base import IAppResolver, IAppLauncher, AppTarget, LaunchResult
from .windows import WindowsAppResolver, WindowsAppLauncher

# Singleton default instance
app_launcher = WindowsAppLauncher()

__all__ = [
    "IAppResolver",
    "IAppLauncher",
    "AppTarget",
    "LaunchResult",
    "WindowsAppResolver",
    "WindowsAppLauncher",
    "app_launcher"
]
