"""
Abstract Interfaces for Application Resolution and Launching.
Adheres to the "Abstractions & Interfaces First" principle.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, Any, List


@dataclass
class AppTarget:
    name: str
    display_name: str
    target_path: str  # Executable path, shell URI, or web fallback URL
    launch_mode: str  # "executable", "shortcut", "shell_uwp", "url"
    args: Optional[List[str]] = None
    confidence: float = 1.0


@dataclass
class LaunchResult:
    status: str  # "success" or "error"
    mode: str
    message: str
    target: str
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "status": self.status,
            "mode": self.mode,
            "message": self.message,
            "response": self.message,
            "target": self.target
        }
        if self.error:
            d["error"] = self.error
        return d


class IAppResolver(ABC):
    """Abstract interface for discovering and resolving application names to executable targets."""

    @abstractmethod
    def resolve(self, query: str) -> Optional[AppTarget]:
        """Resolves a raw query string to a launchable AppTarget."""
        pass

    @abstractmethod
    def refresh_cache(self) -> None:
        """Refreshes any indexed application caches."""
        pass


class IAppLauncher(ABC):
    """Abstract interface for launching application targets reliably."""

    @abstractmethod
    def launch(self, target: AppTarget) -> LaunchResult:
        """Launches the resolved AppTarget."""
        pass

    @abstractmethod
    def launch_by_name(self, app_name: str, browser: Optional[str] = None) -> LaunchResult:
        """Resolves and launches an application by name directly."""
        pass
