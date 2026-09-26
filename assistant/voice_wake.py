"""
Continuous Ambient Wake-Word Listener for Kate ("Hey Kate" / "Hey Jarvis").
Runs non-blocking in a background thread using SpeechRecognition audio listener.
Includes automatic live microphone detection and pause/resume capability.
"""

import os
import sys
import time
import logging
import threading
import re
from PyQt6.QtCore import QObject, pyqtSignal, QMetaObject, Qt, Q_ARG

logger = logging.getLogger("kate.wake_word")

# Broad wake word patterns including Indian English / Hinglish pronunciations of Kate
# Broad wake word patterns including Indian English / Hinglish pronunciations
WAKE_INVOCATION_REGEX = re.compile(
    r"\b(?:hey\s+|hi\s+|hello\s+|ok\s+|okay\s+|listen\s+|wake\s+up\s+)?(?:kate|cate|kayt|kait|kat|ket|keth|keith|kathy|kade|packet|cricket|ticket|pocket|cake|kite|cat|jarvis)\b[,:\s]*",
    re.IGNORECASE
)

WAKE_REGEX = re.compile(
    r"\b(?:hey\s+|hi\s+|hello\s+|ok\s+|listen\s+|wake\s+up\s+)?(?:kate|cate|kayt|kait|kat|ket|keth|keith|kit|cat|kite|cake|kathy|kade|packet|cricket|ticket|pocket|jarvis)\b",
    re.IGNORECASE
)


_CACHED_WORKING_MIC = None


def find_working_microphone(force_refresh: bool = False) -> int:
    """
    Probes available audio input devices and returns the device index
    that has active, live energy input (bypassing dead virtual devices/Sound Mapper).
    Caches the result so ambient wake listener and UI voice capture share the exact same device.
    """
    global _CACHED_WORKING_MIC
    if _CACHED_WORKING_MIC is not None and not force_refresh:
        return _CACHED_WORKING_MIC

    import speech_recognition as sr
    import audioop

    names = sr.Microphone.list_microphone_names()
    candidates = []

    for i, name in enumerate(names):
        lower_name = name.lower()
        if any(w in lower_name for w in ['output', 'speaker', 'stereo mix', 'mapper']):
            continue
        try:
            with sr.Microphone(device_index=i) as source:
                energies = []
                for _ in range(5):
                    time.sleep(0.02)
                    buf = source.stream.read(source.CHUNK)
                    energies.append(audioop.rms(buf, source.SAMPLE_WIDTH))
                avg_e = sum(energies) / len(energies)
                if avg_e > 20:
                    candidates.append((avg_e, i, name))
        except Exception:
            pass

    if candidates:
        candidates.sort(reverse=True)
        best_idx, best_name = candidates[0][1], candidates[0][2]
        logger.info(f"Auto-selected live microphone [Index {best_idx}]: {best_name} (RMS={candidates[0][0]:.1f})")
        _CACHED_WORKING_MIC = best_idx
        return best_idx

    # Fallback to system default input device from PyAudio
    try:
        import pyaudio
        p = pyaudio.PyAudio()
        default_info = p.get_default_input_device_info()
        p.terminate()
        default_idx = default_info.get('index')
        if default_idx is not None and default_idx < len(names):
            _CACHED_WORKING_MIC = int(default_idx)
            return _CACHED_WORKING_MIC
    except Exception:
        pass

    fallback = 6 if len(names) > 6 else (1 if len(names) > 1 else 0)
    _CACHED_WORKING_MIC = fallback
    return fallback


class WakeWordListener(QObject):
    """
    Background ambient voice monitor for 'Hey Kate'.
    Emits `wake_word_detected` signal on match.
    """
    wake_word_detected = pyqtSignal()
    wake_command_detected = pyqtSignal(str)

    def __init__(self, target_window=None, parent=None):
        super().__init__(parent)
        self._target_window = target_window
        self._running = False
        self._paused = False
        self._thread = None
        self._stop_listening_fn = None
        self._last_trigger_time = 0.0
        self._mic_index = None
        self._is_recognizing = False

    def set_window(self, window):
        self._target_window = window

    def pause(self):
        """Temporarily suspend wake-word processing and cleanly release the PyAudio microphone stream."""
        self._paused = True
        if self._stop_listening_fn:
            try:
                self._stop_listening_fn(wait_for_stop=True)
                self._stop_listening_fn = None
            except Exception:
                pass
        logger.info("WakeWordListener: Mic hardware released (paused).")

    def resume(self):
        """Resume ambient wake-word listening."""
        self._paused = False
        if self._running and self._stop_listening_fn is None:
            self._start_listening_stream()
        logger.info("WakeWordListener: Ambient listener resumed.")

    def start(self):
        if self._running:
            return
        self._running = True

        self._thread = threading.Thread(target=self._worker, daemon=True, name="KateWakeWordWorker")
        self._thread.start()
        logger.info("WakeWordListener started for 'Hey Kate' / 'Hey Jarvis'.")

    def stop(self):
        self._running = False
        if self._stop_listening_fn:
            try:
                self._stop_listening_fn(wait_for_stop=True)
                self._stop_listening_fn = None
            except Exception:
                pass

    def _worker(self):
        if os.name == "nt":
            try:
                import win32service, win32con, ctypes
                hdesk = win32service.OpenDesktop("Default", 0, False, win32con.MAXIMUM_ALLOWED)
                if hdesk:
                    ctypes.windll.user32.SetThreadDesktop(int(hdesk))
            except Exception:
                pass

        try:
            import speech_recognition as sr

            # Discover the active live microphone hardware device
            self._mic_index = find_working_microphone()
            print(f"[Kate WakeWord] Binding to microphone device index: {self._mic_index}")

            r = sr.Recognizer()
            r.operation_timeout = 8           # Socket timeout to prevent hung recognition threads
            r.pause_threshold = 0.5           # Snappy end-of-phrase detection
            r.phrase_threshold = 0.2          # Catch short quick words like "Kate"
            r.non_speaking_duration = 0.3
            r.dynamic_energy_threshold = False # Keep fixed threshold to prevent sensitivity drift
            r.energy_threshold = 120           # Perfect sensitivity for spoken voice

            mic = sr.Microphone(device_index=self._mic_index)
            with mic as source:
                r.adjust_for_ambient_noise(source, duration=0.6)
                # Calibrate sensitivity to reliably detect normal room speaking volume
                r.energy_threshold = max(70, min(r.energy_threshold + 40, 220))
                print(f"[Kate WakeWord] Calibrated mic energy threshold: {r.energy_threshold:.1f}")

            def audio_callback(recognizer, audio):
                if not self._running or self._paused or self._is_recognizing:
                    return
                self._is_recognizing = True
                # Offload recognition to separate daemon thread guarded against accumulation
                threading.Thread(
                    target=self._run_recognition_task,
                    args=(recognizer, audio),
                    daemon=True,
                    name="KateWakeRecognizer"
                ).start()

            self._recognizer = r
            self._mic_source = mic
            self._audio_callback = audio_callback
            self._start_listening_stream()
            print("[Kate WakeWord] Continuous ambient listener is active & listening...")

            while self._running:
                time.sleep(0.5)

        except Exception as e:
            logger.warning(f"Continuous wake-word engine failed to initialize: {e}")
            print(f"[Kate WakeWord] Warning: Mic listener could not start ({e}). Hotkeys still active.")

    def _start_listening_stream(self):
        if hasattr(self, "_recognizer") and hasattr(self, "_mic_source") and hasattr(self, "_audio_callback"):
            if self._stop_listening_fn is not None:
                try:
                    self._stop_listening_fn(wait_for_stop=False)
                except Exception:
                    pass
                self._stop_listening_fn = None
            try:
                self._stop_listening_fn = self._recognizer.listen_in_background(
                    self._mic_source, self._audio_callback, phrase_time_limit=4
                )
            except Exception as e:
                logger.debug(f"Restarting listening stream error: {e}")

    def _run_recognition_task(self, recognizer, audio):
        """Worker wrapper ensuring self._is_recognizing is safely cleared even on unhandled exception."""
        try:
            self._recognize_and_check(recognizer, audio)
        finally:
            self._is_recognizing = False

    def _recognize_and_check(self, recognizer, audio):
        """Recognizes audio chunk with language fallback and triggers if wake-word is found."""
        if self._paused:
            return

        import speech_recognition as sr

        text = None
        # Try Indian English first (best for Hinglish / Indian accents)
        try:
            text = recognizer.recognize_google(audio, language="en-IN").lower().strip()
        except sr.UnknownValueError:
            pass
        except Exception:
            pass

        # If not recognized, try standard en-US
        if not text:
            try:
                text = recognizer.recognize_google(audio, language="en-US").lower().strip()
            except sr.UnknownValueError:
                pass
            except Exception:
                pass

        if text and not self._paused:
            print(f"[Kate WakeWord] Heard: '{text}'", flush=True)
            m = WAKE_INVOCATION_REGEX.search(text)
            if m:
                # Command is everything after the wake phrase
                cmd = text[m.end():].strip()
                cmd = re.sub(r"^[,:\s]+", "", cmd).strip()
                if cmd and len(cmd) >= 2:
                    print(f"[Kate WakeWord] ✨ Wake word + command detected: '{cmd}'! Executing...", flush=True)
                    self._trigger_with_command(cmd)
                else:
                    print(f"[Kate WakeWord] ✨ Wake word matched in '{text}'! Summoning Kate...", flush=True)
                    self._trigger()

    def _trigger_with_command(self, cmd: str):
        """Play Siri chime and execute command on Qt main thread without asking user to repeat."""
        if self._paused:
            return
        now = time.time()
        if (now - self._last_trigger_time) < 1.0:
            return
        self._last_trigger_time = now

        try:
            import winsound
            winsound.Beep(580, 80)
            winsound.Beep(780, 100)
        except Exception:
            pass

        if self._target_window:
            QMetaObject.invokeMethod(
                self._target_window,
                "summon_with_command",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, cmd)
            )
        self.wake_command_detected.emit(cmd)

    def _trigger(self):
        """Play Siri chime and summon window on Qt main thread."""
        if self._paused:
            return
        now = time.time()
        if (now - self._last_trigger_time) < 1.0:
            return
        self._last_trigger_time = now

        try:
            import winsound
            winsound.Beep(520, 70)
            winsound.Beep(720, 90)
        except Exception:
            pass

        if self._target_window:
            QMetaObject.invokeMethod(
                self._target_window,
                "summon_voice",
                Qt.ConnectionType.QueuedConnection
            )
        self.wake_word_detected.emit()
