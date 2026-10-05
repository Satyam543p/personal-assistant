import abc
from typing import Callable, Any


class TTSProvider(abc.ABC):
    """
    Abstract interface for Text-to-Speech providers in Kate/Jarvis.
    Follows 'Abstractions & Interfaces First' architecture.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Name of the provider (e.g. 'kokoro', 'edge', 'cartesia')."""
        pass

    @property
    @abc.abstractmethod
    def is_available(self) -> bool:
        """Returns True if the required models/dependencies/keys are loaded and ready."""
        pass

    @abc.abstractmethod
    def speak(
        self,
        text: str,
        on_done_callback: Callable[[], Any] | None = None,
        voice: str | None = None
    ) -> None:
        """
        Asynchronously synthesizes and plays audio for `text`.
        Invokes `on_done_callback` when playback completes.
        Must not block caller or GUI event loop.
        """
        pass

    @abc.abstractmethod
    def stop(self) -> None:
        """Immediately halts any in-progress speech playback."""
        pass
