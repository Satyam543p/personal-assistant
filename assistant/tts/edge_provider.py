import os
import sys
import time
import logging
import tempfile
import asyncio
import threading
from typing import Callable, Any

from assistant.tts.base import TTSProvider

logger = logging.getLogger("kate.tts.edge")


class EdgeTTSProvider(TTSProvider):
    """
    Microsoft Edge Neural TTS Provider.
    High-quality free cloud neural voices (e.g. en-US-AvaMultilingualNeural, en-IN-NeerjaNeural).
    """

    def __init__(self, default_voice: str = "en-US-AvaMultilingualNeural"):
        self.default_voice = default_voice
        self._is_speaking = False
        self._stop_requested = False
        self._current_tmp_file: str | None = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "edge"

    @property
    def is_available(self) -> bool:
        try:
            import edge_tts
            import pygame
            return True
        except ImportError:
            return False

    def select_voice(self, text: str) -> str:
        import re
        hindi_words = {
            "karo", "kardo", "kaise", "kya", "batao", "dhundo", "chalao", "sunao",
            "kholo", "dikhao", "hai", "hain", "mera", "meri", "namaste", "suno", "mein"
        }
        tokens = set(re.findall(r"\b\w+\b", text.lower()))
        if len(tokens.intersection(hindi_words)) >= 1 or re.search(r"[\u0900-\u097F]", text):
            return "en-IN-NeerjaNeural"
        return self.default_voice

    def speak(
        self,
        text: str,
        on_done_callback: Callable[[], Any] | None = None,
        voice: str | None = None
    ) -> None:
        if not text:
            if on_done_callback:
                on_done_callback()
            return

        self.stop()
        self._stop_requested = False

        threading.Thread(
            target=self._run_speech,
            args=(text, on_done_callback, voice),
            daemon=True,
            name="EdgeTTSWorker"
        ).start()

    def _run_speech(self, text: str, on_done_callback: Callable[[], Any] | None, voice: str | None):
        import edge_tts
        import pygame

        self._is_speaking = True
        chosen_voice = voice or self.select_voice(text)

        async def _gen():
            communicate = edge_tts.Communicate(text, voice=chosen_voice, rate="+5%", pitch="+0Hz")
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir=tempfile.gettempdir())
            tmp.close()
            await communicate.save(tmp.name)
            return tmp.name

        tmp_path = None
        try:
            loop = asyncio.new_event_loop()
            tmp_path = loop.run_until_complete(_gen())
            loop.close()

            if self._stop_requested:
                return

            with self._lock:
                self._current_tmp_file = tmp_path

            if not pygame.mixer.get_init():
                try:
                    pygame.init()
                    pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
                except Exception:
                    if not pygame.mixer.get_init():
                        pygame.mixer.init()

            pygame.mixer.music.load(tmp_path)
            pygame.mixer.music.play()

            while pygame.mixer.get_init() and pygame.mixer.music.get_busy() and not self._stop_requested:
                time.sleep(0.04)

        except Exception as e:
            logger.warning(f"EdgeTTS playback error: {e}")
        finally:
            self._is_speaking = False
            try:
                if pygame.mixer.get_init():
                    pygame.mixer.music.stop()
                    pygame.mixer.music.unload()
            except Exception:
                pass

            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

            with self._lock:
                if self._current_tmp_file == tmp_path:
                    self._current_tmp_file = None

            if on_done_callback and not self._stop_requested:
                try:
                    on_done_callback()
                except Exception as e:
                    logger.error(f"Error in on_done_callback: {e}")

    def stop(self) -> None:
        self._stop_requested = True
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
        except Exception:
            pass
