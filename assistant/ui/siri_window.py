"""
Apple Siri-Style Voice-First Floating Assistant & Expandable Companion Hub for Kate.
Combines hands-free voice-first operation with an expandable Companion Hub containing:
  - Full Chat Stream with markdown & code styling
  - Slide-out Chat History
  - Direct ⚙️ In-App Settings Deck (API Keys, Voice, Autostart)
  - Voice-Enabled Permission & Confirmation Cards
  - Seamless Mid-Typing "Hey Kate" Voice Interruption
"""

import math
import os
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
import re
import time
import asyncio
import logging
import threading
import tempfile
from PyQt6.QtCore import (
    Qt, QTimer, QRectF, pyqtSignal, pyqtSlot, QThread, QMetaObject
)
from PyQt6.QtGui import (
    QPainter, QColor, QRadialGradient, QLinearGradient, QPen, QBrush,
    QPainterPath, QKeyEvent, QCursor
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
    QPushButton, QLabel, QFrame, QGraphicsDropShadowEffect, QApplication,
    QScrollArea, QSplitter, QListWidget, QListWidgetItem
)

try:
    from assistant.ipc import HTTPIPCClient
    from assistant.voice_wake import find_working_microphone
    from assistant.ui.settings_window import SettingsDialog
    from assistant.ui.orb_renderer import IOrbRenderer, OrbState
except ModuleNotFoundError:
    from ipc import HTTPIPCClient
    from voice_wake import find_working_microphone
    from ui.settings_window import SettingsDialog
    try:
        from ui.orb_renderer import IOrbRenderer, OrbState
    except ImportError:
        class IOrbRenderer: pass
        class OrbState: pass

logger = logging.getLogger("kate.ui.siri")


# ─────────────────────────────────────────────────────────────────────────────
# Audio Chimes
# ─────────────────────────────────────────────────────────────────────────────

def play_chime(kind: str = "trigger"):
    """Plays Apple Siri-style audio cues via native Windows sound (non-blocking)."""
    if os.environ.get("JARVIS_MUTE_SOUNDS") == "1" or os.environ.get("KATE_SILENT_MODE") == "1":
        return
    def _run():
        try:
            import winsound
            if kind == "trigger":
                winsound.Beep(520, 70)
                winsound.Beep(780, 90)
            elif kind == "success":
                winsound.Beep(650, 70)
                winsound.Beep(880, 110)
            elif kind == "cancel":
                winsound.Beep(520, 80)
                winsound.Beep(380, 90)
            elif kind == "confirm":
                winsound.Beep(600, 80)
                winsound.Beep(750, 80)
                winsound.Beep(900, 100)
            elif kind == "error":
                winsound.Beep(320, 150)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# Neural Voice Speaker (Edge TTS + Pygame Mixer)
# ─────────────────────────────────────────────────────────────────────────────

class NeuralVoiceSpeaker:
    """
    Bilingual Neural TTS engine with soft, melodious female voices:
      - Hindi / Hinglish: Microsoft Swara (hi-IN-SwaraNeural)
      - English: Microsoft Neerja Expressive (en-IN-NeerjaExpressiveNeural)
    """
    HINDI_VOICE = "hi-IN-SwaraNeural"
    ENGLISH_VOICE = "en-IN-NeerjaExpressiveNeural"
    NEURAL_RATE = "+0%"
    NEURAL_PITCH = "+2Hz"

    def __init__(self):
        self.enabled = (os.environ.get("JARVIS_MUTE_SOUNDS") != "1" and os.environ.get("KATE_SILENT_MODE") != "1")
        self._sapi_voice = None
        self._edge_available = False
        try:
            from assistant.tts.manager import TTSManager
            self._manager = TTSManager(auto_prewarm=True)
        except Exception as e:
            logger.warning(f"Could not initialize TTSManager: {e}")
            self._manager = None
        self._setup()

    @classmethod
    def select_voice(cls, text: str) -> str:
        """Dynamically chooses Swara for Hindi and Neerja for English."""
        if re.search(r"[\u0900-\u097F]", text):
            return cls.HINDI_VOICE
        hindi_words = {
            "haan", "nahi", "theek", "kaise", "kya", "karo", "karenge", "shukriya",
            "namaste", "dhanyawad", "aap", "mera", "meri", "hum", "bhai", "kholo",
            "chalao", "bajao", "sunao", "batao", "kaun", "kab", "kaha", "accha", "kuch"
        }
        tokens = set(re.findall(r"\b\w+\b", text.lower()))
        if len(tokens.intersection(hindi_words)) >= 1:
            return cls.HINDI_VOICE
        return cls.ENGLISH_VOICE

    def _setup(self):
        try:
            import edge_tts  # noqa: F401
            self._edge_available = True
        except ImportError:
            self._edge_available = False

        try:
            import win32com.client
            self._sapi_voice = win32com.client.Dispatch("SAPI.SpVoice")
            voices = self._sapi_voice.GetVoices()
            for i in range(voices.Count):
                if "Zira" in voices.Item(i).GetDescription():
                    self._sapi_voice.Voice = voices.Item(i)
                    break
        except Exception:
            self._sapi_voice = None

    @staticmethod
    def _strip_markdown(text: str) -> str:
        text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        text = re.sub(r"`[^`]+`", "", text)
        text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"[*_#~>\[\]|]", "", text)
        text = re.sub(r"^[-•+]\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _trim_for_speech(text: str, max_chars: int = 350) -> str:
        if len(text) <= max_chars:
            return text
        sentences = re.split(r"(?<=[.!?])\s+", text)
        result = ""
        for s in sentences:
            if len(result) + len(s) <= max_chars:
                result = (result + " " + s).strip()
            else:
                break
        return result or text[:max_chars] + "..."

    def speak(self, text: str, on_done_callback=None):
        if not self.enabled or not text:
            if on_done_callback:
                on_done_callback()
            return

        # Use new modular TTSManager (Kokoro-ONNX primary, Edge-TTS fallback)
        if self._manager and self._manager.enabled:
            self._manager.speak(text, on_done_callback=on_done_callback)
            return

        clean = self._strip_markdown(text)
        clean = self._trim_for_speech(clean)
        if not clean:
            if on_done_callback:
                on_done_callback()
            return

        if self._edge_available:
            threading.Thread(
                target=self._speak_neural,
                args=(clean, on_done_callback),
                daemon=True
            ).start()
        elif self._sapi_voice:
            threading.Thread(
                target=self._speak_sapi,
                args=(clean, on_done_callback),
                daemon=True
            ).start()

    def _speak_neural(self, text, on_done_callback):
        try:
            import edge_tts
            import pygame

            chosen_voice = self.select_voice(text)

            async def _gen():
                communicate = edge_tts.Communicate(
                    text,
                    voice=chosen_voice,
                    rate=self.NEURAL_RATE,
                    pitch=self.NEURAL_PITCH,
                )
                tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir=tempfile.gettempdir())
                tmp.close()
                await communicate.save(tmp.name)
                return tmp.name

            try:
                loop = asyncio.new_event_loop()
                mp3_path = loop.run_until_complete(_gen())
                loop.close()
            except Exception as e:
                logger.warning(f"Edge TTS synthesis error: {e}, falling back to SAPI.")
                self._speak_sapi(text, on_done_callback)
                return

            try:
                try:
                    if not pygame.mixer.get_init():
                        pygame.init()
                        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
                except Exception:
                    if not pygame.mixer.get_init():
                        pygame.mixer.init()

                pygame.mixer.music.load(mp3_path)
                pygame.mixer.music.play()
                while pygame.mixer.get_init() and pygame.mixer.music.get_busy():
                    time.sleep(0.05)
            except Exception as e:
                logger.warning(f"Pygame playback error: {e}")
            finally:
                try:
                    if pygame.mixer.get_init():
                        pygame.mixer.music.stop()
                        pygame.mixer.music.unload()
                except Exception:
                    pass
                try:
                    os.remove(mp3_path)
                except Exception:
                    pass

            if on_done_callback:
                try:
                    on_done_callback()
                except Exception as e:
                    logger.error(f"Error in on_done_callback: {e}")
        except Exception as e:
            logger.warning(f"Voice speaker top-level error: {e}")
            if on_done_callback:
                try:
                    on_done_callback()
                except Exception:
                    pass

    def _speak_sapi(self, text, on_done_callback):
        co_init = False
        try:
            import pythoncom
            pythoncom.CoInitialize()
            co_init = True
        except Exception:
            pass

        try:
            import win32com.client
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            voice.Rate = 1
            voice.Speak(text)
        except Exception as e:
            logger.warning(f"SAPI speak error: {e}")
            try:
                import pyttsx3
                engine = pyttsx3.init()
                engine.say(text)
                engine.runAndWait()
            except Exception as e2:
                logger.warning(f"pyttsx3 fallback error: {e2}")
        finally:
            if co_init:
                try:
                    import pythoncom
                    pythoncom.CoUninitialize()
                except Exception:
                    pass
        if on_done_callback:
            try:
                on_done_callback()
            except Exception:
                pass

    def stop(self):
        if hasattr(self, "_manager") and self._manager:
            try:
                self._manager.stop()
            except Exception:
                pass
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
        except Exception:
            pass
        if self._sapi_voice:
            try:
                self._sapi_voice.Speak("", 2)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Query Worker & Voice Recognition Worker
# ─────────────────────────────────────────────────────────────────────────────

class QueryWorker(QThread):
    response_received = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)

    def __init__(self, query: str):
        super().__init__()
        self.query = query

    def run(self):
        try:
            client = HTTPIPCClient()
            code, resp = client.send_query(self.query, timeout=45)
            import json
            if code == 200:
                data = json.loads(resp)
                self.response_received.emit(data)
            else:
                self.error_occurred.emit(f"IPC Error ({code}): {resp}")
        except Exception as e:
            self.error_occurred.emit(str(e))


class VoiceRecognitionWorker(QThread):
    speech_recognized = pyqtSignal(str)
    speech_failed = pyqtSignal(str)

    def __init__(self, timeout: int = 7, phrase_time_limit: int = 12):
        super().__init__()
        self.timeout = timeout
        self.phrase_time_limit = phrase_time_limit

    def run(self):
        try:
            import speech_recognition as sr
            r = sr.Recognizer()
            r.operation_timeout = 5
            r.pause_threshold = 0.7
            r.phrase_threshold = 0.15
            r.non_speaking_duration = 0.4
            r.dynamic_energy_threshold = False  # Fixed threshold to prevent mic going deaf after audio
            r.energy_threshold = 280.0

            mic_idx = find_working_microphone()
            try:
                with sr.Microphone(device_index=mic_idx) as source:
                    audio = r.listen(source, timeout=self.timeout, phrase_time_limit=self.phrase_time_limit)
            except Exception:
                mic_idx = find_working_microphone(force_refresh=True)
                with sr.Microphone(device_index=mic_idx) as source:
                    audio = r.listen(source, timeout=self.timeout, phrase_time_limit=self.phrase_time_limit)

            text = None
            # 1. Try Indian English / Hinglish
            try:
                text = r.recognize_google(audio, language="en-IN").strip()
            except Exception as e:
                logger.debug(f"UI STT en-IN error: {e}")

            # 2. Try Native Hindi
            if not text:
                try:
                    text = r.recognize_google(audio, language="hi-IN").strip()
                except Exception as e:
                    logger.debug(f"UI STT hi-IN error: {e}")

            # 3. Fallback to US English
            if not text:
                try:
                    text = r.recognize_google(audio, language="en-US").strip()
                except Exception as e:
                    logger.debug(f"UI STT en-US error: {e}")

            if text:
                logger.info(f"UI Voice recognized: '{text}'")
                self.speech_recognized.emit(text)
            else:
                self.speech_failed.emit("No speech detected. Speak clearly into mic.")
        except Exception as e:
            logger.warning(f"Voice capture worker error: {e}")
            self.speech_failed.emit(str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Clickable Next-Gen Liquid Kate Orb (IOrbRenderer)
# ─────────────────────────────────────────────────────────────────────────────

class LiquidKateOrb(QWidget):
    """
    Next-Gen Liquid Mercury Bioluminescent Kate Orb:
      - Photorealistic liquid metal quicksilver curvature with prismatic horizon reflection
      - Dynamic harmonic breathing pulse & audio-reactive sonic wave displacement
      - Concentric translucent audio ripples expanding during listening/speaking
      - Celestial quantum singularity core with 8 orbiting constellation particles during thinking
      - Tactile liquid ripple shockwave on click with spring relaxation
      - Standalone zero-clutter floating droplet aesthetic
    """
    clicked = pyqtSignal()

    def __init__(self, parent=None, size=96):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setToolTip("Kate (Click for Chat & Settings)")
        self.phase = 0.0
        self.current_state = "idle"  # idle, listening, thinking, speaking, waiting_permission
        self.is_hovered = False
        self.audio_energy = 0.0
        self._press_pos = None

        # Tactile ripple physics
        self.click_ripple_radius = 0.0
        self.click_ripple_alpha = 0

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_animation)
        self.timer.start(16)  # 60 FPS silky smooth rendering

    def set_state(self, state: str):
        self.current_state = state
        self.update()

    def set_audio_energy(self, level: float):
        self.audio_energy = max(0.0, min(1.0, level))
        self.update()

    def trigger_click_feedback(self):
        self.click_ripple_radius = 4.0
        self.click_ripple_alpha = 230
        self.update()

    def _update_animation(self):
        speeds = {
            "thinking": 0.075,
            "listening": 0.09,
            "speaking": 0.07,
            "waiting_permission": 0.05,
            "idle": 0.022
        }
        self.phase = (self.phase + speeds.get(self.current_state, 0.022)) % (2 * math.pi)

        # Decay click shockwave
        if self.click_ripple_alpha > 0:
            self.click_ripple_radius += 2.2
            self.click_ripple_alpha = max(0, self.click_ripple_alpha - 15)

        self.update()

    def enterEvent(self, event):
        self.is_hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.is_hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.globalPosition().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and hasattr(self, "_press_pos") and self._press_pos:
            diff = (event.globalPosition().toPoint() - self._press_pos).manhattanLength()
            if diff < 6:
                play_chime("confirm")
                self.trigger_click_feedback()
                self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        cx, cy = w / 2.0, (h / 2.0) - 2.0

        is_listening = self.current_state == "listening"
        is_thinking = self.current_state == "thinking"
        is_speaking = self.current_state == "speaking"
        is_permission = self.current_state == "waiting_permission"

        # ── Dynamic Breathing & Audio Harmonics ──
        breathe = math.sin(self.phase * 1.5) * 1.4
        rx = 32.0 + breathe
        ry = 28.0 + breathe * 0.8
        if self.is_hovered:
            rx += 2.0
            ry += 1.6

        # Audio boost
        energy_scale = self.audio_energy if (is_listening or is_speaking) else 0.0
        rx += energy_scale * 5.0
        ry += energy_scale * 4.0

        # ── 1. Soft 3D Cast Shadow ──
        shadow_y = cy + ry * 0.74
        shadow_rect = QRectF(cx - rx * 0.88, shadow_y - 6, rx * 1.76, 14)
        shadow_grad = QRadialGradient(cx, shadow_y, rx * 0.88)
        shadow_grad.setColorAt(0.0, QColor(0, 0, 0, 160))
        shadow_grad.setColorAt(0.45, QColor(0, 0, 0, 80))
        shadow_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setBrush(QBrush(shadow_grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(shadow_rect)

        # ── 2. Bioluminescent Outer Aura / Sonic Sonar Rings ──
        glow_r = rx + 14.0 + (energy_scale * 12.0)
        glow_alpha = 110 if is_listening else (90 if is_thinking else (140 if is_permission else 55))
        glow_grad = QRadialGradient(cx, cy, glow_r)

        if is_permission:
            glow_grad.setColorAt(0.0, QColor(255, 185, 45, glow_alpha))
            glow_grad.setColorAt(0.5, QColor(255, 130, 20, glow_alpha // 2))
        elif is_listening:
            glow_grad.setColorAt(0.0, QColor(0, 235, 255, glow_alpha))
            glow_grad.setColorAt(0.5, QColor(0, 140, 255, glow_alpha // 2))
        elif is_thinking:
            glow_grad.setColorAt(0.0, QColor(160, 120, 255, glow_alpha))
            glow_grad.setColorAt(0.5, QColor(90, 50, 240, glow_alpha // 2))
        elif is_speaking:
            glow_grad.setColorAt(0.0, QColor(255, 215, 100, glow_alpha))
            glow_grad.setColorAt(0.5, QColor(245, 145, 30, glow_alpha // 2))
        else:
            # Ethereal quicksilver pulse with iridescent blue/violet highlight
            glow_grad.setColorAt(0.0, QColor(210, 230, 255, glow_alpha))
            glow_grad.setColorAt(0.5, QColor(140, 175, 230, glow_alpha // 2))
        glow_grad.setColorAt(1.0, QColor(0, 0, 0, 0))

        painter.setBrush(QBrush(glow_grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(cx - glow_r, cy - glow_r, glow_r * 2, glow_r * 2))

        # Concentric audio ripple sonar rings when listening/speaking
        if is_listening or is_speaking:
            ring_rad = rx + 6.0 + (math.sin(self.phase * 4.0) * 4.0) + (energy_scale * 8.0)
            ring_pen = QPen(QColor(0, 235, 255, 80) if is_listening else QColor(255, 215, 100, 80), 1.5)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(ring_pen)
            painter.drawEllipse(QRectF(cx - ring_rad, cy - ring_rad, ring_rad * 2, ring_rad * 2))

        # ── 3. Fluid Droplet Contour Path (Harmonic Surface Wave Ripples) ──
        path = QPainterPath()
        num_pts = 72
        first_pt = None

        wave_amp = 1.6 if is_listening else (1.1 if is_speaking else (0.8 if is_thinking else 0.4))
        if energy_scale > 0:
            wave_amp += energy_scale * 2.5

        for k in range(num_pts):
            theta = k * (2.0 * math.pi / num_pts)
            w_offset = (
                math.sin(2.0 * theta + self.phase * 3.2) * wave_amp +
                math.cos(4.0 * theta - self.phase * 2.4) * (wave_amp * 0.45)
            )
            cur_rx = rx + w_offset
            cur_ry = ry + w_offset * 0.85
            px = cx + math.cos(theta) * cur_rx
            py = cy + math.sin(theta) * cur_ry
            if k == 0:
                path.moveTo(px, py)
                first_pt = (px, py)
            else:
                path.lineTo(px, py)
        if first_pt:
            path.lineTo(first_pt[0], first_pt[1])
        path.closeSubpath()

        # ── 4. Volumetric 3D Convex Base Gradient ──
        core_grad = QRadialGradient(cx - rx * 0.22, cy - ry * 0.28, rx * 1.35)
        if is_permission:
            core_grad.setColorAt(0.00, QColor(255, 235, 190, 255))
            core_grad.setColorAt(0.20, QColor(230, 155, 65, 255))
            core_grad.setColorAt(0.50, QColor(145, 75, 22, 255))
            core_grad.setColorAt(0.85, QColor(55, 22, 9, 255))
            core_grad.setColorAt(1.00, QColor(16, 7, 3, 255))
        elif is_thinking:
            core_grad.setColorAt(0.00, QColor(220, 205, 255, 255))
            core_grad.setColorAt(0.20, QColor(130, 95, 225, 255))
            core_grad.setColorAt(0.50, QColor(50, 25, 110, 255))
            core_grad.setColorAt(0.85, QColor(18, 10, 45, 255))
            core_grad.setColorAt(1.00, QColor(8, 4, 20, 255))
        else:
            core_grad.setColorAt(0.00, QColor(245, 248, 255, 255))
            core_grad.setColorAt(0.18, QColor(195, 210, 230, 255))
            core_grad.setColorAt(0.40, QColor(105, 120, 142, 255))
            core_grad.setColorAt(0.70, QColor(34, 42, 56, 255))
            core_grad.setColorAt(0.92, QColor(14, 18, 26, 255))
            core_grad.setColorAt(1.00, QColor(8, 10, 15, 255))

        painter.setBrush(QBrush(core_grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(path)

        # ── 5. Curved 3D Liquid Horizon Reflection ──
        painter.save()
        painter.setClipPath(path)

        horizon_grad = QLinearGradient(cx - rx * 0.6, cy - ry * 0.9, cx + rx * 0.6, cy + ry * 0.9)
        if is_permission:
            horizon_grad.setColorAt(0.00, QColor(28, 18, 10, 255))
            horizon_grad.setColorAt(0.32, QColor(65, 38, 16, 255))
            horizon_grad.setColorAt(0.44, QColor(255, 175, 45, 255))
            horizon_grad.setColorAt(0.50, QColor(255, 245, 200, 255))
            horizon_grad.setColorAt(0.58, QColor(245, 130, 25, 255))
            horizon_grad.setColorAt(0.72, QColor(135, 75, 20, 255))
            horizon_grad.setColorAt(0.88, QColor(48, 22, 8, 255))
            horizon_grad.setColorAt(1.00, QColor(18, 8, 4, 255))
        elif is_thinking:
            horizon_grad.setColorAt(0.00, QColor(14, 8, 28, 255))
            horizon_grad.setColorAt(0.32, QColor(42, 22, 75, 255))
            horizon_grad.setColorAt(0.44, QColor(180, 140, 255, 255))
            horizon_grad.setColorAt(0.50, QColor(255, 255, 255, 255))
            horizon_grad.setColorAt(0.58, QColor(130, 80, 240, 255))
            horizon_grad.setColorAt(0.72, QColor(50, 25, 95, 255))
            horizon_grad.setColorAt(0.88, QColor(22, 10, 42, 255))
            horizon_grad.setColorAt(1.00, QColor(8, 4, 18, 255))
        else:
            horizon_grad.setColorAt(0.00, QColor(12, 16, 24, 255))
            horizon_grad.setColorAt(0.32, QColor(35, 45, 60, 255))
            horizon_grad.setColorAt(0.42, QColor(135, 195, 240, 255))  # Electric cyan-blue horizon gleam
            horizon_grad.setColorAt(0.50, QColor(255, 255, 255, 255))  # Liquid silver-white horizon crest
            horizon_grad.setColorAt(0.58, QColor(240, 205, 150, 255))  # Warm champagne reflection
            horizon_grad.setColorAt(0.72, QColor(95, 102, 118, 255))
            horizon_grad.setColorAt(0.88, QColor(28, 35, 48, 255))
            horizon_grad.setColorAt(1.00, QColor(10, 14, 20, 255))

        painter.setBrush(QBrush(horizon_grad))
        painter.drawRect(QRectF(cx - rx - 8, cy - ry - 8, (rx + 8) * 2, (ry + 8) * 2))

        # ── 6. 3D Fresnel Edge Rim Lighting ──
        fresnel_grad = QRadialGradient(cx, cy, rx)
        fresnel_grad.setColorAt(0.00, QColor(255, 255, 255, 0))
        fresnel_grad.setColorAt(0.70, QColor(255, 255, 255, 0))
        fresnel_grad.setColorAt(0.88, QColor(200, 230, 255, 65))
        fresnel_grad.setColorAt(0.97, QColor(240, 248, 255, 180))
        fresnel_grad.setColorAt(1.00, QColor(255, 255, 255, 235))
        painter.setBrush(QBrush(fresnel_grad))
        painter.drawRect(QRectF(cx - rx - 8, cy - ry - 8, (rx + 8) * 2, (ry + 8) * 2))

        # ── 7. Top 3D Specular Liquid Gloss Dome ──
        hi_cx = cx - rx * 0.18
        hi_cy = cy - ry * 0.38
        hi_w = rx * 0.85
        hi_h = ry * 0.46
        hi_grad = QLinearGradient(hi_cx, hi_cy - hi_h / 2, hi_cx, hi_cy + hi_h / 2)
        hi_grad.setColorAt(0.0, QColor(255, 255, 255, 245))
        hi_grad.setColorAt(0.5, QColor(255, 255, 255, 110))
        hi_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(QBrush(hi_grad))
        painter.drawEllipse(QRectF(hi_cx - hi_w / 2, hi_cy - hi_h / 2, hi_w, hi_h))

        # Sharp 3D Pinpoint Hotspot
        hotspot_x = cx - rx * 0.22
        hotspot_y = cy - ry * 0.38
        hotspot_grad = QRadialGradient(hotspot_x, hotspot_y, 4.2)
        hotspot_grad.setColorAt(0.0, QColor(255, 255, 255, 255))
        hotspot_grad.setColorAt(0.6, QColor(255, 255, 255, 190))
        hotspot_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(QBrush(hotspot_grad))
        painter.drawEllipse(QRectF(hotspot_x - 4, hotspot_y - 4, 8, 8))

        # ── 8. Bottom Rim Bounce Light ──
        bounce_cy = cy + ry * 0.68
        bounce_grad = QLinearGradient(cx, bounce_cy - 4, cx, bounce_cy + 6)
        bounce_grad.setColorAt(0.0, QColor(255, 255, 255, 0))
        bounce_grad.setColorAt(1.0, QColor(215, 235, 255, 100))
        painter.setBrush(QBrush(bounce_grad))
        painter.drawEllipse(QRectF(cx - rx * 0.62, bounce_cy - 4, rx * 1.24, 10))

        painter.restore()

        # ── 9. Perimeter Liquid Platinum Rim ──
        rim_pen = QPen(QColor(255, 255, 255, 225), 1.25)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(rim_pen)
        painter.drawPath(path)

        # ── 10. Thinking State: Celestial Singularity & 8 Orbit Particles ──
        if is_thinking:
            painter.setBrush(QColor(8, 6, 18, 175))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPath(path)

            # Central glowing quantum singularity
            center_rad = 7.0 + math.sin(self.phase * 5.0) * 1.5
            cg = QRadialGradient(cx, cy, center_rad + 3)
            cg.setColorAt(0.0, QColor(255, 255, 255, 255))
            cg.setColorAt(0.4, QColor(180, 140, 255, 210))
            cg.setColorAt(1.0, QColor(90, 50, 240, 0))
            painter.setBrush(QBrush(cg))
            painter.drawEllipse(QRectF(cx - center_rad, cy - center_rad, center_rad * 2, center_rad * 2))

            # 8 Orbiting constellation nodes with trailing alphas
            num_beads = 8
            orb_rx = rx * 0.58
            orb_ry = ry * 0.58
            rot_speed = self.phase * 3.6

            bead_radii = [4.8, 4.2, 3.8, 3.4, 3.0, 2.6, 2.2, 1.8]
            bead_alphas = [255, 240, 210, 180, 145, 110, 75, 45]

            for i in range(num_beads):
                ang = rot_speed + (i * 2.0 * math.pi / num_beads)
                bx = cx + math.cos(ang) * orb_rx
                by = cy + math.sin(ang) * orb_ry
                br = bead_radii[i]
                ba = bead_alphas[i]

                bg = QRadialGradient(bx, by, br + 2.5)
                bg.setColorAt(0.0, QColor(255, 255, 255, ba))
                bg.setColorAt(0.5, QColor(200, 180, 255, int(ba * 0.8)))
                bg.setColorAt(1.0, QColor(140, 90, 255, 0))

                painter.setBrush(QBrush(bg))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(QRectF(bx - br - 2.5, by - br - 2.5, (br + 2.5) * 2, (br + 2.5) * 2))
                painter.setBrush(QColor(255, 255, 255, ba))
                painter.drawEllipse(QRectF(bx - br, by - br, br * 2, br * 2))

        # ── 11. Click Tactile Shockwave Ripple ──
        if self.click_ripple_alpha > 0:
            rip_pen = QPen(QColor(0, 245, 212, self.click_ripple_alpha), 2.0)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(rip_pen)
            r_rad = self.click_ripple_radius
            painter.drawEllipse(QRectF(cx - r_rad, cy - r_rad, r_rad * 2, r_rad * 2))


# Backward compatibility aliases
LiquidMercuryOrb = LiquidKateOrb
SiriGlowOrb = LiquidKateOrb

try:
    IOrbRenderer.register(LiquidKateOrb)
except Exception:
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Animated Siri Soundwave Widget
# ─────────────────────────────────────────────────────────────────────────────

class SiriSoundWave(QWidget):
    """Sleek multi-bar animated Siri soundwave indicating voice activity."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(70, 26)
        self.phase = 0.0
        self.is_active = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(35)

    def set_active(self, active: bool):
        self.is_active = active
        self.update()

    def _tick(self):
        if self.is_active:
            self.phase += 0.25
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bar_count = 5
        bar_w = 4.0
        gap = 7.0
        total_w = bar_count * bar_w + (bar_count - 1) * gap
        start_x = (self.width() - total_w) / 2.0
        cy = self.height() / 2.0

        colors = [
            QColor(0, 245, 212),
            QColor(56, 189, 248),
            QColor(168, 85, 247),
            QColor(236, 72, 153),
            QColor(0, 245, 212)
        ]

        for i in range(bar_count):
            if self.is_active:
                h = 4.0 + math.sin(self.phase + i * 1.1) * 9.0
            else:
                h = 3.5
            x = start_x + i * (bar_w + gap)
            y = cy - (h / 2.0)
            painter.setBrush(QBrush(colors[i % len(colors)]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(x, y, bar_w, h), 2.0, 2.0)


# ─────────────────────────────────────────────────────────────────────────────
# Permission & Confirmation Modal Card (Voice + Click)
# ─────────────────────────────────────────────────────────────────────────────

class PermissionCardWidget(QFrame):
    """
    Slides down when Kate needs user permission or confirmation.
    Supports both click buttons AND voice commands ('yes', 'do it', 'no', 'cancel').
    """
    approved = pyqtSignal()
    denied = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PermissionCard")
        self.setStyleSheet("""
            #PermissionCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1E1B4B, stop:1 #0F172A);
                border: 1px solid rgba(251, 191, 36, 0.45);
                border-radius: 16px;
            }
        """)
        self.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # Header
        hdr = QHBoxLayout()
        icon = QLabel("⚠️")
        icon.setStyleSheet("font-size: 16px;")
        hdr.addWidget(icon)

        self.title_lbl = QLabel("Kate requests your confirmation")
        self.title_lbl.setStyleSheet("color:#FBBF24; font-weight:bold; font-size:13px;")
        hdr.addWidget(self.title_lbl)
        hdr.addStretch()

        self.voice_cue_lbl = QLabel("Say 'Yes' or 'No'")
        self.voice_cue_lbl.setStyleSheet("color:#94A3B8; font-size:11px; background:rgba(255,255,255,0.06); padding:2px 8px; border-radius:8px;")
        hdr.addWidget(self.voice_cue_lbl)
        layout.addLayout(hdr)

        # Detail text
        self.detail_lbl = QLabel("Action details will appear here...")
        self.detail_lbl.setWordWrap(True)
        self.detail_lbl.setStyleSheet("color:#E2E8F0; font-size:12px; line-height:1.3;")
        layout.addWidget(self.detail_lbl)

        # Action Buttons
        btn_box = QHBoxLayout()
        btn_box.addStretch()

        self.deny_btn = QPushButton("✕ Deny (No)", self)
        self.deny_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.deny_btn.setStyleSheet("""
            QPushButton {
                background: rgba(239, 68, 68, 0.15); color: #FCA5A5;
                border: 1px solid rgba(239, 68, 68, 0.4); border-radius: 8px;
                padding: 6px 14px; font-size: 12px; font-weight: 600;
            }
            QPushButton:hover { background: rgba(239, 68, 68, 0.3); }
        """)
        self.deny_btn.clicked.connect(self._on_deny)
        btn_box.addWidget(self.deny_btn)

        self.approve_btn = QPushButton("✓ Approve (Yes)", self)
        self.approve_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.approve_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #10B981, stop:1 #059669);
                color: #FFFFFF; border: none; border-radius: 8px;
                padding: 6px 16px; font-size: 12px; font-weight: bold;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #34D399, stop:1 #10B981);
            }
        """)
        self.approve_btn.clicked.connect(self._on_approve)
        btn_box.addWidget(self.approve_btn)

        layout.addLayout(btn_box)

    def prompt_confirmation(self, action_text: str):
        self.detail_lbl.setText(action_text)
        self.show()
        play_chime("confirm")

    def handle_voice_answer(self, spoken_text: str) -> bool:
        """Parses speech for voice approval/denial."""
        if not self.isVisible():
            return False
        clean = spoken_text.lower()
        positive_words = ["yes", "yeah", "yep", "sure", "proceed", "allow", "do it", "theek hai", "haan", "ok"]
        negative_words = ["no", "nope", "cancel", "stop", "deny", "mat karo", "nahi", "dont"]

        if any(w in clean for w in positive_words):
            self._on_approve()
            return True
        elif any(w in clean for w in negative_words):
            self._on_deny()
            return True
        return False

    def _on_approve(self):
        play_chime("success")
        self.hide()
        self.approved.emit()

    def _on_deny(self):
        play_chime("cancel")
        self.hide()
        self.denied.emit()


# ─────────────────────────────────────────────────────────────────────────────
# Master Siri Window (Voice-First Pill + Expandable Companion Hub)
# ─────────────────────────────────────────────────────────────────────────────

class SiriWindow(QWidget):
    """
    Apple Siri Voice-First Companion Window for Kate:
      - Default: Sleek 320px voice capsule (Orb + animated soundwave + live status).
      - On Globe Click: Expands into full Companion Hub with chat, history, and settings.
      - Dual confirmation via voice ('Yes'/'No') or buttons.
      - Mid-typing 'Hey Kate' instant voice override.
    """
    dismissed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kate")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.speaker = NeuralVoiceSpeaker()
        self.wake_listener = None
        self.is_pinned = True
        self.is_expanded = False
        self.continuous_talk_enabled = True
        self.session_active = False
        self.silence_count = 0
        self._voice_worker = None
        self._active_worker = None
        self._chat_history_list = []
        self._drag_pos = None

        # Timers
        self._inactivity_timer = QTimer(self)
        self._inactivity_timer.setSingleShot(True)
        self._inactivity_timer.timeout.connect(self._on_inactivity_timeout)

        self._sleep_grace_timer = QTimer(self)
        self._sleep_grace_timer.setSingleShot(True)
        self._sleep_grace_timer.timeout.connect(self._on_sleep_timeout)
        # 10-second fail-safe watchdog: ensures ambient wake-word listener never remains locked
        self._wake_watchdog = QTimer(self)
        self._wake_watchdog.setInterval(10000)
        self._wake_watchdog.timeout.connect(self._check_wake_watchdog)
        self._wake_watchdog.start()

        self._init_ui()
        self._set_capsule_mode()

    def set_wake_listener(self, listener):
        self.wake_listener = listener

    # ── UI Setup ────────────────────────────────────────────────────────────

    def _init_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # ── 1. Voice Toast Pill (Short speech preview banner) ───────────────
        self.toast_pill = QFrame(self)
        self.toast_pill.setObjectName("ToastPill")
        self.toast_pill.setStyleSheet("""
            #ToastPill {
                background: rgba(18, 20, 32, 0.92);
                border: 1px solid rgba(255, 255, 255, 0.16);
                border-radius: 16px;
            }
        """)
        toast_l = QHBoxLayout(self.toast_pill)
        toast_l.setContentsMargins(14, 8, 14, 8)
        self.toast_lbl = QLabel("", self.toast_pill)
        self.toast_lbl.setStyleSheet("color:#E2E8F0; font-size:12px; font-weight:500;")
        toast_l.addWidget(self.toast_lbl)
        self.toast_pill.hide()
        self.main_layout.addWidget(self.toast_pill)

        # ── 2. Permission Confirmation Card ─────────────────────────────────
        self.permission_card = PermissionCardWidget(self)
        self.main_layout.addWidget(self.permission_card)

        # ── 3. Companion Hub (Expanded Chat, History, Settings View) ─────────
        self.companion_hub = QFrame(self)
        self.companion_hub.setObjectName("CompanionHub")
        self.companion_hub.setStyleSheet("""
            #CompanionHub {
                background: rgba(9, 10, 15, 0.94);
                border: 1px solid rgba(255, 255, 255, 0.14);
                border-radius: 26px;
            }
        """)
        hub_shadow = QGraphicsDropShadowEffect(self.companion_hub)
        hub_shadow.setBlurRadius(36)
        hub_shadow.setColor(QColor(0, 0, 0, 220))
        hub_shadow.setOffset(0, 8)
        self.companion_hub.setGraphicsEffect(hub_shadow)

        hub_layout = QVBoxLayout(self.companion_hub)
        hub_layout.setContentsMargins(16, 14, 16, 14)
        hub_layout.setSpacing(10)

        # Hub Header
        hub_hdr = QHBoxLayout()
        self.hub_badge = QLabel("✨ Kate Assistant", self.companion_hub)
        self.hub_badge.setStyleSheet("color:#E2E8F0; font-weight:bold; font-size:13px;")
        hub_hdr.addWidget(self.hub_badge)

        self.status_label = QLabel("Ready", self.companion_hub)
        self.status_label.setStyleSheet("color:#94A3B8; font-size:11px; margin-left:6px;")
        hub_hdr.addWidget(self.status_label)
        hub_hdr.addStretch()

        # Pin Button
        self.pin_btn = QPushButton("📌", self.companion_hub)
        self.pin_btn.setFixedSize(28, 28)
        self.pin_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.pin_btn.setToolTip("Pin to Screen (Keep open indefinitely)")
        self.pin_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255, 255, 255, 0.06); border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 14px; font-size: 11px; color: #94A3B8;
            }
            QPushButton:hover { background: rgba(255, 255, 255, 0.16); color: #FFF; }
        """)
        self.pin_btn.clicked.connect(self._toggle_pin)
        hub_hdr.addWidget(self.pin_btn)

        # Speaker Mute Button
        self.speaker_btn = QPushButton("🔊", self.companion_hub)
        self.speaker_btn.setFixedSize(28, 28)
        self.speaker_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.speaker_btn.setToolTip("Voice Output: ON (click to mute)")
        self.speaker_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255, 255, 255, 0.06); border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 14px; font-size: 12px;
            }
            QPushButton:hover { background: rgba(0, 245, 212, 0.25); }
        """)
        self.speaker_btn.clicked.connect(self._toggle_speaker)
        hub_hdr.addWidget(self.speaker_btn)

        # History Button
        self.btn_history = QPushButton("📜 History", self.companion_hub)
        self.btn_history.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_history.setStyleSheet("""
            QPushButton {
                background: rgba(255, 255, 255, 0.08); color: #94A3B8;
                border: 1px solid rgba(255, 255, 255, 0.14); border-radius: 12px;
                padding: 4px 10px; font-size: 11px; font-weight: 500;
            }
            QPushButton:hover { background: rgba(255, 255, 255, 0.18); color: #FFF; }
        """)
        self.btn_history.clicked.connect(self._toggle_history_drawer)
        hub_hdr.addWidget(self.btn_history)

        # Settings Button (Direct 1-Click Access to API Keys Deck)
        self.btn_settings = QPushButton("⚙️ Settings", self.companion_hub)
        self.btn_settings.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_settings.setToolTip("Open API Keys & System Settings Deck")
        self.btn_settings.setStyleSheet("""
            QPushButton {
                background: rgba(142, 68, 245, 0.25); color: #C084FC;
                border: 1px solid rgba(192, 132, 252, 0.35); border-radius: 12px;
                padding: 4px 10px; font-size: 11px; font-weight: 600;
            }
            QPushButton:hover { background: rgba(142, 68, 245, 0.45); color: #FFF; }
        """)
        self.btn_settings.clicked.connect(self._open_settings_deck)
        hub_hdr.addWidget(self.btn_settings)

        # Minimize to Capsule Button
        self.btn_collapse = QPushButton("✕", self.companion_hub)
        self.btn_collapse.setFixedSize(26, 26)
        self.btn_collapse.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_collapse.setToolTip("Collapse back to Floating Mercury Orb")
        self.btn_collapse.setStyleSheet("""
            QPushButton {
                background: rgba(255, 255, 255, 0.08); color: #94A3B8;
                border: none; border-radius: 13px; font-size: 12px;
            }
            QPushButton:hover { background: rgba(239, 68, 68, 0.35); color: #FFF; }
        """)
        self.btn_collapse.clicked.connect(self._set_orb_mode)
        hub_hdr.addWidget(self.btn_collapse)

        hub_layout.addLayout(hub_hdr)

        # Middle Content Splitter (Chat Stream + History Drawer)
        self.hub_content_splitter = QSplitter(Qt.Orientation.Horizontal, self.companion_hub)
        self.hub_content_splitter.setStyleSheet("QSplitter::handle { background: transparent; }")

        # Chat Stream Area
        self.chat_scroll = QScrollArea(self.hub_content_splitter)
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setStyleSheet("background:transparent; border:none;")
        self.chat_container = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(4, 4, 4, 4)
        self.chat_layout.setSpacing(10)
        self.chat_layout.addStretch()
        self.chat_scroll.setWidget(self.chat_container)
        self.hub_content_splitter.addWidget(self.chat_scroll)

        # Slide-out History Drawer
        self.history_drawer = QFrame(self.hub_content_splitter)
        self.history_drawer.setObjectName("HistoryDrawer")
        self.history_drawer.setStyleSheet("""
            #HistoryDrawer {
                background: rgba(18, 24, 38, 0.95);
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 14px;
            }
        """)
        hist_layout = QVBoxLayout(self.history_drawer)
        hist_layout.setContentsMargins(10, 10, 10, 10)
        hist_lbl = QLabel("Recent Questions", self.history_drawer)
        hist_lbl.setStyleSheet("color:#94A3B8; font-weight:600; font-size:11px;")
        hist_layout.addWidget(hist_lbl)

        self.history_list = QListWidget(self.history_drawer)
        self.history_list.setStyleSheet("""
            QListWidget {
                background: transparent; border: none; color: #E2E8F0; font-size: 12px;
            }
            QListWidget::item { padding: 6px 8px; border-radius: 6px; }
            QListWidget::item:hover { background: rgba(56, 189, 248, 0.15); color: #38BDF8; }
        """)
        self.history_list.itemClicked.connect(self._on_history_item_clicked)
        hist_layout.addWidget(self.history_list)
        self.history_drawer.hide()
        self.hub_content_splitter.addWidget(self.history_drawer)

        hub_layout.addWidget(self.hub_content_splitter)

        # Companion Hub Bottom Input Bar (for optional typing)
        hub_input_bar = QHBoxLayout()
        self.hub_input = QLineEdit(self.companion_hub)
        self.hub_input.setPlaceholderText("Type your question, or say 'Hey Kate' to speak...")
        self.hub_input.setStyleSheet("""
            QLineEdit {
                background: rgba(22, 24, 34, 0.75);
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 18px; color: #F8FAFC; font-size: 13px;
                padding: 8px 14px;
            }
            QLineEdit:focus { border: 1px solid rgba(0, 215, 255, 0.6); background: rgba(26, 28, 40, 0.85); }
        """)
        self.hub_input.returnPressed.connect(self._on_hub_submit)
        # Mid-typing voice interruption hook
        self.hub_input.textChanged.connect(lambda: self._reset_inactivity_timer())
        hub_input_bar.addWidget(self.hub_input)

        # Hub Mic Button
        self.hub_mic_btn = QPushButton("🎙️", self.companion_hub)
        self.hub_mic_btn.setFixedSize(36, 36)
        self.hub_mic_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.hub_mic_btn.setStyleSheet("""
            QPushButton {
                background: rgba(255, 255, 255, 0.07); border: 1px solid rgba(255, 255, 255, 0.14);
                border-radius: 18px; font-size: 14px;
            }
            QPushButton:hover { background: rgba(56, 189, 248, 0.25); border-color: rgba(56, 189, 248, 0.4); }
        """)
        self.hub_mic_btn.clicked.connect(self.start_conversation_session)
        hub_input_bar.addWidget(self.hub_mic_btn)

        # Hub Send Button
        self.hub_send_btn = QPushButton("↑", self.companion_hub)
        self.hub_send_btn.setFixedSize(36, 36)
        self.hub_send_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.hub_send_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #00F2FE, stop:1 #4FACFE);
                color: #0F172A; border: none; border-radius: 18px;
                font-size: 16px; font-weight: bold;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #38F9D7, stop:1 #43E97B);
            }
        """)
        self.hub_send_btn.clicked.connect(self._on_hub_submit)
        hub_input_bar.addWidget(self.hub_send_btn)

        hub_layout.addLayout(hub_input_bar)
        self.companion_hub.hide()
        self.main_layout.addWidget(self.companion_hub)

        # ── 4. Standalone Liquid Mercury Floating Orb (Zero surrounding clutter) ──
        self.orb_container = QWidget(self)
        self.orb_container.setObjectName("OrbContainer")
        self.orb_container.setStyleSheet("background: transparent; border: none;")
        orb_box = QVBoxLayout(self.orb_container)
        orb_box.setContentsMargins(0, 0, 0, 0)
        orb_box.setSpacing(0)
        orb_box.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Standalone Liquid Mercury Droplet (Click expands to Hub)
        self.orb = LiquidMercuryOrb(self.orb_container, size=96)
        self.orb.clicked.connect(self.toggle_companion_hub)
        orb_box.addWidget(self.orb)

        self.main_layout.addWidget(self.orb_container)

    # ── Mode Transitions ───────────────────────────────────────────────────

    def _check_wake_watchdog(self):
        if self.wake_listener and getattr(self.wake_listener, "_paused", False):
            is_recording = hasattr(self, "_voice_worker") and self._voice_worker and self._voice_worker.isRunning()
            if not is_recording and getattr(self.orb, "current_state", "idle") == "idle":
                logger.info("Watchdog: Auto-resuming ambient wake_listener.")
                self.wake_listener.resume()

    def _set_orb_mode(self):
        """Switches to pure standalone floating liquid mercury orb mode (no surrounding pill)."""
        self.is_expanded = False
        self.companion_hub.hide()
        self.toast_pill.hide()
        self.permission_card.hide()
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        self.orb_container.show()
        self.setFixedSize(96, 96)
        self._center_at_bottom()

    def _set_capsule_mode(self):
        self._set_orb_mode()

    def toggle_companion_hub(self):
        """Toggles between Standalone Mercury Orb and Expanded Companion Hub on Click."""
        if self.is_expanded:
            self._set_orb_mode()
        else:
            self._set_expanded_mode()

    def _set_expanded_mode(self):
        """Expands into full visionOS Grey-Black Glassmorphic Chat Deck."""
        self.is_expanded = True
        self.orb_container.hide()
        self.main_layout.setContentsMargins(12, 12, 12, 12)
        self.companion_hub.show()
        self.setFixedSize(480, 600)
        self._center_at_bottom()
        self.hub_input.setFocus()
        self._reset_inactivity_timer()

    def _center_at_bottom(self):
        screen = QApplication.primaryScreen().geometry()
        x = (screen.width() - self.width()) // 2
        y = screen.height() - self.height() - 40
        self.move(x, y)

    # ── Voice Capture & Summon ─────────────────────────────────────────────

    @pyqtSlot()
    def summon(self, start_voice: bool = True):
        """Hands-free summon via 'Hey Kate' or hotkey."""
        if start_voice:
            self.start_conversation_session()
        else:
            self.session_active = False
            self._set_capsule_mode()
            self.show()
            self.orb.set_state("idle")
            self.status_label.setText("Ready")
            if self.wake_listener:
                self.wake_listener.resume()

    @pyqtSlot(str)
    def summon_with_command(self, query: str):
        """Invoked when user speaks wake word and command in one breath ('Hey Kate <command>')."""
        self.start_conversation_session(initial_command=query)

    @pyqtSlot()
    def summon_voice(self):
        """Invoked when ambient wake-word 'Hey Kate' is detected."""
        self.start_conversation_session()

    @pyqtSlot()
    def toggle_visibility(self):
        """Toggles active conversation session on Ctrl+Space / Alt+Space."""
        now = time.time()
        if hasattr(self, "_last_toggle") and (now - self._last_toggle) < 0.4:
            return
        self._last_toggle = now

        if self.session_active:
            # Active conversation running -> Dismiss / stop on Ctrl+Space!
            self.end_conversation_session(speak_goodbye=False)
        else:
            # Wake up Kate and start continuous conversation!
            self.start_conversation_session()

    # ── Continuous Conversation Session Engine ─────────────────────────────

    def _trigger_voice_capture(self):
        """Starts live microphone listening / conversation session."""
        self.start_conversation_session()

    def start_conversation_session(self, initial_command: str = None):
        """
        Starts an uninterrupted continuous conversation session with Kate.
        Kate stays in an active listening & task-execution loop indefinitely
        until the user says 'go kate', 'bye kate', or presses Ctrl+Space.
        """
        self.session_active = True
        self.silence_count = 0

        if self.is_expanded:
            self._set_orb_mode()
        else:
            self._set_capsule_mode()

        self.show()
        self.raise_()
        self.activateWindow()

        # Force Windows foreground focus unlock
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = int(self.winId())
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
            user32.ShowWindow(hwnd, 5)
            user32.AllowSetForegroundWindow(-1)
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

        if self.wake_listener:
            self.wake_listener.pause()

        self._reset_inactivity_timer()

        if initial_command:
            play_chime("success")
            self._execute_query(initial_command)
        else:
            play_chime("trigger")
            self._start_session_voice_capture()

    def _start_session_voice_capture(self):
        """Starts live microphone listening for the ongoing conversation session."""
        if not self.session_active:
            return
        if hasattr(self, "_voice_worker") and self._voice_worker and self._voice_worker.isRunning():
            return
        if self.wake_listener:
            self.wake_listener.pause()

        self.orb.set_state("listening")
        self.status_label.setText("Listening... (say 'go kate' to stop)")
        self.toast_pill.hide()

        self._voice_worker = VoiceRecognitionWorker(timeout=7, phrase_time_limit=14)
        self._voice_worker.speech_recognized.connect(self._on_session_speech_recognized)
        self._voice_worker.speech_failed.connect(self._on_session_speech_failed)
        self._voice_worker.start()

    def _on_session_speech_recognized(self, text: str):
        """Handles user voice input during continuous conversation session."""
        if not self.session_active:
            return
        self.silence_count = 0

        # Check if voice input answers a pending confirmation modal
        if self.permission_card.isVisible():
            handled = self.permission_card.handle_voice_answer(text)
            if handled:
                self._start_session_voice_capture()
                return

        # Check for user dismissal commands (e.g. "go kate", "bye kate", "stop", "dismiss")
        dismiss_pattern = re.compile(
            r"\b(?:go\s+kate|kate\s+go|bye\s+kate|kate\s+bye|bye|goodbye|good\s+bye|sleep|stop|quit|exit|dismiss|shut\s+up|chup|bas\s+kate|bas|alvida|band\s+karo|so\s+jao|kuch\s+nahi|nothing)\b",
            re.IGNORECASE
        )
        clean = text.strip()
        if dismiss_pattern.search(clean):
            self.end_conversation_session(speak_goodbye=True)
            return

        play_chime("success")
        self.orb.set_state("thinking")
        self.status_label.setText("Thinking...")
        self._execute_query(text)

    def _on_session_speech_failed(self, err_msg: str):
        """Handles silence during continuous conversation without abruptly killing the session."""
        if not self.session_active:
            return
        self.silence_count += 1
        # Patiently loop for up to 8 cycles of silence (~56 seconds of silence)
        if self.silence_count < 8:
            self.orb.set_state("listening")
            self.status_label.setText("Listening... (say 'go kate' to stop)")
            self._start_session_voice_capture()
        else:
            # Over 56 seconds of continuous silence -> auto-sleep
            self.end_conversation_session(speak_goodbye=False)

    def end_conversation_session(self, speak_goodbye: bool = False):
        """Terminates active conversation session and returns to ambient wake listener."""
        self.session_active = False
        self.silence_count = 0
        if hasattr(self, "_voice_worker") and self._voice_worker and self._voice_worker.isRunning():
            try:
                self._voice_worker.speech_recognized.disconnect()
                self._voice_worker.speech_failed.disconnect()
            except Exception:
                pass

        if speak_goodbye and self.speaker.enabled:
            self.orb.set_state("speaking")
            self.status_label.setText("Goodbye!")
            self._add_chat_bubble("Goodbye, Satyam!", is_user=False, badge="⚡ Kate")
            self.speaker.speak(
                "Goodbye, Satyam!",
                on_done_callback=lambda: QMetaObject.invokeMethod(
                    self,
                    "_on_session_closed_safe",
                    Qt.ConnectionType.QueuedConnection
                )
            )
        else:
            self._on_session_closed_safe()

    @pyqtSlot()
    def _on_session_closed_safe(self):
        """Resets UI to idle and safely re-enables ambient wake-word listener."""
        self.orb.set_state("idle")
        self.status_label.setText("Ready")
        if self.wake_listener:
            self.wake_listener.resume()
        self._reset_inactivity_timer()

    # ── Query Dispatch & Chat Hub ──────────────────────────────────────────

    def _on_hub_submit(self):
        text = self.hub_input.text().strip()
        if not text:
            return
        self.hub_input.clear()
        self._execute_query(text)

    def _execute_query(self, query: str):
        try:
            if self.wake_listener:
                self.wake_listener.pause()
            self._add_chat_bubble(query, is_user=True)
            self._add_to_history(query)

            self.speaker.stop()
            self.orb.set_state("thinking")
            self.status_label.setText("Thinking...")

            self._active_worker = QueryWorker(query)
            self._active_worker.response_received.connect(self._on_query_response)
            self._active_worker.error_occurred.connect(self._on_query_error)
            self._active_worker.start()
        except Exception as e:
            logger.error(f"Error launching QueryWorker: {e}")
            self._on_query_error(str(e))

    def _on_query_response(self, data: dict):
        resp_msg = data.get("response") or data.get("message") or str(data)
        tier_str = data.get("tier") or data.get("model") or "⚡ Kate"

        # Check if response requires user permission
        if "permission" in resp_msg.lower() or data.get("requires_confirmation", False):
            self.orb.set_state("waiting_permission")
            self.status_label.setText("Waiting for confirmation...")
            self.permission_card.prompt_confirmation(resp_msg)
            if self.speaker.enabled:
                self.speaker.speak(resp_msg)
            return

        self._add_chat_bubble(resp_msg, is_user=False, badge=tier_str)
        self._show_toast(resp_msg[:120] + ("..." if len(resp_msg) > 120 else ""))

        self.status_label.setText("Kate answered")
        self.orb.set_state("speaking")

        if self.speaker.enabled:
            self.speaker.speak(
                resp_msg,
                on_done_callback=lambda: QMetaObject.invokeMethod(
                    self,
                    "_on_speech_done_safe",
                    Qt.ConnectionType.QueuedConnection
                )
            )
        else:
            self._on_speech_done_safe()

    @pyqtSlot()
    def _on_speech_done_safe(self):
        """Thread-safe UI handler invoked when voice speaking terminates."""
        if self.session_active:
            # Conversation is ongoing! Keep listening for the next command or follow-up!
            self.orb.set_state("listening")
            self.status_label.setText("Listening... (say 'go kate' to stop)")
            self._start_session_voice_capture()
        else:
            self._on_session_closed_safe()

    def _on_query_error(self, err_msg: str):
        self.status_label.setText("Error processing request")
        self._add_chat_bubble(f"Error: {err_msg}", is_user=False, badge="⚠️ Error")
        if self.session_active:
            self.orb.set_state("listening")
            self.status_label.setText("Listening... (say 'go kate' to stop)")
            self._start_session_voice_capture()
        else:
            self._on_session_closed_safe()

    # ── Chat Bubbles & History ─────────────────────────────────────────────

    def _add_chat_bubble(self, text: str, is_user: bool, badge: str = None):
        bubble = QFrame(self.chat_container)
        bubble.setObjectName("ChatBubble")
        if is_user:
            bubble.setStyleSheet("""
                #ChatBubble {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 rgba(0, 122, 255, 0.70), stop:1 rgba(90, 50, 240, 0.75));
                    border: 1px solid rgba(255, 255, 255, 0.22);
                    border-radius: 16px; margin-left: 50px; margin-right: 4px;
                }
            """)
        else:
            bubble.setStyleSheet("""
                #ChatBubble {
                    background: rgba(20, 22, 32, 0.78);
                    border: 1px solid rgba(255, 255, 255, 0.10);
                    border-radius: 16px; margin-right: 50px; margin-left: 4px;
                }
            """)

        b_layout = QVBoxLayout(bubble)
        b_layout.setContentsMargins(12, 8, 12, 8)
        b_layout.setSpacing(4)

        if not is_user and badge:
            badge_lbl = QLabel(badge, bubble)
            badge_lbl.setStyleSheet("color:#38BDF8; font-size:10px; font-weight:bold;")
            b_layout.addWidget(badge_lbl)

        msg_lbl = QLabel(text, bubble)
        msg_lbl.setWordWrap(True)
        msg_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        msg_lbl.setStyleSheet("color:#F8FAFC; font-size:12px; font-family:'Segoe UI',sans-serif;")
        b_layout.addWidget(msg_lbl)

        # Insert before the stretch at the end
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
        QTimer.singleShot(50, lambda: self.chat_scroll.verticalScrollBar().setValue(
            self.chat_scroll.verticalScrollBar().maximum()
        ))

    def _show_toast(self, text: str):
        self.toast_lbl.setText(text)
        self.toast_pill.show()
        QTimer.singleShot(7000, self.toast_pill.hide)

    def _add_to_history(self, query: str):
        if query not in self._chat_history_list:
            self._chat_history_list.append(query)
            item = QListWidgetItem(f"• {query[:45]}...")
            item.setData(Qt.ItemDataRole.UserRole, query)
            self.history_list.addItem(item)

    def _toggle_history_drawer(self):
        if self.history_drawer.isVisible():
            self.history_drawer.hide()
        else:
            self.history_drawer.show()

    def _on_history_item_clicked(self, item: QListWidgetItem):
        query = item.data(Qt.ItemDataRole.UserRole)
        if query:
            self._execute_query(query)

    def _open_settings_deck(self):
        dlg = SettingsDialog(self)
        dlg.exec()

    # ── Inactivity Timers & Pinning ────────────────────────────────────────

    def _reset_inactivity_timer(self):
        if self.is_pinned or self.is_expanded:
            return
        self._sleep_grace_timer.stop()
        self._inactivity_timer.stop()
        self._inactivity_timer.start(35000)  # 35 seconds

    def _on_inactivity_timeout(self):
        if self.is_pinned or self.is_expanded or not self.isVisible():
            return
        self.status_label.setText("Kate sleeping soon...")
        self._sleep_grace_timer.start(20000)

    def _on_sleep_timeout(self):
        if self.is_pinned or self.is_expanded or not self.isVisible():
            return
        self.speaker.stop()
        self.hide()
        if self.wake_listener:
            self.wake_listener.resume()
        self.dismissed.emit()

    def _toggle_pin(self):
        self.is_pinned = not self.is_pinned
        if self.is_pinned:
            self._inactivity_timer.stop()
            self._sleep_grace_timer.stop()
            self.pin_btn.setStyleSheet("""
                QPushButton {
                    background: rgba(56, 189, 248, 0.25); border: 1px solid rgba(56, 189, 248, 0.7);
                    border-radius: 16px; font-size: 12px; color: #38BDF8;
                }
            """)
            self.pin_btn.setToolTip("Pinned: Stays open indefinitely")
        else:
            self.pin_btn.setStyleSheet("""
                QPushButton {
                    background: rgba(255, 255, 255, 0.06); border: 1px solid rgba(255, 255, 255, 0.12);
                    border-radius: 16px; font-size: 12px; color: #94A3B8;
                }
            """)
            self.pin_btn.setToolTip("Pin to Screen (Keep open indefinitely)")
            self._reset_inactivity_timer()

    def _toggle_speaker(self):
        self.speaker.enabled = not self.speaker.enabled
        if self.speaker.enabled:
            self.speaker_btn.setText("🔊")
            self.speaker_btn.setStyleSheet("background: rgba(0, 245, 212, 0.15); border: 1px solid rgba(0, 245, 212, 0.35); border-radius: 16px; font-size: 13px;")
            self.speaker_btn.setToolTip("Voice Output: ON (click to mute)")
        else:
            self.speaker.stop()
            self.speaker_btn.setText("🔇")
            self.speaker_btn.setStyleSheet("background: rgba(255, 255, 255, 0.06); border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 16px; font-size: 13px; color: #64748B;")
            self.speaker_btn.setToolTip("Voice Output: OFF (click to unmute)")

    # ── Mouse Drag & Hotkeys ────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            if self.is_expanded:
                self._set_capsule_mode()
            else:
                self.speaker.stop()
                self.hide()
                self.dismissed.emit()
        else:
            super().keyPressEvent(event)
