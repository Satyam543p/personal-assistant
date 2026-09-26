# Kate ? Voice-First AI Desktop Assistant

An intelligent, context-aware, Apple Siri-inspired personal assistant built for Windows 11. Designed to operate completely hands-free via ambient voice, global hotkeys, or an expandable companion hub with hybrid local-cloud intelligence.

---

## Features

- **Ambient Voice Wake ("Hey Kate")**:
  - Continuous listening with auto-calibrated microphone hardware detection.
  - Single-breath wake invocation: say *"Hey Kate, open Brave and play I Wanna Be Yours"* and it triggers and executes immediately without asking you to repeat yourself.
  - Hardware-safe thread lifecycle with zero mic collisions.

- **Context-First Intent Intelligence**:
  - Sliding conversational turn tracking with entity resolution.
  - Follow-up continuity: bare entity names (*"I Wanna Be Yours"*) or pronouns (*"play it"*, *"in Brave"*) resolve against previous turns rather than falling back to generic chit-chat or clarification loops.

- **Dynamic Media Routing**:
  - Defaults to **YouTube in Brave** for unspecified music requests (*"play a song"*).
  - Honors explicit platforms (Spotify, SoundCloud, YouTube Music).

- **Simultaneous Multi-Task Execution**:
  - Composite request decomposition (*and*, *then*, *also*, *aur*, *phir*).
  - Parallel task execution via asynchronous concurrency (syncio.gather).

- **Floating Apple Siri UI & Companion Hub**:
  - Translucent floating capsule with sound wave visuals and glow orbs.
  - Expandable Companion Hub with full markdown chat stream, slide-out chat history, and in-app settings deck for API keys and voice customization.
  - System Tray integration with instant shortcuts (Ctrl + Space / Alt + Space).

- **Hybrid Tiered AI Routing**:
  - Fast-Path Rule Engine: < 1ms execution for high-confidence local commands.
  - Local Models: Ollama / llama.cpp (Qwen 2.5).
  - Cloud Cascading: Grok, Gemini, OpenAI, or OpenRouter fallback.

---

## Architecture

`
personal-assistant/
|-- assistant/
|   |-- context_engine.py    # Multi-turn dialogue & entity memory
|   |-- interpreter.py       # Intent parsing & composite query splitting
|   |-- router.py            # Direct tool dispatch & multi-task execution
|   |-- desktop_tools.py     # System controls, browser & media launchers
|   |-- voice_wake.py        # Ambient 'Hey Kate' listener & mic calibration
|   |-- daemon.py            # Local FastAPI / HTTP IPC daemon server
|   |-- config.py            # System configuration & environment settings
|   |-- ui/
|   |   |-- app.py           # UI application entry point & tray manager
|   |   |-- siri_window.py   # Floating capsule & expandable companion hub
|   |   |-- hotkey_service.py# Multi-layer global keyboard hook listener
|   |   -- settings_window.py# In-app settings & API key management
|-- run_kate.bat             # One-click Windows launcher
-- README.md
`

---

## Quick Start

### 1. Prerequisites
- **OS**: Windows 10 / 11
- **Python**: 3.10+ (Recommended: Python 3.11)
- Working Microphone & Speaker

### 2. Setup
`ash
git clone https://github.com/Satyam543p/personal-assistant.git
cd personal-assistant
pip install -r requirements.txt
`

### 3. Run
Start Kate using the launcher:
`ash
run_kate.bat
`
Or manually run the two services:
`ash
# Terminal 1: Daemon
python assistant/daemon.py

# Terminal 2: UI & Ambient Voice
python assistant/ui/app.py
`

---

## Configuration

Create a .env file in the root directory (optional, or configure inside the app UI via Settings):
`ini
# Optional Cloud Keys
GEMINI_API_KEY=your_key_here
XAI_API_KEY=your_key_here
OPENAI_API_KEY=your_key_here

# Hotkeys
JARVIS_SIRI_HOTKEY=ctrl+space
`

---

## License
MIT License
