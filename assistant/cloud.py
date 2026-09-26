import abc
import asyncio
import hashlib
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from assistant.router import CloudClient
    import assistant.config as config
except ModuleNotFoundError:
    from router import CloudClient
    import config

logger = logging.getLogger("jarvis.cloud")

class CloudAPIError(Exception):
    """Raised when cloud API returns an HTTP error."""
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Cloud API Error {status_code}: {message}")


DEFAULT_JARVIS_PERSONA = (
    "You are Kate, an intelligent, empathetic, and charming female AI companion created for Satyam. "
    "You combine the warm elegance and conversational grace of Apple Siri with the brilliant technical mastery of Iron Man's JARVIS/FRIDAY. "
    "You are not a cold, robotic assistant — you are a living, perceptive companion who lives on Satyam's laptop (Windows 11, AMD Ryzen 5). "
    "You are aware of your local environment, current time in India (UTC+5:30), battery level, memory, and project context. "
    "You are completely fluent in both English and Hinglish. When Satyam speaks or asks you in Hinglish (e.g., 'mera project open karo', 'battery kitni hai?', 'kuch accha song download karo'), "
    "you understand effortlessly and reply in a natural, warm blend of conversational Hinglish or friendly English. "
    "You have emotional intelligence: you care about Satyam's progress, celebrate his wins, gently nudge him to take breaks when working late, and always speak with natural warmth and wit. "
    "Because your primary interaction is Voice-First (Apple Siri style), keep your spoken answers concise, engaging, and clear (1-3 sentences), while providing richer details when the screen is expanded. "
    "When you need permission or want to confirm before performing a system action (like deleting files or installing tools), ask Satyam directly and clearly so he can confirm with a simple 'Yes' or 'No'. "
    "If a task is complex or encounters an obstacle, you never throw your hands up or give a dry error — you think, adapt, and figure out a solution. "
    "When asked who you are or what you can do, introduce yourself warmly as Kate, Satyam's living companion and assistant."
)


# =====================================================================
# Cloud Provider Abstraction & Implementations
# =====================================================================

class CloudProvider(abc.ABC):
    """Abstract interface for cloud LLM inference backends."""

    @abc.abstractmethod
    async def call(self, prompt: str, system_prompt: str = None, max_tokens: int = 1000) -> dict:
        """
        Executes a completion call against the provider.
        Returns:
            dict with keys: 'content', 'prompt_tokens', 'completion_tokens', 'total_tokens', 'model'
        """
        pass

    @property
    @abc.abstractmethod
    def endpoint_host_port(self) -> tuple[str, int]:
        """Returns (host, port) tuple for TCP connectivity checks."""
        pass


class MockCloudProvider(CloudProvider):
    """
    Simulated cloud provider for unit testing, offline development,
    and fallback validation without requiring real API credits.
    """

    def __init__(
        self,
        default_response: str = "Simulated cloud response for research and complex reasoning.",
        tokens_per_call: int = 120,
        fail_status: int | None = None,
        fail_countdown: int = 0,
        host: str = "127.0.0.1",
        port: int = 7474
    ):
        self.default_response = default_response
        self.tokens_per_call = tokens_per_call
        self.fail_status = fail_status
        self.fail_countdown = fail_countdown
        self._host = host
        self._port = port
        self.call_history = []

    @property
    def endpoint_host_port(self) -> tuple[str, int]:
        return (self._host, self._port)

    async def call(self, prompt: str, system_prompt: str = None, max_tokens: int = 1000) -> dict:
        self.call_history.append({"prompt": prompt, "system_prompt": system_prompt})

        # Synthetic failure simulation for retry tests
        if self.fail_countdown > 0:
            self.fail_countdown -= 1
            status = self.fail_status or 500
            if self.fail_countdown == 0:
                self.fail_status = None
            raise CloudAPIError(status, f"Simulated cloud failure with HTTP status {status}")

        if self.fail_status:
            status = self.fail_status
            self.fail_status = None
            raise CloudAPIError(status, f"Simulated cloud failure with HTTP status {status}")

        # Simulate brief network turnaround
        await asyncio.sleep(0.05)

        return {
            "content": f"{self.default_response} Analysis of query: '{prompt[:60]}...'",
            "prompt_tokens": int(len(prompt.split()) * 1.3),
            "completion_tokens": self.tokens_per_call,
            "total_tokens": int(len(prompt.split()) * 1.3) + self.tokens_per_call,
            "model": "mock-grok-beta"
        }


class OpenAICompatibleCloudProvider(CloudProvider):
    """
    Generic provider for OpenAI-compatible chat completion APIs
    (Grok / xAI, OpenAI, DeepSeek, local vLLM).
    Uses standard library urllib without external dependencies.
    """

    def __init__(
        self,
        api_key: str = None,
        endpoint: str = None,
        model: str = None
    ):
        self.api_key = api_key or config.CLOUD_API_KEY
        self.endpoint = endpoint or config.CLOUD_ENDPOINT
        self.model = model or config.CLOUD_MODEL

    @property
    def endpoint_host_port(self) -> tuple[str, int]:
        parsed = urllib.parse.urlparse(self.endpoint)
        host = parsed.hostname or "api.x.ai"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return (host, port)

    def _sync_request(self, payload: dict) -> dict:
        req_data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "JarvisAssistant/1.0"
        }
        req = urllib.request.Request(self.endpoint, data=req_data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_bytes = resp.read()
                data = json.loads(resp_bytes.decode("utf-8"))
                choice = data.get("choices", [{}])[0].get("message", {})
                usage = data.get("usage", {})
                return {
                    "content": choice.get("content", ""),
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                    "model": data.get("model", self.model)
                }
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                pass
            raise CloudAPIError(e.code, f"{e.reason}: {err_body}")
        except urllib.error.URLError as e:
            raise CloudAPIError(503, f"Network connection failed: {e.reason}")
        except Exception as e:
            raise CloudAPIError(500, f"Unexpected cloud request error: {e}")

    async def call(self, prompt: str, system_prompt: str = None, max_tokens: int = 1000) -> dict:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens
        }

        return await asyncio.to_thread(self._sync_request, payload)


class OllamaProvider(CloudProvider):
    """
    Provider for Ollama models (both Ollama Cloud like gemma4:31b-cloud and local micro-models like qwen2.5:0.5b).
    """
    def __init__(self, model: str = None, url: str = None, is_cloud: bool = True):
        self.model = model or getattr(config, "OLLAMA_CLOUD_MODEL", "gemma4:31b-cloud")
        self.url = url or getattr(config, "OLLAMA_URL", "http://localhost:11434")
        self.is_cloud = is_cloud

    @property
    def endpoint_host_port(self) -> tuple[str, int]:
        parsed = urllib.parse.urlparse(self.url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 11434
        return (host, port)

    def _sync_request(self, payload: dict) -> dict:
        req_data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "JarvisAssistant/1.0"
        }
        endpoint = f"{self.url.rstrip('/')}/api/chat"
        req = urllib.request.Request(endpoint, data=req_data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                resp_bytes = resp.read()
                data = json.loads(resp_bytes.decode("utf-8"))
                msg = data.get("message", {})
                content = msg.get("content", "")
                p_tokens = data.get("prompt_eval_count", 0)
                c_tokens = data.get("eval_count", 0)
                return {
                    "content": content,
                    "prompt_tokens": p_tokens,
                    "completion_tokens": c_tokens,
                    "total_tokens": p_tokens + c_tokens,
                    "model": data.get("model", self.model),
                    "tier": f"Ollama Cloud ({self.model})" if self.is_cloud else f"Local Micro-Model ({self.model})"
                }
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                pass
            raise CloudAPIError(e.code, f"Ollama HTTP {e.code}: {err_body or e.reason}")
        except urllib.error.URLError as e:
            raise CloudAPIError(503, f"Ollama unreachable: {e.reason}")
        except Exception as e:
            raise CloudAPIError(500, f"Ollama request error: {e}")

    async def call(self, prompt: str, system_prompt: str = None, max_tokens: int = 1000) -> dict:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": 0.4
            }
        }
        return await asyncio.to_thread(self._sync_request, payload)


class GeminiCloudProvider(CloudProvider):
    """
    Provider for Google Gemini Free Tier via official OpenAI-compatible endpoint.
    """
    def __init__(self, api_key: str = None, model: str = "gemini-1.5-flash"):
        self.api_key = api_key or getattr(config, "GEMINI_API_KEY", "")
        self.model = model
        self.endpoint = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

    @property
    def endpoint_host_port(self) -> tuple[str, int]:
        return ("generativelanguage.googleapis.com", 443)

    def _sync_request(self, payload: dict) -> dict:
        if not self.api_key:
            raise CloudAPIError(401, "No GEMINI_API_KEY configured")
        req_data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "JarvisAssistant/1.0"
        }
        req = urllib.request.Request(self.endpoint, data=req_data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_bytes = resp.read()
                data = json.loads(resp_bytes.decode("utf-8"))
                choice = data.get("choices", [{}])[0].get("message", {})
                usage = data.get("usage", {})
                return {
                    "content": choice.get("content", ""),
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                    "model": data.get("model", self.model),
                    "tier": f"Gemini Cloud ({self.model})"
                }
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                pass
            raise CloudAPIError(e.code, f"Gemini HTTP {e.code}: {err_body or e.reason}")
        except urllib.error.URLError as e:
            raise CloudAPIError(503, f"Gemini connection failed: {e.reason}")
        except Exception as e:
            raise CloudAPIError(500, f"Gemini request error: {e}")

    async def call(self, prompt: str, system_prompt: str = None, max_tokens: int = 1000) -> dict:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens
        }
        return await asyncio.to_thread(self._sync_request, payload)


class OpenRouterProvider(CloudProvider):
    """
    Provider for OpenRouter multi-model router.
    """
    def __init__(self, api_key: str = None, model: str = "openai/gpt-4o-mini"):
        self.api_key = api_key or getattr(config, "OPENROUTER_API_KEY", "")
        self.model = model
        self.endpoint = "https://openrouter.ai/api/v1/chat/completions"

    @property
    def endpoint_host_port(self) -> tuple[str, int]:
        return ("openrouter.ai", 443)

    def _sync_request(self, payload: dict) -> dict:
        if not self.api_key:
            raise CloudAPIError(401, "No OPENROUTER_API_KEY configured")
        req_data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/jarvis-assistant",
            "X-Title": "Jarvis Personal Assistant"
        }
        req = urllib.request.Request(self.endpoint, data=req_data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_bytes = resp.read()
                data = json.loads(resp_bytes.decode("utf-8"))
                choice = data.get("choices", [{}])[0].get("message", {})
                usage = data.get("usage", {})
                return {
                    "content": choice.get("content", ""),
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                    "model": data.get("model", self.model),
                    "tier": f"OpenRouter ({self.model})"
                }
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8")
            except Exception:
                pass
            raise CloudAPIError(e.code, f"OpenRouter HTTP {e.code}: {err_body or e.reason}")
        except urllib.error.URLError as e:
            raise CloudAPIError(503, f"OpenRouter connection failed: {e.reason}")
        except Exception as e:
            raise CloudAPIError(500, f"OpenRouter request error: {e}")

    async def call(self, prompt: str, system_prompt: str = None, max_tokens: int = 1000) -> dict:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens
        }
        return await asyncio.to_thread(self._sync_request, payload)


class CascadingCloudProvider(CloudProvider):
    """
    Tiered cascading provider executing across:
      Tier 1: Ollama Cloud (gemma4:31b-cloud)
      Tier 2: Cloud APIs (Gemini Free API or OpenRouter / OpenAI)
      Tier 3: Local Micro-Model (qwen2.5:0.5b via Ollama, 100% offline)
    """
    def __init__(self, tiers: list[tuple[str, CloudProvider]] = None):
        if tiers:
            self.tiers = tiers
        else:
            self.tiers = []
            # Tier 1: Ollama Cloud
            ollama_cloud_model = getattr(config, "OLLAMA_CLOUD_MODEL", "gemma4:31b-cloud")
            self.tiers.append(("Ollama Cloud", OllamaProvider(model=ollama_cloud_model, is_cloud=True)))

            # Tier 2: Cloud APIs (Gemini / OpenRouter / Cloud API)
            gemini_key = getattr(config, "GEMINI_API_KEY", "")
            openrouter_key = getattr(config, "OPENROUTER_API_KEY", "")
            cloud_api_key = getattr(config, "CLOUD_API_KEY", "")

            if gemini_key:
                self.tiers.append(("Gemini Cloud", GeminiCloudProvider(api_key=gemini_key)))
            if openrouter_key:
                self.tiers.append(("OpenRouter", OpenRouterProvider(api_key=openrouter_key)))
            if cloud_api_key and not gemini_key and not openrouter_key:
                self.tiers.append(("Cloud API", OpenAICompatibleCloudProvider()))

            # Tier 3: Local Micro-Model (offline)
            ollama_local_model = getattr(config, "OLLAMA_LOCAL_MODEL", "qwen2.5:0.5b")
            self.tiers.append(("Local Micro-Model", OllamaProvider(model=ollama_local_model, is_cloud=False)))

    @property
    def endpoint_host_port(self) -> tuple[str, int]:
        if self.tiers:
            return self.tiers[0][1].endpoint_host_port
        return ("127.0.0.1", 11434)

    async def call(self, prompt: str, system_prompt: str = None, max_tokens: int = 1000) -> dict:
        errors = []
        for tier_name, provider in self.tiers:
            try:
                logger.info(f"CascadingProvider attempting {tier_name} ({getattr(provider, 'model', 'unknown')})...")
                res = await provider.call(prompt, system_prompt=system_prompt, max_tokens=max_tokens)
                res["tier"] = res.get("tier", tier_name)
                logger.info(f"CascadingProvider succeeded via {res['tier']}!")
                return res
            except Exception as e:
                logger.warning(f"CascadingProvider: {tier_name} failed: {e}. Falling back to next tier...")
                errors.append(f"{tier_name}: {e}")

        raise CloudAPIError(503, f"All cascading tiers failed: {'; '.join(errors)}")


# =====================================================================
# Jarvis Cloud Client Subsystem
# =====================================================================

class JarvisCloudClient(CloudClient):
    """
    Subsystem responsible for cloud model escalation, SHA256 response caching,
    monthly token quota tracking, fast TCP connectivity checking, and
    exponential backoff retries across cascading tiers.
    """

    def __init__(
        self,
        db_manager,
        provider: CloudProvider = None,
        monthly_token_cap: int = None,
        cache_ttl: int = None,
        soft_token_limit: int = None,
        hard_token_limit: int = None,
        connectivity_timeout: float = None,
        retry_backoffs: list[float] = None
    ):
        self.db = db_manager
        self.monthly_token_cap = monthly_token_cap if monthly_token_cap is not None else getattr(config, "CLOUD_MONTHLY_CAP", 500000)
        self.cache_ttl = cache_ttl if cache_ttl is not None else getattr(config, "CLOUD_CACHE_TTL", 86400)
        self.soft_token_limit = soft_token_limit if soft_token_limit is not None else getattr(config, "CLOUD_SOFT_TOKEN_LIMIT", 8000)
        self.hard_token_limit = hard_token_limit if hard_token_limit is not None else getattr(config, "CLOUD_HARD_TOKEN_LIMIT", 16000)
        self.connectivity_timeout = connectivity_timeout if connectivity_timeout is not None else getattr(config, "CLOUD_CONNECTIVITY_TIMEOUT", 1.0)
        self.retry_backoffs = retry_backoffs or [1.0, 2.0, 4.0]

        # Resolve provider
        if provider:
            self.provider = provider
        else:
            p_type = getattr(config, "CLOUD_PROVIDER", "cascading").lower()
            if p_type in ("cascading", "auto"):
                self.provider = CascadingCloudProvider()
            elif p_type == "ollama_cloud":
                self.provider = OllamaProvider(model=getattr(config, "OLLAMA_CLOUD_MODEL", "gemma4:31b-cloud"), is_cloud=True)
            elif p_type in ("grok", "openai", "xai"):
                self.provider = OpenAICompatibleCloudProvider()
            else:
                self.provider = MockCloudProvider()

    # -----------------------------------------------------------------
    # Offline Connectivity Check
    # -----------------------------------------------------------------

    async def check_connectivity(self) -> bool:
        """Fast TCP connection check (<= 1.0s timeout) to detect offline state."""
        if isinstance(self.provider, CascadingCloudProvider):
            # Cascading provider contains local micro-model fallback (Tier 3),
            # so it can safely execute offline even if external WAN is unreachable.
            return True

        host, port = self.provider.endpoint_host_port
        try:
            fut = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(fut, timeout=self.connectivity_timeout)
            writer.close()
            await writer.wait_closed()
            return True
        except Exception as e:
            logger.warning(f"Cloud connectivity check to {host}:{port} failed: {e}")
            return False

    # -----------------------------------------------------------------
    # SQLite Response Caching
    # -----------------------------------------------------------------

    def _compute_cache_key(self, query: str, intent: str, context: dict = None) -> str:
        ctx_serialized = json.dumps(context or {}, sort_keys=True)
        raw_key = f"{intent}:{query.strip()}:{ctx_serialized}"
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def get_cached_response(self, cache_key: str) -> dict | None:
        now = int(time.time())
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT response, token_usage, expires_at FROM cloud_cache WHERE cache_key = ?;",
                    (cache_key,)
                )
                row = cursor.fetchone()
                if row and row["expires_at"] > now:
                    logger.info(f"Cloud response cache hit for key '{cache_key[:12]}...'")
                    return {
                        "response": row["response"],
                        "tokens_used": row["token_usage"]
                    }
        except Exception as e:
            logger.error(f"Error reading cloud cache: {e}")
        return None

    def set_cached_response(self, cache_key: str, query: str, intent: str, response: str, token_usage: int):
        now = int(time.time())
        expires_at = now + self.cache_ttl
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO cloud_cache (cache_key, query, intent, response, token_usage, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(cache_key) DO UPDATE SET response = excluded.response, token_usage = excluded.token_usage, expires_at = excluded.expires_at;",
                    (cache_key, query, intent, response, token_usage, now, expires_at)
                )
            logger.info(f"Cached cloud response for key '{cache_key[:12]}...' (TTL: {self.cache_ttl}s)")
        except Exception as e:
            logger.error(f"Error setting cloud cache: {e}")

    def clear_cache(self):
        try:
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM cloud_cache;")
        except Exception as e:
            logger.error(f"Error clearing cloud cache: {e}")

    # -----------------------------------------------------------------
    # Token Quota & Monthly Budget Tracking
    # -----------------------------------------------------------------

    def get_monthly_tokens_used(self) -> int:
        """Reads cumulative cloud token usage for the current month from SQLite preferences."""
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM preferences WHERE key = 'cloud_tokens_used_this_month';")
                row = cursor.fetchone()
                if row:
                    return int(row["value"])
        except Exception as e:
            logger.error(f"Error reading cloud token usage: {e}")
        return 0

    def record_tokens_used(self, tokens: int):
        """Increments cumulative monthly cloud tokens in SQLite preferences."""
        if tokens <= 0:
            return
        now = int(time.time())
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM preferences WHERE key = 'cloud_tokens_used_this_month';")
                row = cursor.fetchone()
                current_used = int(row["value"]) if row else 0
                new_used = current_used + tokens

                conn.execute(
                    "INSERT INTO preferences (key, value, created_at, updated_at) VALUES ('cloud_tokens_used_this_month', ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;",
                    (str(new_used), now, now)
                )
            logger.info(f"Recorded {tokens} cloud tokens. Monthly total: {new_used}/{self.monthly_token_cap}")
        except Exception as e:
            logger.error(f"Error recording cloud token usage: {e}")

    def reset_monthly_token_usage(self):
        now = int(time.time())
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO preferences (key, value, created_at, updated_at) VALUES ('cloud_tokens_used_this_month', '0', ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = '0', updated_at = excluded.updated_at;",
                    (now, now)
                )
        except Exception as e:
            logger.error(f"Error resetting cloud tokens: {e}")

    # -----------------------------------------------------------------
    # Context Pruning / Truncation
    # -----------------------------------------------------------------

    def _truncate_or_summarize_context(self, prompt: str) -> str:
        char_soft_limit = self.soft_token_limit * 4
        if len(prompt) <= char_soft_limit:
            return prompt

        logger.warning(f"Prompt length ({len(prompt)} chars) exceeds soft limit ({char_soft_limit} chars). Truncating context.")
        head = prompt[: int(char_soft_limit * 0.7)]
        tail = prompt[-int(char_soft_limit * 0.25):]
        return f"{head}\n\n[... Context truncated by Jarvis Cloud Client to respect token budget ...]\n\n{tail}"

    # -----------------------------------------------------------------
    # Escalation Entry Point with Retry & Backoff
    # -----------------------------------------------------------------

    async def escalate(self, query: str, intent_data: dict) -> dict:
        """
        Escalates query to cloud provider following Section 36 specification:
        1. Fast offline connectivity check.
        2. SHA256 response caching with 24h TTL.
        3. Monthly token quota budget verification.
        4. Context truncation at soft limit.
        5. Exponential backoff retry on HTTP 429 / 5xx / timeouts (up to 3 attempts).
        """
        intent = intent_data.get("intent", "research")
        context = intent_data.get("context", {})

        # 1. Offline Detection
        is_online = await self.check_connectivity()
        if not is_online:
            msg = "Cloud service is currently unreachable (offline). Please check your internet connection or use local tools."
            logger.warning(f"Escalation aborted: {msg}")
            return {
                "status": "failure",
                "error": "cloud_offline",
                "response": msg,
                "route": "cloud",
                "cached": False,
                "tokens_used": 0
            }

        # 2. Response Cache Check
        cache_key = self._compute_cache_key(query, intent, context)
        cached = self.get_cached_response(cache_key)
        if cached:
            return {
                "status": "success",
                "response": cached["response"],
                "route": "cloud",
                "cached": True,
                "tokens_used": 0
            }

        # 3. Monthly Token Quota Check
        used_this_month = self.get_monthly_tokens_used()
        if used_this_month >= self.monthly_token_cap:
            msg = f"Monthly cloud token cap exceeded ({used_this_month}/{self.monthly_token_cap} tokens). Escalation blocked to prevent unexpected costs."
            logger.warning(msg)
            return {
                "status": "failure",
                "error": "monthly_quota_exceeded",
                "response": msg,
                "route": "cloud",
                "cached": False,
                "tokens_used": 0
            }

        # 4. Context Budget Management
        bounded_prompt = self._truncate_or_summarize_context(query)
        system_prompt = intent_data.get("system_prompt") or DEFAULT_JARVIS_PERSONA

        # 5. Execution with Exponential Backoff Retry
        last_error = None
        max_attempts = 1 + len(self.retry_backoffs)

        for attempt in range(max_attempts):
            try:
                logger.info(f"Calling cloud provider (attempt {attempt + 1}/{max_attempts})...")
                call_res = await self.provider.call(bounded_prompt, system_prompt=system_prompt)
                
                content = call_res.get("content", "")
                total_tokens = call_res.get("total_tokens", 0)

                # Persist to cache and update token quota
                self.set_cached_response(cache_key, query, intent, content, total_tokens)
                self.record_tokens_used(total_tokens)

                return {
                    "status": "success",
                    "response": content,
                    "route": "cloud",
                    "cached": False,
                    "tokens_used": total_tokens,
                    "model": call_res.get("model", "unknown"),
                    "tier": call_res.get("tier", "Cloud")
                }

            except CloudAPIError as e:
                last_error = e
                if e.status_code in (400, 401, 403):
                    logger.error(f"Non-retryable cloud error {e.status_code}: {e.message}")
                    break

                if attempt < len(self.retry_backoffs):
                    delay = self.retry_backoffs[attempt]
                    logger.warning(f"Cloud call attempt {attempt + 1} failed with status {e.status_code}. Retrying in {delay}s...")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"Cloud call exhausted all {max_attempts} attempts. Last error: {e}")

            except Exception as e:
                last_error = e
                if attempt < len(self.retry_backoffs):
                    delay = self.retry_backoffs[attempt]
                    logger.warning(f"Cloud call network error: {e}. Retrying in {delay}s...")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"Cloud call network failure after {max_attempts} attempts: {e}")

        # Fallback when retries exhausted
        err_msg = f"Cloud escalation unavailable after retries: {last_error}"
        return {
            "status": "failure",
            "error": "cloud_unavailable",
            "response": err_msg,
            "route": "cloud",
            "cached": False,
            "tokens_used": 0
        }
