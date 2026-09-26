"""
Build script to compile Kate Assistant into a standalone Windows Executable (.exe).
Uses PyInstaller to bundle Python runtime, PyQt6, audio subsystems, and tools.
"""

import os
import sys
import subprocess

def build():
    repo_root = os.path.dirname(os.path.abspath(__file__))
    entry_point = os.path.join(repo_root, "launch_siri.py")
    
    hidden_imports = [
        "PyQt6",
        "PyQt6.QtCore",
        "PyQt6.QtGui",
        "PyQt6.QtWidgets",
        "PyQt6.QtMultimedia",
        "edge_tts",
        "speech_recognition",
        "win32gui",
        "win32con",
        "win32api",
        "win32process",
        "win32clipboard",
        "reportlab",
        "dotenv",
        "yt_dlp",
        "PIL",
        "PIL.ImageGrab",
        "keyboard",
        "pynput",
        "pynput.keyboard",
        "psutil",
        "pyaudio",
        "google.genai"
    ]
    
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name=KateAssistant",
        "--onefile",
        "--windowed",
        f"--paths={repo_root}",
    ]
    
    for hi in hidden_imports:
        cmd.extend(["--hidden-import", hi])
        
    cmd.append(entry_point)
    
    print(f"Building Kate Assistant executable...")
    print("Command:", " ".join(cmd))
    res = subprocess.run(cmd, cwd=repo_root)
    if res.returncode == 0:
        dist_path = os.path.join(repo_root, "dist", "KateAssistant.exe")
        print(f"\nBuild SUCCESS! Executable created at: {dist_path}\n")
    else:
        print(f"\nBuild FAILED with return code {res.returncode}")

if __name__ == "__main__":
    build()
