import os
import time
import logging
import threading
from typing import Callable, Any

from assistant.tts.base import TTSProvider

logger = logging.getLogger("kate.tts.kokoro")


class KokoroTTSProvider(TTSProvider):
    """
    Kokoro-ONNX ultra-low-latency, expressive offline Text-to-Speech provider.
    Runs locally on CPU in ~100-140ms with warm human vocal naturalness.
    """

    def __init__(
        self,
        model_path: str | None = None,
        voices_path: str | None = None,
        default_voice: str = "af_heart"
    ):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.model_path = model_path or os.path.join(base_dir, "models", "kokoro-v1.0.onnx")
        self.voices_path = voices_path or os.path.join(base_dir, "models", "voices-v1.0.bin")
        self.default_voice = default_voice

        self._kokoro = None
        self._load_lock = threading.Lock()
        self._is_speaking = False
        self._stop_requested = False

    @property
    def name(self) -> str:
        return "kokoro"

    @property
    def is_available(self) -> bool:
        if not (os.path.exists(self.model_path) and os.path.exists(self.voices_path)):
            return False
        try:
            import kokoro_onnx
            return True
        except ImportError:
            return False

    def _ensure_loaded(self):
        if self._kokoro is not None:
            return
        with self._load_lock:
            if self._kokoro is None:
                from kokoro_onnx import Kokoro
                t0 = time.time()
                logger.info(f"Initializing Kokoro-ONNX model from {self.model_path}...")
                self._kokoro = Kokoro(self.model_path, self.voices_path)
                logger.info(f"Kokoro-ONNX model ready in {(time.time() - t0)*1000:.1f}ms")

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
            name="KokoroTTSWorker"
        ).start()

    def _run_speech(self, text: str, on_done_callback: Callable[[], Any] | None, voice: str | None):
        try:
            self._is_speaking = True
            self._ensure_loaded()

            if self._stop_requested:
                return

            chosen_voice = voice or self.default_voice
            t0 = time.time()
            samples, sample_rate = self._kokoro.create(
                text,
                voice=chosen_voice,
                speed=1.05,
                lang="en-us"
            )
            gen_ms = (time.time() - t0) * 1000
            logger.info(f"Kokoro synthesized audio in {gen_ms:.1f}ms (voice={chosen_voice})")

            if self._stop_requested:
                return

            self._play_audio(samples, sample_rate)

        except Exception as e:
            logger.warning(f"Kokoro speech generation/playback error: {e}")
        finally:
            self._is_speaking = False
            if on_done_callback and not self._stop_requested:
                try:
                    on_done_callback()
                except Exception as e:
                    logger.error(f"Error in Kokoro on_done_callback: {e}")

    def _play_audio(self, samples, sample_rate: int):
        """Plays audio in-memory without disk I/O using sounddevice."""
        try:
            import sounddevice as sd
            sd.play(samples, sample_rate)
            while sd.get_stream() and sd.get_stream().active and not self._stop_requested:
                time.sleep(0.03)
            if self._stop_requested:
                sd.stop()
        except Exception as e:
            logger.warning(f"Sounddevice playback failed: {e}, attempting pygame fallback")
            self._play_audio_pygame(samples, sample_rate)

    def _play_audio_pygame(self, samples, sample_rate: int):
        """Fallback in-memory playback via pygame if sounddevice has issues."""
        try:
            import pygame
            import numpy as np

            # Convert float32 [-1.0, 1.0] to int16
            audio_int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=sample_rate, size=-16, channels=1)

            sound = pygame.sndarray.make_sound(audio_int16)
            channel = sound.play()
            while channel and channel.get_busy() and not self._stop_requested:
                time.sleep(0.03)
            if self._stop_requested and channel:
                channel.stop()
        except Exception as e:
            logger.error(f"Pygame sound fallback error: {e}")

    def stop(self) -> None:
        self._stop_requested = True
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
