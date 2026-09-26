"""
Context Engine Subsystem for Kate Personal Assistant.
Implements Context-First Intent Intelligence:
  - Tracks conversational flow, recent turns, and mentioned entities.
  - Resolves elliptical follow-ups, anaphora, and context continuations.
  - Persists context in memory and SQLite.
Following Workspace Design Rules: Abstractions & Interfaces First.
"""

import abc
import time
import json
import logging
import re
from typing import Any

logger = logging.getLogger("kate.context")


# =====================================================================
# Subsystem Abstraction (Abstractions & Interfaces First)
# =====================================================================

class ContextProvider(abc.ABC):
    """Abstract interface for inspecting and updating conversational & system context."""

    @abc.abstractmethod
    def get_context(self) -> dict:
        """Returns snapshot of current conversational, entity, and system context."""
        pass

    @abc.abstractmethod
    def resolve_context_anaphora(self, query: str) -> dict | None:
        """
        Attempts to resolve an elliptical query, follow-up, or pronoun ('it', 'that', 'i wanna be yours')
        using recent turns and active entities. Returns normalized interpretation dict or None.
        """
        pass

    @abc.abstractmethod
    def record_turn(self, user_query: str, intent_data: dict, assistant_response: str, action_taken: str):
        """Records a completed turn in conversational memory."""
        pass


# =====================================================================
# Concrete Implementation: SQLite & In-Memory Sliding Context Engine
# =====================================================================

class ContextEngine(ContextProvider):
    """
    Maintains short-term conversational context and entity memory for Kate.
    Stores sliding history of turns and tracks active entities (songs, apps, browsers, URLs).
    """

    def __init__(self, db_manager=None, max_turns: int = 10):
        self.db = db_manager
        self.max_turns = max_turns
        self.recent_turns: list[dict] = []
        
        # Tracked active entities
        self.active_entities: dict[str, Any] = {
            "last_media_title": None,
            "last_media_platform": "youtube",
            "last_browser": "brave",
            "last_opened_app": None,
            "last_project": None,
            "last_file": None,
            "pending_topic": None
        }

        self._load_from_db()

    def attach_db(self, db_manager):
        """Attaches a DatabaseManager instance and loads persisted history."""
        self.db = db_manager
        self._load_from_db()
        logger.info(f"ContextEngine: Attached DB and restored {len(self.recent_turns)} turns.")

    def _load_from_db(self):
        """Restores recent state from SQLite if available."""
        if not self.db:
            return
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key, value FROM active_context WHERE key LIKE 'kate_ctx_%';")
                rows = cursor.fetchall()
                for r in rows:
                    k = r["key"].replace("kate_ctx_", "")
                    try:
                        val = json.loads(r["value"])
                        if k == "recent_turns" and isinstance(val, list):
                            self.recent_turns = val[-self.max_turns:]
                        elif k in self.active_entities:
                            self.active_entities[k] = val
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"ContextEngine: Initial DB load non-critical error: {e}")

    def _persist_to_db(self):
        """Saves current state snapshot to SQLite."""
        if not self.db:
            return
        try:
            with self.db.transaction() as conn:
                now = int(time.time())
                # Save recent turns
                conn.execute(
                    "INSERT INTO active_context (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;",
                    ("kate_ctx_recent_turns", json.dumps(self.recent_turns), now)
                )
                # Save key entities
                for k, v in self.active_entities.items():
                    conn.execute(
                        "INSERT INTO active_context (key, value, updated_at) VALUES (?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;",
                        (f"kate_ctx_{k}", json.dumps(v), now)
                    )
        except Exception as e:
            logger.debug(f"ContextEngine: Persist non-critical error: {e}")

    def get_context(self) -> dict:
        """Returns full snapshot for intent classification."""
        return {
            "recent_turns": self.recent_turns,
            "active_entities": self.active_entities,
            "turn_count": len(self.recent_turns),
            "last_turn": self.recent_turns[-1] if self.recent_turns else None
        }

    def get_dialogue_summary(self, max_turns: int = 3) -> str:
        """Formats the last N dialogue turns into a compact prompt string for LLM awareness."""
        if not self.recent_turns:
            return "No previous conversation."
        
        lines = []
        for t in self.recent_turns[-max_turns:]:
            q = t.get("query", "")
            r = t.get("response", "")
            lines.append(f"User: {q}")
            lines.append(f"Kate: {r}")
        return "\n".join(lines)

    def resolve_context_anaphora(self, query: str) -> dict | None:
        """
        Deep context-first resolution:
        If user says something that depends on previous context (e.g. song name, 'play it', 'in brave'),
        resolve the full goal and intent rather than treating it as an isolated utterance.
        """
        q_clean = query.strip()
        q_lower = q_clean.lower()
        if not self.recent_turns:
            return None

        last_turn = self.recent_turns[-1]
        last_intent = last_turn.get("intent", "")
        last_query = last_turn.get("query", "").lower()
        last_response = last_turn.get("response", "").lower()

        # -------------------------------------------------------------
        # Scenario 1: Follow-up Song Title after Media/YouTube Discussion
        # e.g. Turn 1 was about playing music or opening YouTube in Brave
        # Turn 2 is a bare song name like 'i wanna be yours', 'believer', 'starboy'
        # -------------------------------------------------------------
        is_media_context = (
            last_intent in ("play_media", "search_youtube", "watch_video") or
            (last_intent == "open_app" and any(w in last_query for w in ("youtube", "spotify", "music", "song", "soundcloud", "brave"))) or
            any(phrase in last_response for phrase in ("which song", "what song", "what would you like to hear", "what song do you want", "playing on youtube"))
        )

        if is_media_context:
            # If the query contains explicit system action verbs or conjunctions, it's an independent command, not a song title
            action_verbs = ("open", "take", "screenshot", "show", "desktop", "switch", "lock", "organize", "pdf", "git", "run", "search", "browse", "list", "schedule", "delete", "kholo", "dikhao", "set", "make", "create", "turn")
            if any(v in q_lower.split() for v in action_verbs) or " and " in q_lower or " aur " in q_lower or " then " in q_lower:
                return None

            # Exclude conversational responses, negations, questions, and commands
            non_media_phrases = {
                "no", "nope", "cancel", "nevermind", "wait", "later", "don't", "dont", "yes", "yeah",
                "sure", "ok", "okay", "fine", "what", "how", "why", "when", "where", "volume",
                "mute", "unmute", "thanks", "thank you", "stop", "exit", "quit", "help", "who are you",
                "what can you do", "hi", "hello", "hey", "nothing", "nothing played", "not working"
            }
            if q_lower in non_media_phrases or re.search(r"^(who|what|how|why|when|where|can you|could you)\b", q_lower):
                return None

            # Clean filler phrases if user said 'that is song ... i wanna be yours'
            song_candidate = q_clean
            m_lead = re.search(r"(?:that is|it is|play|the song is|song name is|song|gana)?\s*(.+)", q_clean, re.IGNORECASE)
            if m_lead:
                song_candidate = m_lead.group(1).strip()
            # Remove trailing browser/youtube mentions from title
            song_candidate = re.sub(r"\s+(?:on\s+youtube|in\s+brave|on\s+brave|in\s+youtube)$", "", song_candidate, flags=re.IGNORECASE).strip()

            if len(song_candidate) >= 2 and not song_candidate.lower() in non_media_phrases:
                browser = self.active_entities.get("last_browser") or "brave"
                platform = self.active_entities.get("last_media_platform") or "youtube"

                logger.info(f"ContextEngine: Resolved follow-up song title '{song_candidate}' in media context.")
                return {
                    "intent": "play_media",
                    "entities": {
                        "title": song_candidate,
                        "platform": platform,
                        "browser": browser
                    },
                    "resolved_reference": song_candidate,
                    "confidence": 0.85,
                    "suggested_route": "tool",
                    "needs_clarification": False,
                    "metadata": {
                        "provider": "context_anaphora",
                        "reasoning": f"Resolved song '{song_candidate}' from prior media turn: '{last_query}'"
                    }
                }

        # -------------------------------------------------------------
        # Scenario 2: Pronoun 'play it' / 'open it' / 'do it'
        # -------------------------------------------------------------
        if re.search(r"\b(play it|chala do|play this|open it|kholo isse)\b", q_lower):
            last_song = self.active_entities.get("last_media_title")
            last_app = self.active_entities.get("last_opened_app")

            if "play" in q_lower or "chala" in q_lower:
                if last_song:
                    return {
                        "intent": "play_media",
                        "entities": {
                            "title": last_song,
                            "platform": self.active_entities.get("last_media_platform", "youtube"),
                            "browser": self.active_entities.get("last_browser", "brave")
                        },
                        "resolved_reference": last_song,
                        "confidence": 0.98,
                        "suggested_route": "tool",
                        "needs_clarification": False
                    }
            elif "open" in q_lower or "kholo" in q_lower:
                if last_app:
                    return {
                        "intent": "open_app",
                        "entities": {"app_name": last_app},
                        "resolved_reference": last_app,
                        "confidence": 0.98,
                        "suggested_route": "tool",
                        "needs_clarification": False
                    }

        # -------------------------------------------------------------
        # Scenario 3: Browser specification follow-up ('in brave', 'on brave')
        # -------------------------------------------------------------
        if re.search(r"^\s*(?:in|on)\s+(brave|chrome|firefox|edge)\s*$", q_lower):
            m_b = re.search(r"(brave|chrome|firefox|edge)", q_lower)
            browser_choice = m_b.group(1) if m_b else "brave"
            last_song = self.active_entities.get("last_media_title")
            if last_song:
                return {
                    "intent": "play_media",
                    "entities": {
                        "title": last_song,
                        "platform": "youtube",
                        "browser": browser_choice
                    },
                    "resolved_reference": last_song,
                    "confidence": 0.98,
                    "suggested_route": "tool",
                    "needs_clarification": False
                }

        return None

    def record_turn(self, user_query: str, intent_data: dict, assistant_response: str, action_taken: str):
        """Appends turn and updates tracked entities."""
        intent = intent_data.get("intent", "conversation") if isinstance(intent_data, dict) else "conversation"
        entities = intent_data.get("entities", {}) if isinstance(intent_data, dict) else {}

        # Update tracked active entities
        if intent == "play_media":
            title = entities.get("title")
            if title:
                self.active_entities["last_media_title"] = title
            self.active_entities["last_media_platform"] = entities.get("platform", "youtube")
            if entities.get("browser"):
                self.active_entities["last_browser"] = entities.get("browser")

        elif intent == "open_app":
            app_name = entities.get("app_name") or intent_data.get("resolved_reference")
            if app_name:
                self.active_entities["last_opened_app"] = app_name

        turn_record = {
            "query": user_query,
            "intent": intent,
            "entities": entities,
            "response": assistant_response,
            "action": action_taken,
            "timestamp": time.time()
        }

        self.recent_turns.append(turn_record)
        if len(self.recent_turns) > self.max_turns:
            self.recent_turns.pop(0)

        self._persist_to_db()
        logger.info(f"ContextEngine: Turn recorded. Total history turns: {len(self.recent_turns)}")


# Global singleton instance
context_engine = ContextEngine()
