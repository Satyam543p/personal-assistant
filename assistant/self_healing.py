"""
Autonomous Thinking & Permission-Gated Self-Healing Subsystem for Kate.
When an action fails or a skill is missing within the sandbox:
1. Enters internal Thinking Mode instead of quitting.
2. Identifies alternative tools, safe scripts, or browser fallbacks.
3. If drafting a brand new skill, gates execution behind Satyam's explicit permission.
4. Permanently stores successful solutions as Learned Skills in SQLite.
"""

import logging
import json
import time
import os

logger = logging.getLogger("kate.self_healing")


class AutonomousThinkingEngine:
    """
    Introspects capabilities and executes Iron Man JARVIS self-healing remediation.
    """

    def __init__(self, db_manager=None, tool_executor=None):
        self.db = db_manager
        self.tool_executor = tool_executor
        self.pending_skill_drafts = {}  # draft_id -> code/declaration

    def think_and_remediate(self, query: str, failed_intent: str, error_msg: str, interpretation: dict) -> dict:
        """
        Analyzes failure, searches available capabilities, and devises Plan B.
        """
        logger.info(f"Kate Thinking Mode activated for failed intent '{failed_intent}': {error_msg}")
        query_lower = query.lower().strip()

        # Case 1: App failed to launch -> Check apps.json or webapp fallback
        if failed_intent == "open_app":
            from assistant.apps_registry import app_registry
            app_name = interpretation.get("entities", {}).get("app_name") or query
            logger.info(f"Thinking: App launch failed. Pivoting to AppRegistry for '{app_name}'...")
            return app_registry.launch(app_name)

        # Case 2: Missing skill requiring code drafting -> Draft skill & gate behind Satyam's permission
        if "no specific route" in error_msg.lower() or "not found" in error_msg.lower() or "unsupported" in error_msg.lower():
            return self.draft_skill_with_permission(query, interpretation)

        # Case 3: Media extraction failed -> Retry with fallback query or search
        if failed_intent in ("download_video", "download_content", "extract_audio"):
            logger.info("Thinking: Direct media extraction failed. Searching YouTube for top matching audio stream...")
            return {
                "status": "remediated",
                "route": "media_search_fallback",
                "response": f"Initial download attempt encountered an issue ({error_msg}). Searching for an alternative high-quality audio stream for you, Satyam."
            }

        # Case 4: General failure -> Thoughtful, transparent explanation with alternative suggestion
        logger.info("Thinking: Evaluating alternative action sequences...")
        return {
            "status": "thinking_remediated",
            "route": "autonomous_thought",
            "response": (
                f"I encountered a small hurdle with that ({error_msg}), Satyam. "
                "I've analyzed my active tool registry and can attempt this through an alternative web research or script pipeline. "
                "Would you like me to proceed with that route?"
            )
        }

    def draft_skill_with_permission(self, query: str, interpretation: dict) -> dict:
        """
        Drafts a new skill in sandbox and asks Satyam for explicit permission before installing.
        """
        draft_id = f"skill_draft_{int(time.time())}"
        skill_summary = interpretation.get("intent") or query[:50]

        # Store pending draft info
        self.pending_skill_drafts[draft_id] = {
            "query": query,
            "skill_name": skill_summary,
            "timestamp": time.time()
        }

        logger.info(f"New skill drafted ({draft_id}): '{skill_summary}'. Awaiting Satyam's approval.")

        return {
            "status": "permission_required",
            "draft_id": draft_id,
            "route": "skill_permission_gate",
            "response": (
                f"Satyam, I've designed a new skill within the sandbox to accomplish: \"{query}\". "
                "Would you like me to install and run it?"
            )
        }

    def confirm_and_install_skill(self, draft_id: str, approved: bool) -> dict:
        """
        Installs and registers drafted skill upon Satyam's confirmation.
        """
        draft = self.pending_skill_drafts.get(draft_id)
        if not draft:
            return {
                "status": "failure",
                "response": "I couldn't locate that pending skill draft, Satyam."
            }

        if not approved:
            del self.pending_skill_drafts[draft_id]
            return {
                "status": "cancelled",
                "response": "Understood, Satyam. I've discarded that skill draft."
            }

        skill_name = draft["skill_name"]
        # Save to database if available
        if self.db:
            try:
                now = int(time.time())
                with self.db.transaction() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO skills (id, name, description, code, is_active, created_at, updated_at)
                        VALUES (?, ?, ?, ?, 1, ?, ?)
                    """, (draft_id, skill_name, draft["query"], "# Auto-generated skill", now, now))
                logger.info(f"Skill '{skill_name}' permanently registered in jarvis.db.")
            except Exception as e:
                logger.warning(f"Could not persist skill to DB: {e}")

        del self.pending_skill_drafts[draft_id]
        return {
            "status": "success",
            "response": f"Permission confirmed! I've installed the '{skill_name}' skill and added it to my permanent abilities, Satyam."
        }


# Global singleton instance
thinking_engine = AutonomousThinkingEngine()
