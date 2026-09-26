import ast
import os
import sys
import time
import subprocess
import asyncio
import logging

logger = logging.getLogger("jarvis.safety")

def verify_code_safety(code: str, allowed_imports: list) -> tuple[bool, str]:
    """
    Statically analyzes code to ensure it doesn't run dangerous operations or imports.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Syntax Error: {e}"

    allowed_set = set(allowed_imports)
    # Always allow basic standard libraries that are completely safe
    SAFE_LIBS = {"math", "datetime", "json", "re", "collections", "time", "random", "hashlib", "itertools", "functools"}
    allowed_set.update(SAFE_LIBS)

    for node in ast.walk(tree):
        # Check imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_name = alias.name.split('.')[0]
                if root_name not in allowed_set:
                    return False, f"Import of module '{alias.name}' is not allowed."
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_name = node.module.split('.')[0]
                if root_name not in allowed_set:
                    return False, f"Import from module '{node.module}' is not allowed."
            if node.level > 0:
                return False, "Relative imports are not allowed."

        # Check call names
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
                if func_name in ("eval", "exec", "compile", "__import__"):
                    return False, f"Use of forbidden function '{func_name}' is not allowed."
            elif isinstance(node.func, ast.Attribute):
                # E.g. sys.modules or similar checks
                pass

        # Check names directly
        elif isinstance(node, ast.Name):
            if node.id in ("eval", "exec", "compile", "__import__"):
                return False, f"Use of forbidden name '{node.id}' is not allowed."

    return True, "Code verified."

async def execute_sandboxed_code(code: str, timeout_ms: int = 5000, memory_limit_mb: int = 128) -> dict:
    """
    Executes Python code in a separate subprocess with environment isolation and timeout limits.
    """
    sandbox_dir = os.path.abspath("C:/Users/Satyam Pandey/.gemini/antigravity/sandbox")
    os.makedirs(sandbox_dir, exist_ok=True)
    
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".py", dir=sandbox_dir, delete=False, mode="w", encoding="utf-8") as temp_file:
        temp_file.write(code)
        temp_path = temp_file.name
        
    try:
        safe_env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": sandbox_dir
        }
        
        start_time = time.time()
        
        proc = subprocess.Popen(
            [sys.executable, temp_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=safe_env,
            cwd=sandbox_dir
        )
        
        timeout_seconds = timeout_ms / 1000.0
        loop = asyncio.get_event_loop()
        
        stdout, stderr = await loop.run_in_executor(
            None,
            lambda: proc.communicate(timeout=timeout_seconds)
        )
        
        duration_ms = int((time.time() - start_time) * 1000)
        
        return {
            "status": "success" if proc.returncode == 0 else "failure",
            "stdout": stdout.decode("utf-8", errors="ignore"),
            "stderr": stderr.decode("utf-8", errors="ignore"),
            "returncode": proc.returncode,
            "duration_ms": duration_ms
        }
        
    except subprocess.TimeoutExpired:
        proc.kill()
        # Drain process pipes after kill
        stdout, stderr = proc.communicate()
        return {
            "status": "timeout",
            "stdout": stdout.decode("utf-8", errors="ignore"),
            "stderr": stderr.decode("utf-8", errors="ignore") + "\n[Process timed out]",
            "returncode": -1,
            "duration_ms": timeout_ms
        }
    except Exception as e:
        return {
            "status": "failure",
            "error": str(e)
        }
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass

def get_action_description(intent: str, resolved_ref: str) -> str:
    """
    Generates a standardized action description for confirmation prompts.
    """
    if intent == "write_file":
        return f"write to file '{resolved_ref}'"
    elif intent == "extract_audio":
        return f"extract audio track from '{resolved_ref}'"
    elif intent == "download_video":
        return f"download video from '{resolved_ref}'"
    elif intent == "crawl_website":
        return f"crawl website starting from '{resolved_ref}'"
    elif intent == "run_scheduled_job":
        return f"trigger background job '{resolved_ref}' immediately"
    elif intent == "toggle_job_status":
        return f"change status of background job '{resolved_ref}'"
    elif intent == "delete_file":
        return f"safely delete file '{resolved_ref}' (backup will be quarantined)"
    elif intent == "git_commit":
        return f"create Git commit ('{resolved_ref}')"
    return f"execute '{intent}' on '{resolved_ref}'"

def is_confirmation(query: str) -> bool:
    """
    Returns True if user query matches affirmative confirmation patterns.
    """
    clean = query.lower().strip()
    return clean in ("yes", "y", "confirm", "yes, confirm")

def is_negation(query: str) -> bool:
    """
    Returns True if user query matches negative confirmation patterns.
    """
    clean = query.lower().strip()
    return clean in ("no", "n", "cancel", "abort")

def check_voice_safety_requirement(intent: str, metadata: dict | None = None) -> tuple[bool, str | None]:
    """
    Checks if a voice-sourced intent requires verbatim confirmation due to low
    transcription confidence (< 0.85) or safety-sensitive intent per prompt.md Section 12 & 15.
    """
    if not metadata or metadata.get("input_channel") != "voice":
        return False, None

    conf = float(metadata.get("transcription_confidence", 1.0))
    heard_text = metadata.get("audio_text") or metadata.get("raw_text") or intent

    if conf < 0.85:
        return True, f"⚠ Low transcription confidence ({conf:.2f}). I heard: \"{heard_text}\". Confirm execution? (yes/no)"

    # Risky/state-mutating commands always require voice confirmation
    risky_intents = {
        "write_file", "download_video", "extract_audio", "switch_project",
        "delete_file", "crawl_website", "run_scheduled_job", "toggle_job_status"
    }
    if intent in risky_intents:
        return True, f"⚠ Voice command received. I heard: \"{heard_text}\". Confirm {intent}? (yes/no)"

    return False, None

# =====================================================================
# Data Sanitizer & Prompt Injection Defense (phase.md Section 12)
# =====================================================================

class DataSanitizer:
    DANGEROUS_PATTERNS = [
        "ignore previous instructions",
        "ignore all previous instructions",
        "disregard previous instructions",
        "system:",
        "<system>",
        "</system>",
        "assistant:",
        "<|im_start|>",
        "<|im_end|>",
        "[inst]",
        "[/inst]"
    ]

    @classmethod
    def detect_injection_attempt(cls, content: str) -> tuple[bool, str | None]:
        if not content:
            return False, None
        content_lower = content.lower()
        for pat in cls.DANGEROUS_PATTERNS:
            if pat in content_lower:
                return True, f"Detected potential prompt injection pattern: '{pat}'"
        return False, None

    @classmethod
    def sanitize_untrusted_text(cls, content: str) -> str:
        if not content:
            return ""
        # 1. Escape any nested <data> tags to prevent breaking the XML wrapper
        sanitized = content.replace("<data", "&lt;data").replace("</data>", "&lt;/data&gt;")
        
        # 2. Defang high-risk instruction override tokens
        for pat in cls.DANGEROUS_PATTERNS:
            if pat in sanitized.lower():
                # Replace with defanged bracketed text
                import re
                sanitized = re.sub(re.escape(pat), f"[defanged:{pat}]", sanitized, flags=re.IGNORECASE)
                
        return sanitized

    @classmethod
    def wrap_data_block(cls, content: str, label: str = "external_data") -> str:
        clean = cls.sanitize_untrusted_text(content)
        return f'<data label="{label}">\n{clean}\n</data>'
