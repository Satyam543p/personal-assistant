import re
import os
import logging
from typing import Callable, Any

from assistant.tts.base import TTSProvider
from assistant.tts.kokoro_provider import KokoroTTSProvider
from assistant.tts.edge_provider import EdgeTTSProvider

logger = logging.getLogger("kate.tts.manager")


def clean_text_for_speech(text: str, max_words: int = 35) -> str:
    """
    Transforms detailed assistant output into concise, natural spoken speech.
    - Removes markdown, code blocks, URLs, and noisy bullet formatting.
    - Keeps speech punchy (under max_words) so voice replies start in < 150ms.
    """
    if not text:
        return ""

    # 1. Remove code blocks and inline code
    t = re.sub(r"```[\s\S]*?```", "", text)
    t = re.sub(r"`[^`]*`", "", t)

    # 2. Remove URLs
    t = re.sub(r"https?://\S+", "", t)

    # 3. Clean up bullet lists: if it's a bullet list, take the first 1-2 points
    lines = [line.strip() for line in t.splitlines() if line.strip()]
    bullet_lines = [l for l in lines if l.startswith(("-", "*", "•"))]
    if bullet_lines:
        # Take at most the first two bullets
        spoken_bullets = [re.sub(r"^[-*•]\s*", "", b) for b in bullet_lines[:2]]
        t = ". ".join(spoken_bullets)
    else:
        # Join regular lines
        t = " ".join(lines)

    # 4. Remove Markdown bold/italic/links
    t = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", t)
    t = re.sub(r"[*_~#]+", " ", t)

    # 5. Clean up whitespace
    t = re.sub(r"\s+", " ", t).strip()

    # 6. Sentence limit: Take the first 1-2 sentences up to max_words
    sentences = re.split(r"(?<=[.!?])\s+", t)
    selected = []
    word_count = 0
    for s in sentences:
        words = s.split()
        if not words:
            continue
        if word_count + len(words) <= max_words or not selected:
            selected.append(s)
            word_count += len(words)
        else:
            break

    result = " ".join(selected).strip()
    return result or t[:150]


class TTSManager:
    """
    Unified Text-to-Speech manager for Kate.
    Dynamically routes to Kokoro-ONNX (primary ultra-low-latency local engine)
    or Edge-TTS (fallback and Hindi voice engine).
    """

    def __init__(self, auto_prewarm: bool = True):
        self.kokoro = KokoroTTSProvider()
        self.edge = EdgeTTSProvider()
        self.enabled = os.environ.get("KATE_MUTE_VOICE", "0") != "1"

        if auto_prewarm:
            self.prewarm()

    def prewarm(self):
        """Asynchronously pre-warms Kokoro-ONNX so first speech has zero cold-start delay."""
        import threading
        def _warm():
            try:
                if self.kokoro.is_available:
                    self.kokoro._ensure_loaded()
                    self.kokoro._kokoro.create("Hi.", voice=self.kokoro.default_voice, speed=1.0)
                    logger.info("Kokoro-ONNX voice pre-warmed and ready.")
            except Exception as e:
                logger.warning(f"Kokoro prewarm non-critical warning: {e}")

        threading.Thread(target=_warm, daemon=True, name="KokoroPrewarm").start()

    @property
    def primary_provider(self) -> TTSProvider:
        if self.kokoro.is_available:
            return self.kokoro
        return self.edge

    def speak(
        self,
        full_text: str,
        on_done_callback: Callable[[], Any] | None = None,
        voice: str | None = None
    ) -> None:
        """
        Formats text for voice and speaks it aloud without blocking.
        """
        if not self.enabled or not full_text:
            if on_done_callback:
                on_done_callback()
            return

        # Check silent mode env vars
        if os.environ.get("JARVIS_MUTE_SOUNDS") == "1" or os.environ.get("KATE_SILENT_MODE") == "1":
            if on_done_callback:
                on_done_callback()
            return

        speech_text = clean_text_for_speech(full_text)
        if not speech_text:
            if on_done_callback:
                on_done_callback()
            return

        # If text contains Hindi / Devanagari script, use Edge-TTS with Indian neural voice
        has_devanagari = bool(re.search(r"[\u0900-\u097F]", speech_text))
        if has_devanagari or not self.kokoro.is_available:
            provider = self.edge
        else:
            provider = self.kokoro

        logger.info(f"Speaking with {provider.name}: '{speech_text[:60]}...'")
        provider.speak(speech_text, on_done_callback=on_done_callback, voice=voice)

    def stop(self) -> None:
        """Interrupts speech immediately on all providers."""
        self.kokoro.stop()
        self.edge.stop()
