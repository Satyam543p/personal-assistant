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
import sys
import os
import re
import time
import asyncio
import logging
import threading
import tempfile
import subprocess
from PyQt6.QtCore import (
    Qt, QTimer, QUrl, QRectF, pyqtSignal, pyqtSlot, QThread, QPoint
)
from PyQt6.QtGui import (
    QPainter, QColor, QRadialGradient, QLinearGradient, QPen, QBrush,
    QPainterPath, QFont, QKeyEvent, QCursor
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QTextEdit,
    QPushButton, QLabel, QFrame, QGraphicsDropShadowEffect, QApplication,
    QScrollArea, QSplitter, QSizePolicy, QListWidget, QListWidgetItem
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput

try:
    from assistant.ipc import HTTPIPCClient
    from assistant.voice_wake import find_working_microphone
    from assistant.ui.settings_window import SettingsDialog
    import assistant.config as config
except ModuleNotFoundError:
    from ipc import HTTPIPCClient
    from voice_wake import find_working_microphone
    from ui.settings_window import SettingsDialog
    import config

logger = logging.getLogger("kate.ui.siri")


# ─────────────────────────────────────────────────────────────────────────────
# Audio Chimes
# ─────────────────────────────────────────────────────────────────────────────

def play_chime(kind: str = "trigger"):
    """Plays Apple Siri-style audio cues via native Windows sound (non-blocking)."""
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
        self.enabled = True
        self._sapi_voice = None
        self._edge_available = False
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
            return
        clean = self._strip_markdown(text)
        clean = self._trim_for_speech(clean)
        if not clean:
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

            loop = asyncio.new_event_loop()
            mp3_path = loop.run_until_complete(_gen())
            loop.close()

            if not pygame.mixer.get_init():
                pygame.mixer.init()
            pygame.mixer.music.load(mp3_path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                time.sleep(0.05)

            try:
                os.remove(mp3_path)
            except Exception:
                pass

            if on_done_callback:
                on_done_callback()
        except Exception as e:
            logger.warning(f"Edge TTS / pygame playback error: {e}, using SAPI.")
            self._speak_sapi(text, on_done_callback)

    def _speak_sapi(self, text, on_done_callback):
        if self._sapi_voice:
            try:
                self._sapi_voice.Speak(text, 1 | 2)
            except Exception:
                pass
        if on_done_callback:
            on_done_callback()

    def stop(self):
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

    def run(self):
        try:
            import speech_recognition as sr
            r = sr.Recognizer()
            r.operation_timeout = 8
            r.pause_threshold = 0.8
            r.phrase_threshold = 0.2
            r.dynamic_energy_threshold = True

            mic_idx = find_working_microphone()
            try:
                with sr.Microphone(device_index=mic_idx) as source:
                    r.adjust_for_ambient_noise(source, duration=0.2)
                    r.energy_threshold = max(300.0, r.energy_threshold)
                    audio = r.listen(source, timeout=7, phrase_time_limit=12)
            except Exception:
                mic_idx = find_working_microphone(force_refresh=True)
                with sr.Microphone(device_index=mic_idx) as source:
                    r.adjust_for_ambient_noise(source, duration=0.2)
                    r.energy_threshold = max(300.0, r.energy_threshold)
                    audio = r.listen(source, timeout=7, phrase_time_limit=12)

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
# Clickable Liquid Mercury Glowing Orb
# ─────────────────────────────────────────────────────────────────────────────

class LiquidMercuryOrb(QWidget):
    """
    Liquid Mercury Floating Orb:
      - Photorealistic liquid metal / quicksilver shader aesthetic
      - Surface tension ripple waves responding dynamically to voice & sound
      - Rotating constellation orbit nodes during 'thinking' / query processing
      - Pure standalone floating droplet with zero surrounding box/pill
    """
    clicked = pyqtSignal()

    def __init__(self, parent=None, size=80):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setToolTip("Kate (Click for Chat & Settings)")
        self.phase = 0.0
        self.current_state = "idle"  # idle, listening, thinking, speaking, waiting_permission
        self.is_hovered = False
        self.audio_energy = 0.0

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_animation)
        self.timer.start(20)  # 50 FPS silky smooth animation

    def set_state(self, state: str):
        self.current_state = state
        self.update()

    def set_audio_energy(self, level: float):
        self.audio_energy = max(0.0, min(1.0, level))
        self.update()

    def _update_animation(self):
        speeds = {
            "thinking": 0.08,
            "listening": 0.09,
            "speaking": 0.07,
            "waiting_permission": 0.06,
            "idle": 0.025
        }
        self.phase = (self.phase + speeds.get(self.current_state, 0.025)) % (2 * math.pi)
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
        if event.button() == Qt.MouseButton.LeftButton and hasattr(self, "_press_pos"):
            diff = (event.globalPosition().toPoint() - self._press_pos).manhattanLength()
            if diff < 6:
                play_chime("confirm")
                self.clicked.emit()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0

        is_listening = self.current_state == "listening"
        is_thinking = self.current_state == "thinking"
        is_speaking = self.current_state == "speaking"
        is_permission = self.current_state == "waiting_permission"

        # Base radius
        base_radius = (min(w, h) / 2.0) - 12.0
        if self.is_hovered:
            base_radius += 1.5

        # ── 1. Outer Platinum-Silver Luminescence / Ambient Glow ────────
        halo_alpha = 150 if is_listening else (130 if is_thinking else (100 if is_speaking else (70 if self.is_hovered else 45)))
        halo_r = base_radius + 11.0
        halo_grad = QRadialGradient(cx, cy, halo_r)
        if is_permission:
            halo_grad.setColorAt(0.0, QColor(255, 190, 60, halo_alpha))
            halo_grad.setColorAt(0.6, QColor(255, 100, 40, halo_alpha // 2))
        elif is_listening:
            halo_grad.setColorAt(0.0, QColor(220, 240, 255, halo_alpha))
            halo_grad.setColorAt(0.5, QColor(160, 200, 255, halo_alpha // 2))
        else:
            halo_grad.setColorAt(0.0, QColor(240, 245, 255, halo_alpha))
            halo_grad.setColorAt(0.5, QColor(180, 195, 220, halo_alpha // 2))
        halo_grad.setColorAt(1.0, QColor(0, 0, 0, 0))

        painter.setBrush(QBrush(halo_grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(cx - halo_r, cy - halo_r, halo_r * 2, halo_r * 2))

        # ── 2. Fluid Surface Wave Path (Liquid Mercury Droplet Physics) ──
        wave_amp = 3.8 if is_listening else (2.4 if is_speaking else (1.4 if self.is_hovered else 0.8))
        num_pts = 48
        path = QPainterPath()
        first_pt = None

        for k in range(num_pts):
            theta = k * (2.0 * math.pi / num_pts)
            wave1 = math.sin(3.0 * theta + self.phase * 3.2) * wave_amp
            wave2 = math.cos(5.0 * theta - self.phase * 2.1) * (wave_amp * 0.45)
            r = base_radius + wave1 + wave2
            px = cx + math.cos(theta) * r
            py = cy + math.sin(theta) * r
            if k == 0:
                path.moveTo(px, py)
                first_pt = (px, py)
            else:
                path.lineTo(px, py)

        if first_pt:
            path.lineTo(first_pt[0], first_pt[1])
        path.closeSubpath()

        # ── 3. Liquid Mercury Metallic Core ──────────────────────────────
        core_grad = QRadialGradient(cx - base_radius * 0.22, cy - base_radius * 0.25, base_radius * 1.15)
        if is_permission:
            core_grad.setColorAt(0.0, QColor(255, 245, 200, 255))
            core_grad.setColorAt(0.25, QColor(255, 195, 70, 250))
            core_grad.setColorAt(0.60, QColor(220, 110, 20, 245))
            core_grad.setColorAt(0.85, QColor(100, 40, 10, 250))
            core_grad.setColorAt(1.0, QColor(20, 10, 5, 255))
        else:
            # Pure Liquid Mercury / Quicksilver Platinum
            core_grad.setColorAt(0.0, QColor(255, 255, 255, 255))   # Specular highlight
            core_grad.setColorAt(0.18, QColor(241, 245, 249, 250))  # Liquid platinum
            core_grad.setColorAt(0.42, QColor(203, 213, 225, 245))  # Silver chrome
            core_grad.setColorAt(0.70, QColor(100, 116, 139, 245))  # Metallic steel
            core_grad.setColorAt(0.88, QColor(30, 41, 59, 250))     # Dark graphite refraction
            core_grad.setColorAt(1.0, QColor(8, 12, 18, 255))       # Deep obsidian liquid edge

        painter.setBrush(QBrush(core_grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(path)

        # ── 4. Liquid Chromatic Iridescence Gleam ────────────────────────
        gleam_x = cx + math.cos(self.phase * 1.5) * (base_radius * 0.25)
        gleam_y = cy + math.sin(self.phase * 1.5) * (base_radius * 0.20)
        gleam_grad = QRadialGradient(gleam_x, gleam_y, base_radius * 0.70)
        gleam_grad.setColorAt(0.0, QColor(255, 255, 255, 130))
        gleam_grad.setColorAt(0.35, QColor(186, 230, 253, 75))   # Icy cyan sheen
        gleam_grad.setColorAt(0.70, QColor(216, 180, 254, 50))   # Soft violet dispersion
        gleam_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(QBrush(gleam_grad))
        painter.drawPath(path)

        # ── 5. Specular Liquid Highlights (Zero-G Droplet Reflections) ───
        hi_x = cx - base_radius * 0.28
        hi_y = cy - base_radius * 0.32
        hi_w = base_radius * 0.70
        hi_h = base_radius * 0.45
        hi_grad = QLinearGradient(hi_x, hi_y - hi_h / 2, hi_x, hi_y + hi_h / 2)
        hi_grad.setColorAt(0.0, QColor(255, 255, 255, 240))
        hi_grad.setColorAt(0.5, QColor(255, 255, 255, 110))
        hi_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(QBrush(hi_grad))
        painter.drawEllipse(QRectF(hi_x - hi_w / 2, hi_y - hi_h / 2, hi_w, hi_h))

        # Secondary bottom bounce rim light
        bounce_grad = QLinearGradient(cx, cy + base_radius * 0.5, cx, cy + base_radius)
        bounce_grad.setColorAt(0.0, QColor(255, 255, 255, 0))
        bounce_grad.setColorAt(1.0, QColor(226, 232, 240, 110))
        painter.setBrush(QBrush(bounce_grad))
        painter.drawEllipse(QRectF(cx - base_radius * 0.6, cy + base_radius * 0.55, base_radius * 1.2, base_radius * 0.35))

        # Precision liquid mercury edge stroke
        edge_pen = QPen(QColor(255, 255, 255, 190), 1.2)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(edge_pen)
        painter.drawPath(path)

        # ── 6. Rotating Constellation Loading Ring (Processing / Thinking Mode) ──
        # Exact reproduction of user reference photo media_1790488988537.png
        if is_thinking:
            num_nodes = 7
            orbit_r = base_radius + 7.5
            for i in range(num_nodes):
                ang = self.phase * 3.0 + (i * 2.0 * math.pi / num_nodes)
                nx = cx + math.cos(ang) * orbit_r
                ny = cy + math.sin(ang) * orbit_r
                node_size = 3.6 + 1.6 * math.sin(self.phase * 4.0 + i)

                node_glow = QRadialGradient(nx, ny, node_size + 3.0)
                node_glow.setColorAt(0.0, QColor(255, 255, 255, 255))
                node_glow.setColorAt(0.5, QColor(226, 232, 240, 190))
                node_glow.setColorAt(1.0, QColor(255, 255, 255, 0))

                painter.setBrush(QBrush(node_glow))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(QRectF(nx - node_size - 3, ny - node_size - 3, (node_size + 3) * 2, (node_size + 3) * 2))


# Backward compatibility alias
SiriGlowOrb = LiquidMercuryOrb


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
        self.is_pinned = False
        self.is_expanded = False
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
        self.main_layout.setContentsMargins(10, 10, 10, 10)
        self.main_layout.setSpacing(6)

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
        self.hub_mic_btn.clicked.connect(self._trigger_voice_capture)
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
        orb_box.setContentsMargins(3, 3, 3, 3)
        orb_box.setSpacing(0)
        orb_box.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Standalone Liquid Mercury Droplet (Click expands to Hub)
        self.orb = LiquidMercuryOrb(self.orb_container, size=80)
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
        self.orb_container.show()
        self.setFixedSize(86, 86)
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
        self.companion_hub.show()
        self.setFixedSize(460, 580)
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

        self._reset_inactivity_timer()
        if start_voice:
            self._trigger_voice_capture()
        else:
            self.orb.set_state("idle")
            self.status_label.setText("Ready")

    @pyqtSlot(str)
    def summon_with_command(self, query: str):
        """Invoked when user speaks wake word and command in one breath ('Hey Kate <command>')."""
        if self.is_expanded:
            self.hub_input.clear()
            self._set_orb_mode()
        else:
            self._set_orb_mode()

        self.show()
        self.raise_()
        self.activateWindow()
        self._reset_inactivity_timer()
        if self.wake_listener:
            self.wake_listener.pause()
        play_chime("success")
        self._execute_query(query)

    @pyqtSlot()
    def summon_voice(self):
        """Invoked when ambient wake-word 'Hey Kate' is detected."""
        if self.is_expanded:
            self.hub_input.clear()
            self._set_orb_mode()
        self.summon(start_voice=True)

    @pyqtSlot()
    def toggle_visibility(self):
        now = time.time()
        if hasattr(self, "_last_toggle") and (now - self._last_toggle) < 0.4:
            return
        self._last_toggle = now

        if self.isVisible():
            self._inactivity_timer.stop()
            self._sleep_grace_timer.stop()
            self.speaker.stop()
            self.hide()
            if self.wake_listener:
                self.wake_listener.resume()
            self.dismissed.emit()
        else:
            self.summon(start_voice=True)

    def _trigger_voice_capture(self):
        """Starts live microphone listening with liquid ripple animation."""
        if self.wake_listener:
            self.wake_listener.pause()

        play_chime("trigger")
        self.orb.set_state("listening")
        self.status_label.setText("Listening... speak now")
        self.toast_pill.hide()

        self._voice_worker = VoiceRecognitionWorker()
        self._voice_worker.speech_recognized.connect(self._on_voice_recognized)
        self._voice_worker.speech_failed.connect(self._on_voice_failed)
        self._voice_worker.start()

    def _on_voice_recognized(self, text: str):
        play_chime("success")
        self.orb.set_state("thinking")
        self.status_label.setText("Thinking...")

        # Check if this voice input was an answer to a pending permission modal!
        if self.permission_card.isVisible():
            handled = self.permission_card.handle_voice_answer(text)
            if handled:
                if self.wake_listener:
                    self.wake_listener.resume()
                self._reset_inactivity_timer()
                return

        # Execute query
        self._execute_query(text)

    def _on_voice_failed(self, err_msg: str):
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
                on_done_callback=lambda: (
                    self.orb.set_state("idle"),
                    self.status_label.setText("Ready"),
                    self._on_response_complete()
                )
            )
        else:
            self.orb.set_state("idle")
            self._on_response_complete()

    def _on_response_complete(self):
        if self.wake_listener:
            self.wake_listener.resume()
        self._reset_inactivity_timer()

    def _on_query_error(self, err_msg: str):
        self.orb.set_state("idle")
        self.status_label.setText("Error processing request")
        self._add_chat_bubble(f"Error: {err_msg}", is_user=False, badge="⚠️ Error")
        if self.wake_listener:
            self.wake_listener.resume()
        self._reset_inactivity_timer()

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
