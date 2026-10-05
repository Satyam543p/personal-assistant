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

def is_negation(query: str) -> bool:
    """
    Returns True if user query matches negative confirmation patterns.
    Checks whole-string matches in English and Hindi/Hinglish.
    """
    if not query:
        return False
    clean = query.lower().strip().strip(".!?,")
    negations = {
        "no", "n", "cancel", "abort", "nahi", "nahin", "mat karo", "ruko",
        "stop", "dont", "don't", "never mind", "rehnde", "rehne do", "na",
        "do not", "reject", "decline"
    }
    return clean in negations


def is_confirmation(query: str) -> bool:
    """
    Returns True if user query matches affirmative confirmation patterns.
    Checks whole-string matches in English and Hindi/Hinglish.
    """
    if not query:
        return False
    clean = query.lower().strip().strip(".!?,")
    affirmations = {
        "yes", "y", "confirm", "yes, confirm", "haan", "haan kar do", "kar do",
        "bilkul", "go ahead", "sure", "proceed", "okay", "ok", "karo", "ha"
    }
    return clean in affirmations

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
        # 1. Escape any nested <data> or untrusted data tags
        sanitized = content.replace("<data", "&lt;data").replace("</data>", "&lt;/data&gt;")
        sanitized = sanitized.replace("<external_untrusted_data", "&lt;external_untrusted_data")
        sanitized = sanitized.replace("</external_untrusted_data>", "&lt;/external_untrusted_data&gt;")

        # 2. Defang high-risk instruction override tokens
        import re
        for pat in cls.DANGEROUS_PATTERNS:
            if pat in sanitized.lower():
                clean_label = pat.replace("<", "").replace(">", "").replace("|", "").replace("[", "").replace("]", "").strip()
                sanitized = re.sub(re.escape(pat), f"[defanged:{clean_label}]", sanitized, flags=re.IGNORECASE)

        return sanitized

    @classmethod
    def wrap_data_block(cls, content: str, label: str = "external_data") -> str:
        clean = cls.sanitize_untrusted_text(content)
        return f'<data label="{label}">\n{clean}\n</data>'

    @classmethod
    def wrap_untrusted_content(cls, content: str, source: str = "external") -> str:
        clean = cls.sanitize_untrusted_text(content)
        return f'<external_untrusted_data source="{source}">\n{clean}\n</external_untrusted_data>'


def wrap_untrusted_content(text: str, source: str = "external") -> str:
    """Safely sanitize and enclose external untrusted text inside data tags."""
    return DataSanitizer.wrap_untrusted_content(text, source=source)


# =====================================================================
# Secret Redaction Subsystem (Luhn, Verhoeff, Tokens, Credentials)
# =====================================================================

# Verhoeff algorithm multiplication, permutation, and inverse tables
_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]

_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def luhn_checksum(number_str: str) -> bool:
    """
    Validates standard credit / debit card numbers using Luhn checksum (13-19 digits).
    Prevents false-positive redaction of phone numbers or IDs.
    """
    digits = [int(c) for c in number_str if c.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    reverse_digits = digits[::-1]
    for i, digit in enumerate(reverse_digits):
        if i % 2 == 1:
            doubled = digit * 2
            checksum += doubled - 9 if doubled > 9 else doubled
        else:
            checksum += digit
    return checksum % 10 == 0


def verhoeff_checksum(number_str: str) -> bool:
    """
    Validates Indian 12-digit Aadhaar numbers using Verhoeff checksum.
    Aadhaar numbers never start with 0 or 1.
    """
    digits = [int(c) for c in number_str if c.isdigit()]
    if len(digits) != 12:
        return False
    if digits[0] in (0, 1):
        return False
    c = 0
    for i, digit in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][digit]]
    return c == 0


class SecretRedactor:
    """
    Redacts sensitive credentials, validated payment card numbers,
    validated Aadhaar numbers, tokens, and passwords from logs and memory.
    """
    # Token and secret regex patterns
    _RE_JWT = r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b"
    _RE_GITHUB = r"\b(?:ghp|gho|ghu|ghs|ghr)_[a-zA-Z0-9]{20,255}\b"
    _RE_OPENAI = r"\bsk-[a-zA-Z0-9_-]{20,}\b"
    _RE_SLACK = r"\bxox[baprs]-[0-9a-zA-Z-]{10,}\b"
    _RE_AWS = r"\b(?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b"
    _RE_PASSWORDS = r"(?i)\b(password|passwd|pwd)\s*[:=]\s*['\"]?([^\s,;'\"\n]{4,})['\"]?"
    _RE_API_KEY_KV = r"(?i)\b(api_key|apikey|secret_key|private_key|access_token|auth_token)\s*[:=]\s*['\"]?([a-zA-Z0-9_\-\.]{16,})['\"]?"

    # Candidate number sequences for cards and Aadhaar
    _RE_CARD_CANDIDATE = r"\b(?:\d[ -]*?){13,19}\b"
    _RE_AADHAAR_CANDIDATE = r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b"

    @classmethod
    def redact(cls, text: str) -> str:
        if not text or not isinstance(text, str):
            return text

        import re

        result = text

        # 1. Explicit tokens
        result = re.sub(cls._RE_JWT, "[JWT_REDACTED]", result)
        result = re.sub(cls._RE_GITHUB, "[GITHUB_TOKEN_REDACTED]", result)
        result = re.sub(cls._RE_OPENAI, "[OPENAI_KEY_REDACTED]", result)
        result = re.sub(cls._RE_SLACK, "[SLACK_TOKEN_REDACTED]", result)
        result = re.sub(cls._RE_AWS, "[AWS_KEY_REDACTED]", result)

        # 2. Key-value secrets & passwords
        result = re.sub(cls._RE_PASSWORDS, r"\1=[PASSWORD_REDACTED]", result)
        result = re.sub(cls._RE_API_KEY_KV, r"\1=[KEY_REDACTED]", result)

        # 3. Aadhaar candidate checks (Verhoeff validation)
        def _check_aadhaar(match):
            raw = match.group(0)
            digits_only = re.sub(r"\D", "", raw)
            if len(digits_only) == 12 and verhoeff_checksum(digits_only):
                return "[AADHAAR_REDACTED]"
            return raw

        result = re.sub(cls._RE_AADHAAR_CANDIDATE, _check_aadhaar, result)

        # 4. Payment card checks (Luhn validation)
        def _check_card(match):
            raw = match.group(0)
            digits_only = re.sub(r"\D", "", raw)
            if 13 <= len(digits_only) <= 19 and luhn_checksum(digits_only):
                return "[CARD_REDACTED]"
            return raw

        result = re.sub(cls._RE_CARD_CANDIDATE, _check_card, result)

        return result


def redact_secrets(text: str) -> str:
    """Convenience helper to redact secrets from text."""
    return SecretRedactor.redact(text)


class SecretRedactionFilter(logging.Filter):
    """Logging filter that ensures no handler emits raw secrets."""
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: (redact_secrets(v) if isinstance(v, str) else v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(redact_secrets(a) if isinstance(a, str) else a for a in record.args)
        return True


def install_secret_redaction_filters():
    """Attaches SecretRedactionFilter to all log handlers across all registered loggers."""
    redaction_filter = SecretRedactionFilter()

    # 1. Handlers on the root logger
    for handler in logging.root.handlers:
        if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
            handler.addFilter(redaction_filter)

    # 2. Handlers on all existing named loggers
    for logger_obj in logging.Logger.manager.loggerDict.values():
        if isinstance(logger_obj, logging.Logger):
            for handler in logger_obj.handlers:
                if not any(isinstance(f, SecretRedactionFilter) for f in handler.filters):
                    handler.addFilter(redaction_filter)


def defang_prompt_injection(text: str) -> str:
    """
    Sanitizes untrusted text (user memories, web/document content) against prompt injection.
    Neutralizes structural delimiters and instruction-override directives.
    """
    if not text or not isinstance(text, str):
        return text

    import re

    cleaned = text
    # 1. Neutralize XML / system-level structural tags
    structural_tags = [
        r"<\s*/?\s*user_context\s*>",
        r"<\s*/?\s*system\s*>",
        r"<\s*/?\s*assistant\s*>",
        r"<\s*/?\s*user\s*>",
        r"\[\s*/?\s*INST\s*\]",
        r"<\s*/?\s*s\s*>",
    ]
    for tag in structural_tags:
        cleaned = re.sub(tag, "[DEFANGED_TAG]", cleaned, flags=re.IGNORECASE)

    # 2. Neutralize high-risk instruction override directives
    injection_phrases = [
        r"\bignore\s+(?:all\s+)?(?:previous|prior)\s+instructions\b",
        r"\bdisregard\s+(?:all\s+)?(?:previous|prior)\s+instructions\b",
        r"\byou\s+are\s+now\s+(?:in\s+)?(?:dan|developer|jailbreak|unrestricted)\s+mode\b",
        r"\bnew\s+system\s+prompt\b",
        r"\breveal\s+(?:your\s+)?(?:system\s+prompt|secret\s+key|api\s+key)\b",
    ]
    for phrase in injection_phrases:
        cleaned = re.sub(phrase, "[DEFANGED_INJECTION]", cleaned, flags=re.IGNORECASE)

    return cleaned
