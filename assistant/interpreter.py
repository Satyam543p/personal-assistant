import abc
import json
import logging
import re
import urllib.request
import urllib.error
try:
    import assistant.config as config
    from assistant.config import LLAMACPP_URL, OLLAMA_URL, INTERPRETER_TYPE
except ModuleNotFoundError:
    import config
    from config import LLAMACPP_URL, OLLAMA_URL, INTERPRETER_TYPE

logger = logging.getLogger("jarvis.interpreter")

try:
    from assistant.context_engine import context_engine
except ModuleNotFoundError:
    try:
        from context_engine import context_engine
    except Exception:
        context_engine = None

try:
    from assistant.time_parser import parse_natural_time
except ModuleNotFoundError:
    try:
        from time_parser import parse_natural_time
    except Exception:
        parse_natural_time = None


# Canonical System Prompt defining the output JSON schema contract
SYSTEM_PROMPT = """You are the Qwen local interpreter for Jarvis personal assistant.
Your job is to parse the user's natural language input and output a structured JSON intent classification object.
You must output ONLY a valid JSON object matching this schema:
{
  "intent": "open_project | open_app | run_project | search_web | memory_query | recommend_next_action | coding_help | create_tool | execute_tool | summarize | learn_topic | multi_step_task | download_content | file_management | system_control | research | conversation",
  "entities": {
    "project_name": "string or null",
    "app_name": "string or null",
    "file_ref": "string or null",
    "topic": "string or null",
    "time_ref": "string or null"
  },
  "resolved_reference": "string or null",
  "confidence": float between 0.0 and 1.0,
  "needs_clarification": boolean,
  "clarification_question": "string or null",
  "suggested_route": "tool | memory | planner | local_model | cloud"
}

Intent mapping guidelines:
- "open project_name", "go to project_name" -> intent="open_project", suggested_route="tool"
- "open app_name", "start app_name" -> intent="open_app", suggested_route="tool"
- "run project_name", "start server" -> intent="run_project", suggested_route="tool"
- "search web for X", "google X" -> intent="search_web", suggested_route="tool"
- "what was my last goal", "retrieve memories" -> intent="memory_query", suggested_route="memory"
- "what should I do next", "suggest next step" -> intent="recommend_next_action", suggested_route="memory"
- "how do I write a binary search in Python" -> intent="coding_help", suggested_route="local_model"
- "summarize file_ref" -> intent="summarize", suggested_route="local_model"
- "create a new React app" -> intent="multi_step_task", suggested_route="planner"
- "research topic" -> intent="research", suggested_route="cloud"
- Casual chatting ("hi", "how are you") -> intent="conversation", suggested_route="local_model"

Output ONLY the raw JSON block. Do not wrap in markdown tags, and do not append any explanations."""

# Standard default dictionary schema template
DEFAULT_SCHEMA = {
    "intent": "conversation",
    "entities": {
        "project_name": None,
        "app_name": None,
        "file_ref": None,
        "topic": None,
        "time_ref": None,
        "profile_name": None
    },
    "resolved_reference": None,
    "confidence": 0.5,
    "needs_clarification": False,
    "clarification_question": None,
    "suggested_route": "local_model"
}

class LocalInterpreter(abc.ABC):
    @abc.abstractmethod
    async def interpret(self, query: str, context: dict = None) -> dict:
        """
        Parses a user query and returns a structured JSON intent classification.
        """
        pass

def normalize_interpretation(data: dict, provider: str, reasoning: str) -> dict:
    """
    Ensure the classification dictionary matches the strict output schema contract.
    """
    normalized = DEFAULT_SCHEMA.copy()
    if not isinstance(data, dict):
        data = {}
        
    normalized["intent"] = data.get("intent", "conversation")
    normalized["confidence"] = float(data.get("confidence", 0.5))
    normalized["resolved_reference"] = data.get("resolved_reference", None)
    normalized["needs_clarification"] = bool(data.get("needs_clarification", False))
    normalized["clarification_question"] = data.get("clarification_question", None)
    normalized["suggested_route"] = data.get("suggested_route", "local_model")
    
    # Custom added fields from user refinement
    normalized["requires_clarification"] = normalized["needs_clarification"]
    
    # Normalize entities dictionary
    input_entities = data.get("entities", {})
    if not isinstance(input_entities, dict):
        input_entities = {}
    normalized["entities"] = {
        "project_name": input_entities.get("project_name", None),
        "app_name": input_entities.get("app_name", None),
        "file_ref": input_entities.get("file_ref", None),
        "topic": input_entities.get("topic", None),
        "time_ref": input_entities.get("time_ref", None),
        "profile_name": input_entities.get("profile_name", None)
    }
    for k, v in input_entities.items():
        if k not in normalized["entities"]:
            normalized["entities"][k] = v
    
    # Metadata for debugging / metrics logging
    normalized["metadata"] = {
        "provider": provider,
        "reasoning": reasoning
    }
    
    return normalized


def split_composite_query(query: str) -> list[str]:
    """
    Decomposes multi-task requests joined by conjunctions into discrete executable actions.
    e.g. 'open brave and open spotify' -> ['open brave', 'open spotify']
    Carefully preserves URLs, quoted parameters, and single tasks with destination clauses.
    """
    q_clean = query.strip()
    
    # 1. Do not split if query contains a URL (prevent breaking query params like &t= or commas)
    if re.search(r"https?://|www\.", q_clean, re.IGNORECASE):
        return [q_clean]

    # 2. Preserved compound idioms (e.g. 'open youtube in brave and play <song>')
    if re.search(r"^(?:could\s+you\s+|can\s+you\s+|please\s+)?open\s+(?:brave|chrome|youtube|spotify|edge|firefox)(?:\s+in\s+\w+)?\s+(?:and\s+)?play\s+", q_clean, re.IGNORECASE):
        return [q_clean]

    # 3. Don't split search queries containing conjunctions
    if re.search(r"^(?:search\s+for|google|find|lookup)\s+", q_clean, re.IGNORECASE):
        return [q_clean]

    # 4. Don't split single tasks that specify an output destination (e.g. 'save to downloads and keep name')
    if re.search(r"\b(?:save\s+to|save\s+in|save\s+kar|download\s+karke\s+save)\b", q_clean, re.IGNORECASE) and not re.search(r"\b(?:aur\s+phir|and\s+then)\b", q_clean, re.IGNORECASE):
        return [q_clean]

    # Split on explicit task conjunctions
    parts = re.split(r"\b(?:and\s+then|and\s+also|aur\s+phir|aur\s+bhi|and|then|aur)\b|[,;]\s*", q_clean, flags=re.IGNORECASE)
    valid_parts = []
    for p in parts:
        p_strip = p.strip()
        if len(p_strip) > 2 and not re.match(r"^(?:please|also|now|then|can\s+you)$", p_strip, re.IGNORECASE):
            valid_parts.append(p_strip)

    if len(valid_parts) >= 2:
        action_verbs = (
            "open", "play", "start", "launch", "run", "take", "show", "switch",
            "lock", "mute", "unmute", "stop", "pause", "kholo", "chalao", "bajao",
            "dikhao", "capture", "close", "band", "download", "save", "remind",
            "summarize", "search", "read", "write", "screenshot", "lo", "le"
        )
        # Every candidate part must contain an explicit action verb to be an independent task
        action_count = sum(1 for p in valid_parts if any(v in p.lower().split() or p.lower().startswith(v) for v in action_verbs))
        if action_count >= 2 and action_count == len(valid_parts):
            return valid_parts

    return [q_clean]


def extract_media_intent(query: str) -> dict | None:
    """
    Extracts semantic media/song/music playback intent.
    Supports user-directed platforms (Spotify, Soundcloud, YouTube) with YouTube as the primary default.
    """
    ql = query.lower().strip()
    # Only pause, stop, next, previous are pure playback controls without content
    if re.search(r"^(?:pause|stop|next|previous)(?:\s+(?:track|video|music|song|playback))?$", ql):
        return None

    media_patterns = [
        r"(?:open\s+youtube\s+(?:in\s+\w+\s+)?(?:and\s+)?play\s+(.+))",
        r"(?:open\s+(?:brave|chrome|firefox|edge)\s+(?:and\s+)?(?:open\s+youtube\s+)?(?:and\s+)?play\s+(.+))",
        r"(?:play|stream|listen\s+to|put\s+on|blast)\s+(?:the\s+)?(?:song\s+|music\s+|track\s+)?(.+)",
        r"^(?:play\s+music|play\s+a\s+song|play\s+some\s+music|play\s+something|gana\s+bajao|gana\s+chalao)$",
        r"(?:gana\s+(?:bajao|chalao|laga\s+do)|song\s+chalao)\s*(.+)?",
        r"(.+?)\s+(?:gana\s+)?(?:chalao|bajao|laga\s+do|play\s+karo)"
    ]

    matched_title = None
    for pat in media_patterns:
        m = re.search(pat, ql)
        if m:
            if m.groups() and m.group(1):
                matched_title = m.group(1).strip()
            else:
                matched_title = "trending songs"
            break

    if not matched_title:
        return None

    # If generic request like "music" or "a song", default to trending music
    if matched_title in ("music", "some music", "a song", "songs", "gana", "koi gana", "something"):
        matched_title = "trending songs"

    # Detect explicit platform
    platform = "youtube" # Default!
    if "spotify" in ql:
        platform = "spotify"
    elif "soundcloud" in ql:
        platform = "soundcloud"
    elif "youtube" in ql:
        platform = "youtube"

    # Detect browser
    browser = "brave" if "brave" in ql else ("chrome" if "chrome" in ql else None)

    # Clean title
    clean_title = matched_title
    clean_title = re.sub(r"^(?:youtube\s+pe|spotify\s+pe)\s+", "", clean_title, flags=re.IGNORECASE).strip()
    clean_title = re.sub(r"\s+(?:on|in)\s+(?:youtube|spotify|soundcloud|brave|chrome|firefox|edge|browser)$", "", clean_title, flags=re.IGNORECASE).strip()
    clean_title = re.sub(r"\s+(?:on|in)\s+(?:youtube|spotify|soundcloud|brave|chrome|firefox|edge|browser)$", "", clean_title, flags=re.IGNORECASE).strip()
    clean_title = re.sub(r"^(?:on|in)\s+(?:youtube|spotify|soundcloud|brave|chrome)\s+", "", clean_title, flags=re.IGNORECASE).strip()
    clean_title = re.sub(r"\s+here$", "", clean_title, flags=re.IGNORECASE).strip()

    if not clean_title or len(clean_title) < 2:
        return None

    return {
        "title": clean_title,
        "platform": platform,
        "browser": browser
    }


def parse_smart_web_search(query: str) -> dict | None:
    """
    Parses conversational search requests across Hindi, Hinglish, and English.
    Extracts the clean topic without leaving behind 'karo', browser mentions, or stop words.
    Examples:
      - 'brave mein search karo best Hindi anime sites' -> 'best Hindi anime sites'
      - 'find website that has anime in hindi for free' -> 'website that has anime in hindi for free'
      - 'find website where i can download movies' -> 'website download movies'
      - 'google pe search karo latest tech news' -> 'latest tech news'
    """
    ql = query.strip().lower()

    # Exclude non-search patterns
    if any(k in ql for k in ("gana bajao", "song", "remind", "volume", "take screenshot", "open project", "run project")):
        return None

    # Detect browser preference
    browser = None
    if "brave" in ql:
        browser = "brave"
    elif "chrome" in ql:
        browser = "chrome"

    # 1. Hindi/Hinglish: 'brave mein search karo best hindi anime sites'
    m_hi = re.search(r"(?:(?:brave|chrome|google|browser)\s+(?:mein|me|pe)\s+)?search\s+(?:karo|kijiye|kar\s+do)\s+(.+)", ql)
    if m_hi:
        topic = m_hi.group(1).strip()
        topic = re.sub(r"^(?:karo|kijiye|kripya|please)\s+", "", topic).strip()
        if len(topic) > 1:
            return {"query": topic, "browser": browser}

    # 2. Hindi/Hinglish suffix: '<topic> search karo / dhoondo / khojo'
    m_hi2 = re.search(r"(.+?)\s+(?:search\s+karo|dhoondo|khojo|pata\s+lagao)$", ql)
    if m_hi2:
        topic = m_hi2.group(1).strip()
        topic = re.sub(r"^(?:brave|chrome|google|browser)\s+(?:mein|me|pe)\s+", "", topic).strip()
        if len(topic) > 1:
            return {"query": topic, "browser": browser}

    # 3. 'find website that has...', 'find website where...', 'find website to...'
    m_find_web = re.search(r"\b(?:find|search|look\s+up|suggest)\s+(?:a\s+)?(?:good\s+)?(?:website|site|portal)\s+(?:that\s+has|where\s+i\s+can|to|for)\s+(.+)", ql)
    if m_find_web:
        clean_topic = m_find_web.group(1).strip()
        return {"query": f"website {clean_topic}", "browser": browser}

    # 4. 'find website for downloading movies'
    m_site = re.search(r"\b(?:website|site)\s+(?:for|to|where)\s+(.+)", ql)
    if m_site:
        return {"query": f"website {m_site.group(1).strip()}", "browser": browser}

    # 5. Direct search: 'search for X', 'google X', 'look up X'
    m_dir = re.search(r"\b(?:search|google|look\s+up)\s+(?:for\s+)?(.+)", ql)
    if m_dir and not any(k in ql for k in ("codebase", "code", "file", "folder", "project")):
        raw_topic = m_dir.group(1).strip()
        raw_topic = re.sub(r"^(?:in|on)\s+(?:brave|chrome|browser)\s+", "", raw_topic).strip()
        raw_topic = re.sub(r"\s+(?:in|on)\s+(?:brave|chrome|browser)$", "", raw_topic).strip()
        raw_topic = re.sub(r"\s+karo$", "", raw_topic).strip()
        if len(raw_topic) > 1:
            return {"query": raw_topic, "browser": browser}

    # 6. 'find <query>' (e.g. 'find anime in hindi for free', 'find website to download movies')
    m_find = re.search(r"^find\s+(.+)", ql)
    if m_find and not any(k in ql for k in ("file", "code", "folder", "project", "my", "me")):
        topic = m_find.group(1).strip()
        topic = re.sub(r"^(?:a\s+|an\s+|the\s+)", "", topic).strip()
        if len(topic) > 2:
            return {"query": topic, "browser": browser}

    return None


def resolve_media_platform(url: str) -> str:
    """Extracts standardized platform identifier from a URL."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "music.youtube.com" in host:
        return "youtube_music"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube_shorts" if "/shorts/" in path else "youtube"
    if "instagram.com" in host:
        return "instagram"
    if "tiktok.com" in host:
        return "tiktok"
    if "facebook.com" in host or "fb.watch" in host:
        return "facebook"
    if "twitter.com" in host or "x.com" in host:
        return "twitter"
    if "vimeo.com" in host:
        return "vimeo"
    if "reddit.com" in host or "v.redd.it" in host:
        return "reddit"
    return "generic"


def parse_guarded_download_request(query: str, context: dict = None) -> dict | None:
    """
    Evaluates query for guarded media download intent.
    Requires:
    1. An explicit HTTP/HTTPS URL, OR
    2. An explicit media keyword (video, reel, song, audio, playlist, short, mp3, mp4, gaana, clip)
       combined with download/save phrasing, OR
    3. Anaphora referencing context.last_url (e.g. 'isko download kar', 'ye download karo').
    """
    q_clean = query.strip()
    q_lower = q_clean.lower()
    url_m = re.search(r"https?://[^\s]+", q_clean)
    url = url_m.group(0).rstrip(".,;!?\"'") if url_m else None

    has_anaphora = bool(re.search(r"\b(isko|ise|ye|yeh|this|it)\b", q_lower))
    if not url and has_anaphora:
        last_url = (context or {}).get("last_url")
        if not last_url and context_engine and hasattr(context_engine, "active_entities"):
            last_url = context_engine.active_entities.get("last_url")
        if last_url:
            url = last_url

    has_download_action = bool(re.search(
        r"\b(download|save|nikalo|get|extract|rip)\b|(?:download\s+kar|download\s+karo|save\s+karo|save\s+kar)",
        q_lower
    ))

    media_keywords = {
        "video": "download_video",
        "clip": "download_video",
        "reel": "download_video",
        "short": "download_video",
        "mp4": "download_video",
        "audio": "extract_audio",
        "song": "download_song",
        "gaana": "download_song",
        "gana": "download_song",
        "mp3": "extract_audio",
        "playlist": "inspect_playlist",
        "subtitles": "extract_subtitles",
        "transcript": "extract_subtitles"
    }

    matched_type = None
    for kw, intent_name in media_keywords.items():
        if re.search(rf"\b{kw}\b", q_lower):
            matched_type = (kw, intent_name)
            break

    if not url and not matched_type:
        return None
    if not has_download_action and not url:
        return None

    intent = matched_type[1] if matched_type else "download_video"
    if re.search(r"\b(mp3|audio|sound)\b", q_lower) and intent == "download_video":
        intent = "extract_audio"

    source = url
    if not source:
        title = q_clean
        for p in [
            r"\b(download|save|nikalo|karo|kar|do|chahiye|please)\b",
            r"\b(video|reel|song|gaana|gana|audio|mp3|mp4|short)\b",
            r"\b(from|of|for|ka|ki|ke|pe|on)\s+(?:youtube|instagram|spotify)?\b",
            r"\b(isko|ise|ye|yeh|this|it)\b"
        ]:
            title = re.sub(p, "", title, flags=re.IGNORECASE)
        source = re.sub(r"\s+", " ", title).strip()
        if not source or len(source) < 2 or source.lower() in ("media", "none", "video", "song"):
            return None

    platform = resolve_media_platform(source) if url else "search"
    entities = {
        "source": source,
        "url": source if url else None,
        "platform": platform
    }
    if intent == "download_song":
        entities["title"] = source
        entities["song_title"] = source

    return {"intent": intent, "entities": entities, "platform": platform}


class RuleBasedInterpreter(LocalInterpreter):
    def __init__(self):
        self.provider_name = "rule_based"

    async def interpret(self, query: str, context: dict = None) -> dict:
        query_clean = query.strip()
        query_lower = query_clean.lower()

        # ── 0. Context-First Check: Anaphora / Follow-up Resolution ──
        if context_engine:
            resolved_anaphora = context_engine.resolve_context_anaphora(query_clean)
            if resolved_anaphora:
                logger.info(f"RuleBasedInterpreter: Resolved via ContextEngine: {resolved_anaphora.get('intent')}")
                return normalize_interpretation(resolved_anaphora, "context_engine", "Resolved via conversational context")

        # ── 1. Multi-Task Query Decomposition ──
        sub_tasks = split_composite_query(query_clean)
        if len(sub_tasks) >= 2:
            logger.info(f"RuleBasedInterpreter: Detected {len(sub_tasks)} composite tasks: {sub_tasks}")
            return normalize_interpretation({
                "intent": "multi_task",
                "entities": {"tasks": sub_tasks},
                "confidence": 0.98,
                "suggested_route": "tool",
                "needs_clarification": False
            }, "rule_based", f"Detected {len(sub_tasks)} composite tasks")

        # ── 1b. Guarded Media Download NLU ──
        download_info = parse_guarded_download_request(query_clean, context)
        if download_info:
            return normalize_interpretation({
                "intent": download_info["intent"],
                "entities": download_info["entities"],
                "resolved_reference": download_info["entities"].get("source") or download_info["entities"].get("url") or "",
                "confidence": 0.98,
                "suggested_route": "tool",
                "needs_clarification": False
            }, "rule_based", f"Guarded download intent for {download_info.get('platform', 'media')}")

        # ── 1c. Smart Reminders & Natural Time Parsing (Asia/Kolkata) ──
        if parse_natural_time and not any(k in query_lower for k in ("send notification", "desktop alert", "push notification", "toast notification")):
            if re.search(r"\b(reminder|remind|yaad dilana|yaad dila dena|yaad dilao|alarm)\b", query_lower):
                parsed_time = parse_natural_time(query_clean)
                if parsed_time:
                    return normalize_interpretation({
                        "intent": "create_reminder",
                        "entities": {
                            "time_ist": parsed_time.dt_ist.isoformat(),
                            "time_utc": parsed_time.dt_utc.isoformat(),
                            "spoken_time": parsed_time.spoken_time,
                            "reminder_text": parsed_time.reminder_text,
                            "is_tomorrow": parsed_time.is_tomorrow
                        },
                        "resolved_reference": parsed_time.spoken_time,
                        "confidence": 0.98,
                        "suggested_route": "tool",
                        "needs_clarification": False
                    }, "rule_based", f"Parsed reminder for {parsed_time.spoken_time}")
                elif re.search(r"\b(cancel|delete|hatao|rok|ruko)\b", query_lower):
                    return normalize_interpretation({
                        "intent": "cancel_reminder",
                        "entities": {"query": query_clean},
                        "confidence": 0.95,
                        "suggested_route": "tool",
                        "needs_clarification": False
                    }, "rule_based", "Cancel reminder intent")
                elif re.search(r"\b(list|show|dikhao|batao|check)\b", query_lower):
                    return normalize_interpretation({
                        "intent": "list_reminders",
                        "entities": {},
                        "confidence": 0.95,
                        "suggested_route": "tool",
                        "needs_clarification": False
                    }, "rule_based", "List reminders intent")

        # ── 1d. Smart Web Search (Hindi, Hinglish & Natural Language) ──
        search_info = parse_smart_web_search(query_clean)
        if search_info:
            return normalize_interpretation({
                "intent": "search_web",
                "entities": {
                    "topic": search_info["query"],
                    "query": search_info["query"],
                    "browser": search_info.get("browser")
                },
                "resolved_reference": search_info["query"],
                "confidence": 0.98,
                "suggested_route": "tool",
                "needs_clarification": False
            }, "rule_based", f"Smart web search for '{search_info['query']}'")

        # ── 1d. Safe Undo Action Intent ──
        if re.search(r"^(?:undo|undo karo|undo last action|undo that|pichla action wapas lo|wapas lo|revert|undo download|undo commit)$", query_lower) or re.search(r"\b(undo last action|undo last|undo download|undo commit|undo karo)\b", query_lower):
            return normalize_interpretation({
                "intent": "undo_action",
                "entities": {"query": query_clean},
                "confidence": 0.98,
                "suggested_route": "tool",
                "needs_clarification": False
            }, "rule_based", "Safe undo action intent")

        # ── 1e. Cancel Download Intent ──
        if re.search(r"^(?:cancel download|download cancel karo|download roko|stop download)$", query_lower):
            return normalize_interpretation({
                "intent": "cancel_download",
                "entities": {},
                "confidence": 0.98,
                "suggested_route": "tool",
                "needs_clarification": False
            }, "rule_based", "Cancel download intent")

        # ── 1f. Remember Fact Intent ──
        m_rem = re.search(
            r"^(?:remember that|remember|yaad rakh(?:na)?(?:\s+ki)?)\s+(.+)$",
            query_lower
        )
        if m_rem:
            raw_fact = m_rem.group(1).strip()
            # Try to split into key/value if contains 'is', 'hai', ':', '='
            m_kv = re.search(r"^(?:mera|meri|my)?\s*(.+?)\s+(?:is|hai|=|:)\s+(.+)$", raw_fact)
            if m_kv:
                fact_key = m_kv.group(1).strip()
                fact_val = m_kv.group(2).strip()
            else:
                fact_key = raw_fact.split()[0] if raw_fact.split() else "note"
                fact_val = raw_fact
            return normalize_interpretation({
                "intent": "store_memory",
                "entities": {"key": fact_key, "content": fact_val, "category": "preference"},
                "confidence": 0.96,
                "suggested_route": "tool",
                "needs_clarification": False
            }, "rule_based", "Store memory intent")

        # ── 1g. Forget Fact Intent ──
        m_forg = re.search(
            r"^(?:forget that|forget|bhool jao|hata do yaad se)\s+(.+)$",
            query_lower
        ) or re.search(r"^(.+?)\s+(?:bhool jao|yaad mat rakhna)$", query_lower)
        if m_forg:
            target = m_forg.group(1).strip()
            return normalize_interpretation({
                "intent": "forget_fact",
                "entities": {"query": target},
                "confidence": 0.96,
                "suggested_route": "tool",
                "needs_clarification": False
            }, "rule_based", "Forget fact intent")

        # ── 1h. Query Knowledge Intent ──
        qk_pattern = (
            r"^(?:what do you know about me|who am i|batao mere bare me|"
            r"mere bare me kya jante ho|what do you know about)\b"
        )
        if re.search(qk_pattern, query_lower):
            m_q = re.search(r"(?:about|bare me)\s+(.+)$", query_lower)
            q_target = m_q.group(1).strip() if m_q else "profile"
            return normalize_interpretation({
                "intent": "query_knowledge",
                "entities": {"query": q_target},
                "confidence": 0.95,
                "suggested_route": "tool",
                "needs_clarification": False
            }, "rule_based", "Query knowledge intent")

        # ── 2. Semantic Media & Music Intent (Dynamic Platform + YouTube Default) ──
        media_info = extract_media_intent(query_clean)
        if media_info:
            return normalize_interpretation({
                "intent": "play_media",
                "entities": media_info,
                "resolved_reference": media_info["title"],
                "confidence": 0.99,
                "suggested_route": "tool",
                "needs_clarification": False
            }, "rule_based", f"Semantic music intent: {media_info['title']} on {media_info['platform']}")

        # ── 2b. Smart Web & Browser Search Intent ──
        web_search_info = parse_smart_web_search(query_clean)
        if web_search_info:
            return normalize_interpretation({
                "intent": "web_search_browser",
                "entities": web_search_info,
                "resolved_reference": web_search_info["query"],
                "confidence": 0.98,
                "suggested_route": "tool",
                "needs_clarification": False
            }, "rule_based", f"Smart web search intent: {web_search_info['query']}")

        # Default starting dictionary
        data = {
            "intent": "conversation",
            "entities": {},
            "confidence": 0.5,
            "needs_clarification": False,
            "clarification_question": None,
            "suggested_route": "local_model"
        }

        # Match conversation/greetings/capabilities (strictly start of query or with assistant name to avoid Hindi 'tum hi ho')
        if (re.search(r"^(?:hi|hello|hey|yo|greetings)\b", query_lower) or
            re.search(r"\b(?:hi|hello|hey|yo)\s+(?:kate|jarvis)\b", query_lower) or
            re.search(r"\b(?:who are you|what can you do|what things you can do|capabilities|capability|features|what do you do|help me|how can you help|introduce yourself|tell me about yourself)\b", query_lower)):
            data["intent"] = "conversation"
            data["confidence"] = 0.95
            data["suggested_route"] = "local_model"

        # ── Desktop & Media Commands (Kate Expanded Capabilities) ───────────
        # 1. Screenshot Capture
        elif re.search(r"\b(screenshot|capture screen|screen capture|screenshot lo|screenshot le lo)\b", query_lower):
            data["intent"] = "take_screenshot"
            data["confidence"] = 0.98
            data["suggested_route"] = "tool"

        # 2. Show Desktop / Clear Screen
        elif re.search(r"\b(show desktop|clear screen|minimize all|desktop dikhao|screen clear karo)\b", query_lower):
            data["intent"] = "show_desktop"
            data["confidence"] = 0.98
            data["suggested_route"] = "tool"

        # 3. Window Switching
        elif re.search(r"\b(switch window to|bring to front|focus window|pe switch karo)\b", query_lower):
            target = re.sub(r"\b(switch window to|bring to front|focus window|pe switch karo)\b", "", query_lower).strip()
            data["intent"] = "switch_window"
            data["entities"]["window_title"] = target or "app"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # 4. Media Playback Control (Stop, Pause, Resume, Next)
        elif re.search(r"\b(?:stop|pause|resume)\s+(?:the\s+)?(?:video|music|song|track|playback)\b|\b(?:next|previous)\s+track\b|\b(?:video\s+roko|pause\s+karo|gana\s+roko)\b|\b(?:pause|resume)\b", query_lower):
            data["intent"] = "control_media"
            action = "play_pause"
            if "stop" in query_lower or "roko" in query_lower:
                action = "stop"
            elif "next" in query_lower:
                action = "next"
            elif "prev" in query_lower:
                action = "prev"
            data["entities"]["action"] = action
            data["confidence"] = 0.98
            data["suggested_route"] = "tool"

        # 5. Audio Volume Control (Mute, Volume Up/Down)
        elif re.search(r"\b(?:mute\s+audio|mute\s+sound|mute|unmute)\b|\b(?:turn\s+)?volume\s+(?:up|down|increase|decrease)\b|\b(?:volume\s+badhao|volume\s+kam\s+karo|awaaz\s+badhao|awaaz\s+kam\s+karo)\b", query_lower):
            data["intent"] = "control_volume"
            action = "toggle_mute"
            if "up" in query_lower or "badhao" in query_lower or "increase" in query_lower:
                action = "up"
            elif "down" in query_lower or "kam" in query_lower or "decrease" in query_lower:
                action = "down"
            data["entities"]["action"] = action
            data["confidence"] = 0.98
            data["suggested_route"] = "tool"

        # 6. Lock Workstation / Laptop
        elif re.search(r"\b(lock my laptop|lock my pc|lock pc|lock workstation|laptop lock karo|pc lock karo)\b", query_lower):
            data["intent"] = "lock_workstation"
            data["confidence"] = 0.98
            data["suggested_route"] = "tool"

        # 7. Smart Folder Organizer
        elif re.search(r"\b(organize downloads|organize my folder|organize folder|clean downloads|downloads saaf karo|downloads organize karo)\b", query_lower):
            data["intent"] = "organize_folder"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # 8. Song / Music Downloader
        elif re.search(r"\b(download song|download music|download track|gana download karo|song download karo)\b", query_lower):
            s_match = re.search(r"\b(?:download song|download music|download track|gana download karo|song download karo)\s+(.*)", query_lower)
            song_title = s_match.group(1).strip() if s_match else query_lower
            data["intent"] = "download_song"
            data["entities"]["title"] = song_title
            data["entities"]["query"] = song_title
            data["confidence"] = 0.98
            data["suggested_route"] = "tool"

        # 9. PDF Report Generator
        elif re.search(r"\b(make a pdf|create pdf|generate pdf|pdf banao|save as pdf)\b", query_lower):
            data["intent"] = "generate_pdf"
            data["entities"]["content"] = query
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        elif (re.search(r"\b(run|start server|exec|execute|launch)\b", query_lower) and
              not re.search(r"\b(dry[\s\-]run|prune|job|scheduled|background\s+job)\b", query_lower)):
            run_match = re.search(r"\b(run|start server|exec|execute|launch)\s+([a-zA-Z0-9_\-\s]+)", query_lower)
            proj = run_match.group(2).strip() if run_match else "unknown"
            if proj.startswith("project "):
                proj = proj[len("project "):].strip()
            data["intent"] = "run_project"
            data["entities"]["project_name"] = proj
            data["confidence"] = 0.9
            data["suggested_route"] = "tool"

        # Match workspace profile queries
        elif re.search(r"\b(current profile|active profile|what mode am i in|what is my mode|what profile am i in|list profiles|show profiles|available profiles)\b", query_lower):
            data["intent"] = "current_profile"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match workspace profile switching
        elif (re.search(r"\b(switch to|switch profile to|set mode to|change mode to|switch mode to|activate profile|set profile to)\s+([a-zA-Z0-9_\-]+)", query_lower) and
              any(k in query_lower for k in ("mode", "profile", "coding", "study", "research"))):
            sw_match = re.search(r"\b(?:switch to|switch profile to|set mode to|change mode to|switch mode to|activate profile|set profile to)\s+([a-zA-Z0-9_\-]+)", query_lower)
            p_cand = sw_match.group(1).strip() if sw_match else "coding"
            words = query_lower.split()
            for cand in ("coding", "study", "research", "project"):
                if cand in words:
                    p_cand = cand
                    break
            data["intent"] = "switch_profile"
            data["entities"]["profile_name"] = p_cand
            data["confidence"] = 0.95
        # Match switch project focus (Section 43)
        elif re.search(r"\b(switch focus to|focus on project|focus project)\b", query_lower):
            p_match = re.search(r"\b(?:switch focus to|focus on project|focus project)\s+(?:project\s+)?([a-zA-Z0-9_\-]+)", query_lower)
            p_id = p_match.group(1).strip() if p_match else "current"
            data["intent"] = "switch_project_focus"
            data["entities"]["project_id"] = p_id
            data["entities"]["project_name"] = p_id
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match switch project / workspace (Section 44)
        elif (re.search(r"\b(switch project to|switch to project|switch workspace to|resume work on project|work on project)\b", query_lower) or
              (re.search(r"\bswitch to\s+([a-zA-Z0-9_\-]+)\b", query_lower) and not any(k in query_lower for k in ("mode", "profile", "coding", "study", "research")))):
            p_match = re.search(r"\b(?:switch project to|switch to project|switch workspace to|resume work on project|work on project|switch to)\s+(?:project\s+)?([a-zA-Z0-9_\-]+)", query_lower)
            p_id = p_match.group(1).strip() if p_match else "current"
            data["intent"] = "switch_project"
            data["entities"]["project_name"] = p_id
            data["entities"]["project_id"] = p_id
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match list active projects (Section 44)
        elif re.search(r"\b(list active projects|show active projects|what projects am i working on|what projects am i on|show my projects|list my projects|active projects)\b", query_lower):
            data["intent"] = "list_active_projects"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match project catch-up brief (Section 45)
        elif (re.search(r"\b(catch up on|catchup on|catch-up brief|catchup brief|where did i leave off on|where did i leave off)\b", query_lower)):
            b_match = re.search(r"\b(?:catch up on|catchup on|catch-up brief for|catchup brief for|where did i leave off on|where did i leave off in)\s+(?:project\s+)?([a-zA-Z0-9_\-]+)", query_lower)
            p_id = b_match.group(1).strip() if b_match else "current"
            data["intent"] = "project_catchup_brief"
            data["entities"]["project_id"] = p_id
            data["entities"]["project_name"] = p_id
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match open project or app (supports English 'open brave' and Hindi 'brave kholo' / 'यूट्यूब खोलो')
        elif re.search(r"(?:\b(?:open|go to|cd to|view|launch|start|kholo|chalao)\b|(?:खोलो|चलाओ))", query_lower):
            if "project" in query_lower:
                open_match = re.search(r"(?:\b(?:open|go to|cd to|view|launch|start|kholo)\b|(?:खोलो))\s+(?:project\s+)?([a-zA-Z0-9_\-\s\.]+)", query_lower)
                proj = open_match.group(1).strip() if open_match else ""
                data["intent"] = "open_project"
                data["entities"]["project_name"] = proj
                data["confidence"] = 0.95
                data["suggested_route"] = "tool"
            else:
                # English order: 'open <target>'
                open_match = re.search(r"(?:\b(?:open|launch|start|kholo)\b|(?:खोलो))\s+(?:the\s+)?(?:app\s+|application\s+)?([a-zA-Z0-9_\-\s\.\u0900-\u097F]+)", query_lower)
                # Hindi order: '<target> kholo / खोलो'
                if not open_match or not open_match.group(1).strip():
                    open_match = re.search(r"([a-zA-Z0-9_\-\s\.\u0900-\u097F]+?)\s*(?:kholo|chalao|open\s+karo|खोलो|चलाओ)", query_lower)
                target = open_match.group(1).strip() if open_match else query_lower
                # Devanagari common transliterations
                devanagari_map = {
                    "यूट्यूब": "youtube",
                    "गूगल": "google",
                    "क्रोम": "chrome",
                    "ब्रेव": "brave",
                    "स्पॉटिफ़ाई": "spotify",
                    "स्पॉटिफाई": "spotify",
                    "नोटपैड": "notepad"
                }
                for hi_name, en_name in devanagari_map.items():
                    if hi_name in target:
                        target = target.replace(hi_name, en_name)
                # Strip trailing filler phrases
                target = re.sub(r"\s+(?:in|on)\s+(?:brave|chrome|browser)$", "", target, flags=re.IGNORECASE).strip()
                try:
                    from assistant.launchers import app_launcher
                    resolved_app = app_launcher.resolver.resolve(target)
                    if resolved_app:
                        target = resolved_app.name
                except Exception:
                    pass
                data["intent"] = "open_app"
                data["entities"]["app_name"] = target
                data["resolved_reference"] = target
                data["confidence"] = 0.98
                data["suggested_route"] = "tool"

        # Match finish video / study session
        elif (re.search(r"\b(finished (?:the )?video|done watching|finish video|complete (?:video|study) session|finished tutorial)\b", query_lower) or
              re.search(r"\bi finished the video\b", query_lower)):
            notes_match = re.search(r"\b(?:finished (?:the )?video|done watching|finish video)\s+(?:with note|note|summary)?\s*(.*)", query, re.IGNORECASE)
            notes = notes_match.group(1).strip() if notes_match else ""
            data["intent"] = "finish_video"
            data["entities"]["notes"] = notes
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match full video download (YouTube, Instagram, etc. - Section 47)
        elif re.search(r"\b(download video|save video|download reel|save reel|download instagram|download from youtube|download youtube video|download clip|download mp4)\b", query_lower):
            v_match = re.search(r"\b(?:download video|save video|download reel|save reel|download instagram|download from youtube|download youtube video|download clip|download mp4)\s+(?:from|for|of)?\s*(.+)", query, re.IGNORECASE)
            v_src = v_match.group(1).strip() if v_match else "unknown"
            # Check for resolution requested
            res_val = "720p"
            if "1080" in query_lower:
                res_val = "1080p"
                v_src = re.sub(r"\s+1080p?\b", "", v_src, flags=re.IGNORECASE).strip()
            elif "480" in query_lower:
                res_val = "480p"
                v_src = re.sub(r"\s+480p?\b", "", v_src, flags=re.IGNORECASE).strip()
            elif "360" in query_lower:
                res_val = "360p"
                v_src = re.sub(r"\s+360p?\b", "", v_src, flags=re.IGNORECASE).strip()
            elif "best" in query_lower:
                res_val = "best"
                v_src = re.sub(r"\s+best\b", "", v_src, flags=re.IGNORECASE).strip()
            data["intent"] = "download_video"
            data["entities"]["source"] = v_src
            data["entities"]["url"] = v_src
            data["entities"]["resolution"] = res_val
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match extract / download audio (Section 47)
        elif re.search(r"\b(extract audio|download audio|convert video to audio|rip audio|save audio|download mp3|get audio)\b", query_lower):
            a_match = re.search(r"\b(?:extract audio|download audio|convert video to audio|rip audio|save audio|download mp3|get audio)\s+(?:from|for|of)?\s*(.+)", query, re.IGNORECASE)
            a_src = a_match.group(1).strip() if a_match else "unknown"
            data["intent"] = "extract_audio"
            data["entities"]["source"] = a_src
            data["entities"]["url"] = a_src
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match extract subtitles / transcript (Section 47)
        elif re.search(r"\b(extract subtitles|download subtitles|get subtitles|get transcript|extract transcript|transcribe video|video transcript)\b", query_lower):
            s_match = re.search(r"\b(?:extract subtitles|download subtitles|get subtitles|get transcript|extract transcript|transcribe video|video transcript)\s+(?:from|for|of)?\s*(.+)", query, re.IGNORECASE)
            s_src = s_match.group(1).strip() if s_match else "unknown"
            data["intent"] = "extract_subtitles"
            data["entities"]["source"] = s_src
            data["entities"]["url"] = s_src
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match local media conversion & compression (Section 47)
        elif re.search(r"\b(convert media|convert video|convert audio|compress video|compress media|convert file)\b", query_lower):
            c_match = re.search(r"\b(?:convert media|convert video|convert audio|compress video|compress media|convert file)\s+(.+)", query, re.IGNORECASE)
            c_target = c_match.group(1).strip() if c_match else "unknown"
            fmt_target = "mp4"
            to_fmt_match = re.search(r"\s+to\s+([a-zA-Z0-9]+)$", c_target, re.IGNORECASE)
            if to_fmt_match:
                fmt_target = to_fmt_match.group(1).lower()
                c_target = c_target[:to_fmt_match.start()].strip()
            else:
                for f in ("mp3", "mp4", "wav", "mkv", "webm", "m4a"):
                    if f"to {f}" in query_lower:
                        fmt_target = f
                        break
            data["intent"] = "convert_media"
            data["entities"]["source"] = c_target
            data["entities"]["target_format"] = fmt_target
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match inspect playlist (Section 47)
        elif re.search(r"\b(inspect playlist|check playlist|list playlist|playlist info|playlist details)\b", query_lower):
            p_match = re.search(r"\b(?:inspect playlist|check playlist|list playlist|playlist info|playlist details)\s+(?:for|from|of)?\s*(.+)", query, re.IGNORECASE)
            p_src = p_match.group(1).strip() if p_match else "unknown"
            data["intent"] = "inspect_playlist"
            data["entities"]["url"] = p_src
            data["entities"]["source"] = p_src
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match get media metadata / inspect media (Section 47)
        elif re.search(r"\b(get media info|media info|video info|media metadata|inspect video|inspect media|media details)\b", query_lower):
            m_match = re.search(r"\b(?:get media info|media info|video info|media metadata|inspect video|inspect media|media details)\s+(?:for|from|of)?\s*(.+)", query, re.IGNORECASE)
            m_src = m_match.group(1).strip() if m_match else "unknown"
            data["intent"] = "get_media_info"
            data["entities"]["source"] = m_src
            data["entities"]["url"] = m_src
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match voice mode toggles (Section 48 / Milestone 19)
        elif re.search(r"\b(enable voice mode|turn on voice mode|start voice mode|turn on voice|enable voice|disable voice mode|turn off voice mode|stop voice mode|turn off voice|disable voice|mute voice)\b", query_lower):
            enable_val = not any(w in query_lower for w in ("disable", "turn off", "stop", "mute"))
            data["intent"] = "toggle_voice_mode"
            data["entities"]["enabled"] = enable_val
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match text to speech / speak text (Milestone 19)
        elif re.search(r"\b(?:speak text|speak message|read aloud|say out loud|speak aloud|speak:|say:)\b", query_lower) or (query_lower.startswith("speak ") and not query_lower.startswith("speak to")):
            s_match = re.search(r"\b(?:speak text|speak message|read aloud|say out loud|speak aloud|speak:|say:|speak)\s+(.+)", query, re.IGNORECASE)
            msg = s_match.group(1).strip() if s_match else ""
            data["intent"] = "speak_text"
            data["entities"]["text"] = msg
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match speech-to-text transcribe audio (Milestone 19)
        elif re.search(r"\b(transcribe audio|transcribe recording|transcribe voice note|transcribe voice|transcribe file|transcribe)\b", query_lower):
            t_match = re.search(r"\b(?:transcribe audio|transcribe recording|transcribe voice note|transcribe voice|transcribe file|transcribe)\s+(?:from|file)?\s*(.+)", query, re.IGNORECASE)
            f_path = t_match.group(1).strip() if t_match else ""
            data["intent"] = "transcribe_audio"
            data["entities"]["audio_path"] = f_path
            data["entities"]["source"] = f_path
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match deep website crawling (Milestone 20)
        elif re.search(r"\b(crawl website|crawl site|crawl docs|deep crawl|crawl domain|spider website|spider site|crawl)\b", query_lower) and any(x in query_lower for x in ("http://", "https://", "www.", ".com", ".org", ".io", ".dev", ".net", ".edu", "docs")):
            c_match = re.search(r"\b(?:crawl website|crawl site|crawl docs|deep crawl|crawl domain|spider website|spider site|crawl)\s+(?:from|url)?\s*(.+)", query, re.IGNORECASE)
            raw_target = c_match.group(1).strip() if c_match else ""
            
            # Extract depth if present (e.g. depth 3)
            depth_val = 2
            d_match = re.search(r"\bdepth\s+(\d+)\b", raw_target, re.IGNORECASE)
            if d_match:
                depth_val = int(d_match.group(1))
                raw_target = re.sub(r"\bdepth\s+\d+\b", "", raw_target, flags=re.IGNORECASE).strip()

            # Extract pages limit if present (e.g. 20 pages or limit 20)
            pages_val = 15
            p_match = re.search(r"\b(?:limit\s+)?(\d+)\s+pages?\b", raw_target, re.IGNORECASE)
            if p_match:
                pages_val = int(p_match.group(1))
                raw_target = re.sub(r"\b(?:limit\s+)?\d+\s+pages?\b", "", raw_target, flags=re.IGNORECASE).strip()

            data["intent"] = "crawl_website"
            data["entities"]["start_url"] = raw_target
            data["entities"]["url"] = raw_target
            data["entities"]["source"] = raw_target
            data["entities"]["max_depth"] = depth_val
            data["entities"]["max_pages"] = pages_val
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match dynamic browser page fetch (Milestone 20)
        elif re.search(r"\b(render dynamic page|fetch dynamic page|render page|browse dynamic|headless render|fetch js page)\b", query_lower):
            d_match = re.search(r"\b(?:render dynamic page|fetch dynamic page|render page|browse dynamic|headless render|fetch js page)\s+(?:from|url)?\s*(.+)", query, re.IGNORECASE)
            d_url = d_match.group(1).strip() if d_match else ""
            data["intent"] = "fetch_dynamic_page"
            data["entities"]["url"] = d_url
            data["entities"]["source"] = d_url
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match list scheduled jobs (Milestone 21)
        elif re.search(r"\b(list scheduled jobs|show scheduled jobs|show jobs|list jobs|scheduled jobs|background jobs|show background tasks|list background jobs)\b", query_lower):
            data["intent"] = "list_scheduled_jobs"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match proactive morning/evening briefing (Milestone 21)
        elif re.search(r"\b(morning briefing|evening digest|get briefing|proactive briefing|daily briefing|show briefing|today's briefing)\b", query_lower):
            feed_type = "evening_digest" if "evening" in query_lower else "morning_briefing"
            data["intent"] = "get_proactive_briefing"
            data["entities"]["feed_type"] = feed_type
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match run scheduled job now (Milestone 21)
        elif re.search(r"\b(run scheduled job|trigger job|execute job|run background job|run job)\b", query_lower):
            r_match = re.search(r"\b(?:run scheduled job|trigger job|execute job|run background job|run job)\s+(.+)", query, re.IGNORECASE)
            j_name = r_match.group(1).strip() if r_match else ""
            data["intent"] = "run_scheduled_job"
            data["entities"]["job_name"] = j_name
            data["entities"]["name"] = j_name
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match toggle job status (pause / resume) (Milestone 21)
        elif re.search(r"\b(pause job|stop job|disable job|resume job|start job|enable job)\b", query_lower):
            act = "pause" if any(w in query_lower for w in ("pause", "stop", "disable")) else "resume"
            t_match = re.search(r"\b(?:pause job|stop job|disable job|resume job|start job|enable job)\s+(.+)", query, re.IGNORECASE)
            j_name = t_match.group(1).strip() if t_match else ""
            data["intent"] = "toggle_job_status"
            data["entities"]["job_name"] = j_name
            data["entities"]["name"] = j_name
            data["entities"]["action"] = act
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match schedule job (Milestone 21)
        elif re.search(r"\b(schedule job|schedule background task|create scheduled job)\b", query_lower):
            s_match = re.search(r"\b(?:schedule job|schedule background task|create scheduled job)\s+(.+)", query, re.IGNORECASE)
            s_str = s_match.group(1).strip() if s_match else ""
            s_type = "daily_at" if ":" in s_str else "interval"
            data["intent"] = "schedule_job"
            data["entities"]["name"] = s_str.split()[0] if s_str else "custom_job"
            data["entities"]["schedule_type"] = s_type
            data["entities"]["schedule_value"] = "3600" if s_type == "interval" else "03:00"
            data["confidence"] = 0.9
            data["suggested_route"] = "tool"

        # Match watch / play video
        elif (re.search(r"\b(watch (?:a )?tutorial|watch video|play video|open video|watch)\s+([0-9]|first|second|third|#1|#2|#3|[a-zA-Z0-9_\-\s]+)", query_lower) and
              any(k in query_lower for k in ("video", "tutorial", "youtube", "1", "2", "3", "first", "second", "third", "#1", "#2", "#3"))):
            vid_match = re.search(r"\b(?:watch (?:a )?(?:tutorial on|video on|video|tutorial)?|play video|open video)\s+(.+)", query, re.IGNORECASE)
            vid_target = vid_match.group(1).strip() if vid_match else "1"
            data["intent"] = "watch_video"
            data["entities"]["query"] = vid_target
            data["entities"]["topic"] = vid_target
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match search youtube
        elif (re.search(r"\b(search youtube|youtube search)\b", query_lower) or
              re.search(r"\bfind (?:a )?video (?:about|on)\b", query_lower) or
              re.search(r"\blook up (?:a )?tutorial (?:about|on)\b", query_lower)):
            yt_match = re.search(r"\b(?:search youtube (?:for)?|youtube search (?:for)?|find (?:a )?video (?:about|on)|look up (?:a )?tutorial (?:about|on))\s+(.+)", query, re.IGNORECASE)
            q_target = yt_match.group(1).strip() if yt_match else "programming"
            data["intent"] = "search_youtube"
            data["entities"]["query"] = q_target
            data["entities"]["topic"] = q_target
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match autonomous web research (Section 42)
        elif (re.search(r"\b(research|deep\s+research|do\s+research|find\s+me\s+information\s+about|investigate)\b", query_lower) and
              not re.search(r"\b(youtube|video|tutorial)\b", query_lower)):
            is_deep = bool(re.search(r"\b(deep|comprehensive|thorough|in[\s\-]depth)\b", query_lower))
            r_match = re.search(r"\b(?:deep\s+research|do\s+research|research|find\s+me\s+information\s+about|investigate)(?:\s+(?:on|about|into|for))?\s+([a-zA-Z0-9_\-\s\.]+)", query_lower)
            topic_str = r_match.group(1).strip() if r_match else "technology"
            topic_str = re.sub(r"\b(deep|quickly|please)\b", "", topic_str).strip()
            data["intent"] = "research_topic"
            data["entities"]["topic"] = topic_str
            data["entities"]["depth"] = "deep" if is_deep else "quick"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match search web
        elif re.search(r"\b(search|google|find online|look up)\b", query_lower) and not any(k in query_lower for k in ("codebase", "code", "file", "project", "youtube", "video", "job", "schedule")):
            search_match = re.search(r"\b(search|google|find online|look up)\s+(?:for\s+)?([a-zA-Z0-9_\-\s]+)", query_lower)
            topic = search_match.group(2).strip() if search_match else "unknown"
            data["intent"] = "search_web"
            data["entities"]["topic"] = topic
            data["entities"]["query"] = topic
            data["confidence"] = 0.9
            data["suggested_route"] = "tool"

        # Match accept recommendation
        elif (re.search(r"\b(accept|do|agree to)\s+(?:the\s+)?(?:recommendation|suggestion|rec|option)\b", query_lower) or
              re.search(r"\baccept\s+([0-9]|first|second|third|#1|#2|#3)\b", query_lower) or
              re.search(r"\b(i'll do that|let's do that|i will do that)\b", query_lower)):
            idx_match = re.search(r"\b(?:recommendation|suggestion|rec|option|accept)\s+([0-9]|first|second|third|#1|#2|#3|rec_[a-zA-Z0-9_]+)\b", query_lower)
            rec_target = idx_match.group(1).strip() if idx_match else "1"
            data["intent"] = "accept_recommendation"
            data["entities"]["rec_id"] = rec_target
            data["entities"]["recommendation_id"] = rec_target
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match reject recommendation
        elif (re.search(r"\b(reject|dismiss|skip|ignore|pass on)\s+(?:the\s+)?(?:recommendation|suggestion|rec|option)\b", query_lower) or
              re.search(r"\breject\s+([0-9]|first|second|third|#1|#2|#3)\b", query_lower) or
              re.search(r"\b(dismiss recommendation|not now|skip recommendation)\b", query_lower)):
            idx_match = re.search(r"\b(?:recommendation|suggestion|rec|option|reject|dismiss)\s+([0-9]|first|second|third|#1|#2|#3|rec_[a-zA-Z0-9_]+)\b", query_lower)
            rec_target = idx_match.group(1).strip() if idx_match else "1"
            data["intent"] = "reject_recommendation"
            data["entities"]["rec_id"] = rec_target
            data["entities"]["recommendation_id"] = rec_target
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match tune recommendation weights
        elif re.search(r"\b(tune recommendation weights|tune weights|update recommendation weights|retune weights|prioritize learning|prioritize goals|focus on projects|focus on goals)\b", query_lower):
            data["intent"] = "tune_recommendation_weights"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match memory history / lineage
        elif (re.search(r"\b(memory history|history of memory|memory lineage)\b", query_lower) or
              re.search(r"\bwhat was my (previous|prior|earlier|old)\b", query_lower) or
              re.search(r"\bshow history for\b", query_lower)):
            hist_match = re.search(r"\b(?:memory history for|history of memory|memory history|what was my (?:previous|prior|earlier|old)|show history for)\s+([a-zA-Z0-9_\-\s]+)", query_lower)
            topic = hist_match.group(1).strip() if hist_match else query
            data["intent"] = "memory_history"
            data["entities"]["topic"] = topic
            data["entities"]["query"] = topic
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match memory pruning / cleanup
        elif re.search(r"\b(prune memories|prune memory|clean up memories|clean up memory|clean memories)\b", query_lower):
            is_dry = bool(re.search(r"\b(dry run|dry-run|test|preview)\b", query_lower))
            data["intent"] = "prune_memories"
            data["entities"]["dry_run"] = is_dry
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match project-specific memory store (Section 43)
        elif re.search(r"\b(remember for project|remember for this project|store project memory|save project memory|project note for)\b", query_lower):
            p_match = re.search(r"\bremember for project\s+([a-zA-Z0-9_\-]+)\s+(?:that|:)?\s*(.+)", query, re.IGNORECASE)
            if p_match:
                p_id = p_match.group(1).strip()
                fact = p_match.group(2).strip()
            else:
                p_match2 = re.search(r"\bremember for this project\s+(?:that|:)?\s*(.+)", query, re.IGNORECASE)
                if p_match2:
                    p_id = "current"
                    fact = p_match2.group(1).strip()
                else:
                    fact = query
                    p_id = "current"
            data["intent"] = "store_project_memory"
            data["entities"]["project_id"] = p_id
            data["entities"]["content"] = fact
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match project-specific memory query / listing (Section 43)
        elif (re.search(r"\b(project memories|memories for project|project notes for|what do you know about project)\b", query_lower) or
              re.search(r"\b(show|list)\s+(?:all\s+)?project memories\b", query_lower)):
            p_match = re.search(r"\b(?:project memories for|memories for project|project notes for|what do you know about project)\s+(?:project\s+)?([a-zA-Z0-9_\-]+)", query_lower)
            p_id = p_match.group(1).strip() if p_match else "current"
            data["intent"] = "list_project_memories"
            data["entities"]["project_id"] = p_id
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"


        # Match explicit store memory
        elif re.search(r"\b(remember that|store memory|save memory|save note|note down that|note down|yaad rakhna|yaad rakh)\b", query_lower):
            mem_match = re.search(r"\b(?:remember that|store memory|save memory|save note|note down that|note down|yaad rakhna|yaad rakh)\s+(.+)", query, re.IGNORECASE)
            content = mem_match.group(1).strip() if mem_match else query
            data["intent"] = "store_memory"
            data["entities"]["content"] = content
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match explicit query memory
        elif re.search(r"\b(query memory|search memory|find memory)\b", query_lower):
            q_match = re.search(r"\b(?:query memory|search memory|find memory)\s+(?:for\s+)?(.+)", query, re.IGNORECASE)
            q_term = q_match.group(1).strip() if q_match else query
            data["intent"] = "query_memory"
            data["entities"]["query"] = q_term
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match recommendations
        elif re.search(r"\b(what should i do|what next|suggest|recommend|next action)\b", query_lower):
            data["intent"] = "recommend_next_action"
            data["confidence"] = 0.9
            data["suggested_route"] = "memory"

        # Match memory / goals queries
        elif re.search(r"\b(remember|recall|memory|retrieve|what is my|what are my|what was my|what did i|do you remember|preferences)\b", query_lower):
            data["intent"] = "memory_query"
            data["entities"]["topic"] = query
            data["confidence"] = 0.85
            data["suggested_route"] = "memory"

        # Match write file
        elif re.search(r"\b(write to|create file|write file|write)\b", query_lower):
            write_match = re.search(r"\b(write to|create file|write file|write)\s+([a-zA-Z0-9_\-\s\./\\]+)", query_lower)
            file_ref = write_match.group(2).strip() if write_match else "unknown"
            data["intent"] = "write_file"
            data["entities"]["file_ref"] = file_ref
            data["confidence"] = 0.9
            data["suggested_route"] = "tool"

        # Match read file
        elif re.search(r"\b(read file|read|view file|show file)\b", query_lower):
            read_match = re.search(r"\b(read file|read|view file|show file)\s+([a-zA-Z0-9_\-\s\./\\]+)", query_lower)
            file_ref = read_match.group(2).strip() if read_match else "unknown"
            data["intent"] = "read_file"
            data["entities"]["file_ref"] = file_ref
            data["confidence"] = 0.9
            data["suggested_route"] = "tool"

        # Match summarize
        elif re.search(r"\b(summarize|summary of)\b", query_lower):
            sum_match = re.search(r"\b(summarize|summary of)\s+([a-zA-Z0-9_\-\s\./\\]+)", query_lower)
            file_ref = sum_match.group(2).strip() if sum_match else "unknown"
            data["intent"] = "summarize"
            data["entities"]["file_ref"] = file_ref
            data["confidence"] = 0.85
            data["suggested_route"] = "local_model"

        # Match coding help
        elif re.search(r"\b(how to|code|implement|write a|explain this code)\b", query_lower):
            data["intent"] = "coding_help"
            data["entities"]["topic"] = query
            data["confidence"] = 0.8
            data["suggested_route"] = "local_model"
            
        # Match research topic
        elif re.search(r"\b(research|deep research on)\b", query_lower):
            res_match = re.search(r"\b(research|deep research on)\s+([a-zA-Z0-9_\-\s]+)", query_lower)
            topic = res_match.group(2).strip() if res_match else "unknown"
            data["intent"] = "research"
            data["entities"]["topic"] = topic
            data["confidence"] = 0.8
            data["suggested_route"] = "cloud"

        # Match growth dashboard / learning progress
        elif re.search(r"\b(growth dashboard|learning dashboard|my growth|my learning|study progress|skill progress|learning progress|show dashboard)\b", query_lower):
            data["intent"] = "growth_dashboard"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"

        # Match save workspace / snapshot
        elif re.search(r"\b(save workspace|save my workspace|snapshot workspace|snapshot project|save session)\b", query_lower):
            data["intent"] = "save_workspace"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"
            snap_match = re.search(r"\b(?:save|snapshot)\s+(?:my\s+)?(?:workspace|project|session)(?:\s+(?:for|on))?\s*([a-zA-Z0-9_\-]+)?", query_lower)
            if snap_match and snap_match.group(1):
                data["entities"]["project_name"] = snap_match.group(1).strip()

        # Match continue working / resume
        elif re.search(r"\b(continue working|continue work|resume work|resume project|resume workspace|continue on|resume|pick up where i left off|pick up where we left off)\b", query_lower):
            data["intent"] = "continue_working"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"
            cont_match = re.search(r"\b(?:continue working on|continue work on|resume project|continue on|pick up where i left off on|pick up where we left off on|resume)\s+([a-zA-Z0-9_\-]+)", query_lower)
            if cont_match and cont_match.group(1):
                p_cand = cont_match.group(1).strip()
                if p_cand not in ("workspace", "work", "session"):
                    data["entities"]["project_name"] = p_cand


        # Match create tool / tool lifecycle
        elif re.search(r"\b(create tool|create a tool|draft tool|new tool|tool lifecycle|capability gaps)\b", query_lower):
            data["intent"] = "create_tool"
            data["confidence"] = 0.9
            data["suggested_route"] = "tool_lifecycle"

        # Match multi-step tasks
        elif re.search(r"\b(create a new|scaffold|build a workflow for|setup project|create project|plan workflow|plan task)\b", query_lower):
            data["intent"] = "multi_step_task"
            data["confidence"] = 0.85
            data["suggested_route"] = "planner"
            data["entities"]["goal"] = query
            scaffold_m = re.search(r"\b(?:scaffold|create|setup|new)\s+project\s+([a-zA-Z0-9_\-]+)", query, re.IGNORECASE)
            if scaffold_m:
                data["entities"]["workflow"] = "scaffold_project"
                data["entities"]["project_name"] = scaffold_m.group(1).strip()

        # Match system telemetry / hardware
        elif any(k in query_lower for k in ("system telemetry", "battery", "ram usage", "cpu usage", "system resources", "hardware metrics")):
            data["intent"] = "get_system_telemetry"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"
            data["entities"]["check_safety"] = "safe" in query_lower or "check" in query_lower

        # Match send notification
        elif any(k in query_lower for k in ("send notification", "notify me", "desktop alert", "push notification", "toast notification")):
            data["intent"] = "send_notification"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"
            msg = query
            for prefix in ("send notification", "notify me", "desktop alert", "push notification", "toast notification"):
                if prefix in query_lower:
                    idx = query_lower.find(prefix)
                    msg = query[idx + len(prefix):].strip(": ")
                    break
            data["entities"]["message"] = msg or "Notification from Jarvis"
            data["entities"]["title"] = "Jarvis Notification"

        # Match git diff
        elif any(k in query_lower for k in ("git diff", "git diff summary", "show changes", "what did i change")):
            data["intent"] = "git_diff_summary"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"
            data["entities"]["repo_path"] = "."
            data["entities"]["staged_only"] = "staged" in query_lower

        # Match git commit
        elif any(k in query_lower for k in ("git commit", "commit changes", "commit my changes")):
            data["intent"] = "git_commit"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"
            data["entities"]["repo_path"] = "."
            c_msg = "Update"
            if "with message" in query_lower:
                c_msg = query.split("with message")[-1].strip(" '\"")
            elif "message" in query_lower:
                c_msg = query.split("message")[-1].strip(" '\"")
            data["entities"]["message"] = c_msg

        # Match git status
        elif any(k in query_lower for k in ("git status", "check git", "branch status", "uncommitted changes", "uncommitted files", "uncommitted", "git changes", "is git dirty")):
            data["intent"] = "git_status"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"
            data["entities"]["repo_path"] = "."

        # Match index codebase / project
        elif any(k in query_lower for k in ("index codebase", "index project", "index documentation", "index files")):
            data["intent"] = "index_codebase"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"
            data["entities"]["folder_path"] = "."

        # Match search codebase / semantic code search
        elif any(k in query_lower for k in ("search codebase", "find in code", "semantic code search", "search project files", "query codebase")):
            data["intent"] = "search_codebase"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"
            q_code = query
            for prefix in ("search codebase for", "search codebase", "find in code", "semantic code search", "query codebase"):
                if prefix in query_lower:
                    idx = query_lower.find(prefix)
                    q_code = query[idx + len(prefix):].strip(": ")
                    break
            data["entities"]["query"] = q_code

        # Match delete file
        elif any(k in query_lower for k in ("delete file", "remove file", "trash file")):
            data["intent"] = "delete_file"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"
            f_target = query
            for prefix in ("delete file", "remove file", "trash file"):
                if prefix in query_lower:
                    idx = query_lower.find(prefix)
                    f_target = query[idx + len(prefix):].strip(": '\"")
                    break
            data["entities"]["file_path"] = f_target

        # Match restore quarantined file
        elif any(k in query_lower for k in ("restore file", "restore quarantined file", "restore backup", "undo delete")):
            data["intent"] = "restore_quarantined_file"
            data["confidence"] = 0.95
            data["suggested_route"] = "tool"
            b_target = query
            for prefix in ("restore file", "restore quarantined file", "restore backup", "undo delete"):
                if prefix in query_lower:
                    idx = query_lower.find(prefix)
                    b_target = query[idx + len(prefix):].strip(": '\"")
                    break
            data["entities"]["backup_id"] = b_target

        return normalize_interpretation(data, self.provider_name, "Parsed using local regex pattern match rules.")

class LlamaCppInterpreter(LocalInterpreter):
    def __init__(self, url=LLAMACPP_URL):
        self.provider_name = "llama_cpp"
        self.url = url

    async def interpret(self, query: str, context: dict = None) -> dict:
        payload = {
            "model": "qwen",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": query}
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0
        }
        
        try:
            req = urllib.request.Request(
                f"{self.url}/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            # Fetch response synchronously inside async method via urlopen
            # (We use a short timeout so we degrade quickly to fallback if the server blocks)
            with urllib.request.urlopen(req, timeout=3.0) as response:
                if response.status == 200:
                    resp_data = json.loads(response.read().decode("utf-8"))
                    content_str = resp_data["choices"][0]["message"]["content"]
                    data = json.loads(content_str)
                    return normalize_interpretation(data, self.provider_name, f"Processed by llama.cpp chat completion. Prompt: '{query}'")
        except Exception as e:
            logger.warning(f"llama.cpp OpenAI-compatible API call failed: {e}. Trying direct completion.")
            
        # Fallback to direct completion `/completion` endpoint if OpenAI-compatible API is not loaded
        payload_direct = {
            "prompt": f"<system>{SYSTEM_PROMPT}</system>\n<user>{query}</user>\n<assistant>",
            "temperature": 0.0,
            "json_schema": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string"},
                    "confidence": {"type": "number"},
                    "needs_clarification": {"type": "boolean"},
                    "clarification_question": {"type": ["string", "null"]},
                    "suggested_route": {"type": "string"},
                    "entities": {
                        "type": "object",
                        "properties": {
                            "project_name": {"type": ["string", "null"]},
                            "app_name": {"type": ["string", "null"]},
                            "file_ref": {"type": ["string", "null"]},
                            "topic": {"type": ["string", "null"]},
                            "time_ref": {"type": ["string", "null"]}
                        }
                    }
                }
            }
        }
        try:
            req = urllib.request.Request(
                f"{self.url}/completion",
                data=json.dumps(payload_direct).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=3.0) as response:
                if response.status == 200:
                    resp_data = json.loads(response.read().decode("utf-8"))
                    content_str = resp_data["content"]
                    data = json.loads(content_str)
                    return normalize_interpretation(data, self.provider_name, "Processed by llama.cpp direct completion.")
        except Exception as e:
            logger.error(f"llama.cpp direct completion API call failed: {e}")
            
        # Hard fallback to RuleBased
        logger.warning("LlamaCppInterpreter failed to parse query. Falling back to RuleBased.")
        return await RuleBasedInterpreter().interpret(query, context)

class OllamaInterpreter(LocalInterpreter):
    def __init__(self, url=OLLAMA_URL, model=None, fallback_model=None):
        self.provider_name = "ollama"
        self.url = url
        self.model = model or getattr(config, "OLLAMA_CLOUD_MODEL", getattr(config, "INTERPRETER_MODEL", "qwen2.5:0.5b"))
        self.fallback_model = fallback_model or getattr(config, "OLLAMA_LOCAL_MODEL", "qwen2.5:0.5b")

    def _sync_query(self, target_model: str, query: str) -> dict:
        payload = {
            "model": target_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": query}
            ],
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.0
            }
        }
        req = urllib.request.Request(
            f"{self.url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=2.5) as response:
            if response.status == 200:
                resp_data = json.loads(response.read().decode("utf-8"))
                content_str = resp_data["message"]["content"]
                data = json.loads(content_str)
                return normalize_interpretation(data, self.provider_name, f"Processed by Ollama model '{target_model}'.")
            raise RuntimeError(f"Ollama returned HTTP {response.status}")

    async def interpret(self, query: str, context: dict = None) -> dict:
        import asyncio
        # 1. Try primary model (e.g. gemma4:31b-cloud)
        try:
            return await asyncio.to_thread(self._sync_query, self.model, query)
        except Exception as e:
            logger.warning(f"OllamaInterpreter primary model '{self.model}' failed: {e}")

        # 2. Try fallback local model (e.g. qwen2.5:0.5b)
        if self.model != self.fallback_model:
            try:
                logger.info(f"OllamaInterpreter cascading to local micro-model '{self.fallback_model}'...")
                return await asyncio.to_thread(self._sync_query, self.fallback_model, query)
            except Exception as e2:
                logger.warning(f"OllamaInterpreter fallback model '{self.fallback_model}' failed: {e2}")

        # 3. Hard fallback to RuleBased
        logger.warning("OllamaInterpreter all models failed. Falling back to RuleBased.")
        return await RuleBasedInterpreter().interpret(query, context)

class AutoInterpreter(LocalInterpreter):
    def __init__(self):
        self.provider_name = "auto"

    async def _resolve_interpreter(self) -> LocalInterpreter:
        # Check llama.cpp first
        try:
            req = urllib.request.Request(f"{LLAMACPP_URL}/health", method="GET")
            with urllib.request.urlopen(req, timeout=0.5) as resp:
                if resp.status == 200:
                    logger.info("Auto-selector: Resolved to LlamaCppInterpreter")
                    return LlamaCppInterpreter()
        except Exception:
            pass

        # Check Ollama next
        try:
            req = urllib.request.Request(f"{OLLAMA_URL}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=0.5) as resp:
                if resp.status == 200:
                    logger.info("Auto-selector: Resolved to OllamaInterpreter")
                    return OllamaInterpreter()
        except Exception:
            pass

        # Fallback to RuleBased
        logger.info("Auto-selector: Resolved to RuleBasedInterpreter (fallback)")
        return RuleBasedInterpreter()

    async def interpret(self, query: str, context: dict = None) -> dict:
        # Fast path (< 1ms): Direct desktop intents & context-first resolutions
        rule_engine = RuleBasedInterpreter()
        fast_res = await rule_engine.interpret(query, context)
        if fast_res.get("confidence", 0.0) >= 0.80 or fast_res.get("intent") in (
            "play_media", "multi_task", "take_screenshot", "show_desktop", "open_app", "control_media", "control_volume"
        ):
            return fast_res

        interpreter = await self._resolve_interpreter()
        return await interpreter.interpret(query, context)

def get_interpreter() -> LocalInterpreter:
    """
    Factory function returning the configured interpreter.
    """
    if INTERPRETER_TYPE == "llamacpp":
        return LlamaCppInterpreter()
    elif INTERPRETER_TYPE == "ollama":
        return OllamaInterpreter()
    elif INTERPRETER_TYPE == "rule_based":
        return RuleBasedInterpreter()
    else:  # "auto" or other
        return AutoInterpreter()
