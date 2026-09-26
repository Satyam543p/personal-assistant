import os
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

# Network configuration for local IPC
HOST = "127.0.0.1"
PORT = 7474

# Default idle timeout (in seconds) for lazy-loaded resources
IDLE_UNLOAD_TIMEOUT = 60

# Workspace paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.path.join(BASE_DIR, "jarvis.db")
LOG_PATH = os.path.join(BASE_DIR, "daemon.log")

# Model configurations
LOCAL_MODEL_PATH = os.path.join(BASE_DIR, "models", "qwen2.5-0.5b-instruct.gguf")

# Threshold settings for routing (stored in config, user can tune)
THRESHOLD_LOW = 0.55
THRESHOLD_HIGH = 0.82
CLOUD_THRESHOLD = 0.65
MAX_CLARIFY_ROUNDS = 2

# Interpreter configurations
INTERPRETER_TYPE = "auto"  # "auto" | "llamacpp" | "ollama" | "rule_based"
LLAMACPP_URL = "http://localhost:8080"
OLLAMA_URL = "http://localhost:11434"
INTERPRETER_MODEL = "qwen2.5:0.5b"  # Model tag for Ollama/llama.cpp

# Embedding and Semantic Memory configurations
EMBEDDING_PROVIDER = os.environ.get("JARVIS_EMBEDDING_PROVIDER", "auto")  # "auto" | "sentence_transformers" | "ollama" | "llamacpp" | "fallback"
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
EMBEDDING_IDLE_TIMEOUT = 60
MEMORY_TOP_K = 8

# Cloud Escalation configurations
CLOUD_PROVIDER = os.environ.get("JARVIS_CLOUD_PROVIDER", "cascading")  # "cascading" | "ollama_cloud" | "mock" | "grok" | "openai"
CLOUD_API_KEY = os.environ.get("JARVIS_CLOUD_API_KEY", os.environ.get("XAI_API_KEY", ""))
CLOUD_ENDPOINT = os.environ.get("JARVIS_CLOUD_ENDPOINT", "https://api.x.ai/v1/chat/completions")
CLOUD_MODEL = os.environ.get("JARVIS_CLOUD_MODEL", "grok-beta")
CLOUD_MONTHLY_CAP = int(os.environ.get("JARVIS_CLOUD_MONTHLY_CAP", "500000"))
CLOUD_CACHE_TTL = int(os.environ.get("JARVIS_CLOUD_CACHE_TTL", "86400"))  # 24 hours in seconds
CLOUD_SOFT_TOKEN_LIMIT = 8000
CLOUD_HARD_TOKEN_LIMIT = 16000
CLOUD_CONNECTIVITY_TIMEOUT = 1.0  # seconds for TCP connectivity check

# Cascading Brain & Model Fallback configurations
OLLAMA_CLOUD_MODEL = os.environ.get("JARVIS_OLLAMA_CLOUD_MODEL", "gemma4:31b-cloud")
OLLAMA_LOCAL_MODEL = os.environ.get("JARVIS_OLLAMA_LOCAL_MODEL", "qwen2.5:0.5b")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# UI & Siri Invocation configurations
SIRI_HOTKEY = os.environ.get("JARVIS_SIRI_HOTKEY", "ctrl+space")

# Memory Importance & Conflict Resolution configurations
MEMORY_HALF_LIFE_DAYS = int(os.environ.get("JARVIS_MEMORY_HALF_LIFE_DAYS", "90"))
MEMORY_SIMILARITY_CONFLICT_THRESHOLD = float(os.environ.get("JARVIS_MEMORY_CONFLICT_THRESHOLD", "0.58"))
MEMORY_PRUNE_THRESHOLD = float(os.environ.get("JARVIS_MEMORY_PRUNE_THRESHOLD", "0.15"))
MEMORY_PRUNE_MIN_AGE_DAYS = int(os.environ.get("JARVIS_MEMORY_PRUNE_MIN_AGE_DAYS", "180"))

# YouTube Learning Pipeline configurations (Section 41)
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
YOUTUBE_DEFAULT_MAX_RESULTS = int(os.environ.get("JARVIS_YOUTUBE_MAX_RESULTS", "5"))
YOUTUBE_IDLE_CLOSE_MINUTES = int(os.environ.get("JARVIS_YOUTUBE_IDLE_MINUTES", "45"))

# Autonomous Web Research Workflow configurations (Section 42)
WORKSPACE_ROOT = os.path.dirname(BASE_DIR)
RESEARCH_OUTPUT_DIR = os.path.join(WORKSPACE_ROOT, "research")
RESEARCH_DEFAULT_DEPTH = os.environ.get("JARVIS_RESEARCH_DEFAULT_DEPTH", "quick")
RESEARCH_MAX_SOURCES_QUICK = int(os.environ.get("JARVIS_RESEARCH_MAX_SOURCES_QUICK", "2"))
RESEARCH_MAX_SOURCES_DEEP = int(os.environ.get("JARVIS_RESEARCH_MAX_SOURCES_DEEP", "5"))
RESEARCH_TRUSTED_DOMAINS = [
    "docs.python.org",
    "wikipedia.org",
    "github.com",
    "developer.mozilla.org",
    "arxiv.org",
    "stackoverflow.com",
    "geeksforgeeks.org",
    "realpython.com",
    "w3schools.com",
    "rust-lang.org",
    "golang.org",
    "microsoft.com",
    "gnu.org",
    "kernel.org",
]
RESEARCH_BLOCKED_DOMAINS = [
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "tiktok.com",
    "pinterest.com",
    "reddit.com",  # often login-walled/blocked
]

