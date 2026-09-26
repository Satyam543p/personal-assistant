"""
Settings & Configuration Deck for Kate Assistant.
Allows user to view/modify:
- API Keys (Gemini, Groq, OpenRouter)
- Voice Model & Rate
- Hotkey & Wake Word
- Autostart on Windows Boot
"""

import os
import sys
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QComboBox, QSlider, QGroupBox,
    QFormLayout, QMessageBox, QFrame
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QColor

try:
    from assistant.autostart import is_autostart_enabled, enable_autostart, disable_autostart
    import assistant.config as config
except ModuleNotFoundError:
    from autostart import is_autostart_enabled, enable_autostart, disable_autostart
    import config


class SettingsDialog(QDialog):
    """Modern dark-themed Settings Deck for Kate."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kate Settings & Control Deck")
        self.setFixedSize(500, 560)
        self.setStyleSheet("""
            QDialog {
                background-color: #0F172A;
                color: #F8FAFC;
                font-family: 'Segoe UI', system-ui, sans-serif;
            }
            QGroupBox {
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 10px;
                margin-top: 14px;
                padding-top: 14px;
                font-weight: 600;
                color: #38BDF8;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 14px;
                padding: 0 5px;
            }
            QLabel {
                color: #94A3B8;
                font-size: 13px;
            }
            QLineEdit {
                background-color: rgba(30, 41, 59, 0.9);
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 6px;
                padding: 6px 10px;
                color: #F8FAFC;
                font-size: 13px;
            }
            QLineEdit:focus {
                border: 1px solid #38BDF8;
            }
            QComboBox {
                background-color: rgba(30, 41, 59, 0.9);
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 6px;
                padding: 5px 10px;
                color: #F8FAFC;
            }
            QCheckBox {
                color: #F8FAFC;
                font-size: 13px;
                spacing: 8px;
            }
            QPushButton#saveBtn {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #00F2FE, stop:1 #4FACFE);
                color: #0F172A;
                font-weight: bold;
                border: none;
                border-radius: 8px;
                padding: 10px 24px;
                font-size: 14px;
            }
            QPushButton#saveBtn:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #38F9D7, stop:1 #43E97B);
            }
            QPushButton#cancelBtn {
                background: rgba(255, 255, 255, 0.1);
                color: #94A3B8;
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 8px;
                padding: 10px 20px;
                font-size: 14px;
            }
            QPushButton#cancelBtn:hover {
                background: rgba(255, 255, 255, 0.18);
                color: #FFF;
            }
        """)

        self._env_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            ".env"
        )
        self._init_ui()
        self._load_current_values()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        # Header
        hdr = QLabel("✨ Kate Assistant Settings")
        hdr.setStyleSheet("font-size: 18px; font-weight: 700; color: #F8FAFC; margin-bottom: 2px;")
        layout.addWidget(hdr)

        # ── Group 1: API Keys ──────────────────────────────────────────
        grp_keys = QGroupBox("Cloud & Model API Keys")
        form_keys = QFormLayout(grp_keys)
        form_keys.setContentsMargins(14, 16, 14, 14)
        form_keys.setSpacing(10)

        self.txt_gemini = QLineEdit()
        self.txt_gemini.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_gemini.setPlaceholderText("AIzaSy...")
        form_keys.addRow("Gemini API Key:", self.txt_gemini)

        self.txt_groq = QLineEdit()
        self.txt_groq.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_groq.setPlaceholderText("gsk_...")
        form_keys.addRow("Groq API Key:", self.txt_groq)

        self.txt_openrouter = QLineEdit()
        self.txt_openrouter.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_openrouter.setPlaceholderText("sk-or-...")
        form_keys.addRow("OpenRouter Key:", self.txt_openrouter)

        layout.addWidget(grp_keys)

        # ── Group 2: Voice & Personality ───────────────────────────────
        grp_voice = QGroupBox("Neural Voice & Interaction")
        form_voice = QFormLayout(grp_voice)
        form_voice.setContentsMargins(14, 16, 14, 14)
        form_voice.setSpacing(10)

        self.combo_voice = QComboBox()
        self.combo_voice.addItem("Aria (en-US Neural - Warm & Expressive)", "en-US-AriaNeural")
        self.combo_voice.addItem("Jenny (en-US Neural - Natural Female)", "en-US-JennyNeural")
        self.combo_voice.addItem("Neerja (en-IN Neural - Indian English)", "en-IN-NeerjaNeural")
        self.combo_voice.addItem("Guy (en-US Neural - Male)", "en-US-GuyNeural")
        form_voice.addRow("Kate Voice:", self.combo_voice)

        self.txt_hotkey = QLineEdit()
        self.txt_hotkey.setPlaceholderText("ctrl+space")
        form_voice.addRow("Summon Hotkey:", self.txt_hotkey)

        layout.addWidget(grp_voice)

        # ── Group 3: System & Boot ─────────────────────────────────────
        grp_sys = QGroupBox("System Behavior")
        layout_sys = QVBoxLayout(grp_sys)
        layout_sys.setContentsMargins(14, 16, 14, 14)
        layout_sys.setSpacing(8)

        self.chk_autostart = QCheckBox("Run Kate automatically on Windows startup (Background)")
        layout_sys.addWidget(self.chk_autostart)

        self.chk_wake = QCheckBox("Enable Ambient Wake-Word ('Hey Kate')")
        self.chk_wake.setChecked(True)
        layout_sys.addWidget(self.chk_wake)

        layout.addWidget(grp_sys)

        layout.addStretch()

        # ── Buttons ────────────────────────────────────────────────────
        btn_box = QHBoxLayout()
        btn_box.addStretch()

        btn_cancel = QPushButton("Cancel")
        btn_cancel.setObjectName("cancelBtn")
        btn_cancel.clicked.connect(self.reject)
        btn_box.addWidget(btn_cancel)

        btn_save = QPushButton("Save & Apply")
        btn_save.setObjectName("saveBtn")
        btn_save.clicked.connect(self._on_save)
        btn_box.addWidget(btn_save)

        layout.addLayout(btn_box)

    def _load_current_values(self):
        self.txt_gemini.setText(os.environ.get("GEMINI_API_KEY", getattr(config, "GEMINI_API_KEY", "")))
        self.txt_groq.setText(os.environ.get("GROQ_API_KEY", ""))
        self.txt_openrouter.setText(os.environ.get("OPENROUTER_API_KEY", getattr(config, "OPENROUTER_API_KEY", "")))
        self.txt_hotkey.setText(getattr(config, "SIRI_HOTKEY", "ctrl+space"))
        self.chk_autostart.setChecked(is_autostart_enabled())

    def _on_save(self):
        gemini = self.txt_gemini.text().strip()
        groq = self.txt_groq.text().strip()
        openrouter = self.txt_openrouter.text().strip()
        hotkey = self.txt_hotkey.text().strip() or "ctrl+space"
        autostart = self.chk_autostart.isChecked()

        # Save to environment
        if gemini: os.environ["GEMINI_API_KEY"] = gemini
        if groq: os.environ["GROQ_API_KEY"] = groq
        if openrouter: os.environ["OPENROUTER_API_KEY"] = openrouter
        os.environ["JARVIS_SIRI_HOTKEY"] = hotkey

        # Update .env file
        try:
            env_lines = {}
            if os.path.exists(self._env_path):
                with open(self._env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if "=" in line and not line.strip().startswith("#"):
                            k, v = line.strip().split("=", 1)
                            env_lines[k.strip()] = v.strip()

            if gemini: env_lines["GEMINI_API_KEY"] = gemini
            if groq: env_lines["GROQ_API_KEY"] = groq
            if openrouter: env_lines["OPENROUTER_API_KEY"] = openrouter
            env_lines["JARVIS_SIRI_HOTKEY"] = hotkey

            with open(self._env_path, "w", encoding="utf-8") as f:
                for k, v in env_lines.items():
                    f.write(f"{k}={v}\n")
        except Exception as e:
            QMessageBox.warning(self, "Save Warning", f"Could not write .env file: {e}")

        # Update Autostart
        if autostart != is_autostart_enabled():
            if autostart:
                enable_autostart()
            else:
                disable_autostart()

        QMessageBox.information(self, "Kate Settings", "Settings saved successfully! New configurations applied.")
        self.accept()
