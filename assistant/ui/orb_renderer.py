"""
Orb Renderer Abstractions for Kate Assistant UI.
Defines clean contracts for UI rendering, animation states, and audio reactivity.
"""
from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional


class OrbState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    WAITING_PERMISSION = "waiting_permission"


class IOrbRenderer(ABC):
    """Abstract interface defining the visual lifecycle and reactivity of Kate's Orb."""

    @abstractmethod
    def set_state(self, state: str) -> None:
        """Sets current animation state ('idle', 'listening', 'thinking', 'speaking', 'waiting_permission')."""
        pass

    @abstractmethod
    def set_audio_energy(self, level: float) -> None:
        """Passes real-time microphone or speaker audio amplitude level (0.0 to 1.0)."""
        pass

    @abstractmethod
    def trigger_click_feedback(self) -> None:
        """Triggers a tactile ripple / bloom shockwave on click."""
        pass
