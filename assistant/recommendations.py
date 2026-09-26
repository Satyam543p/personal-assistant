import os
import re
import time
import uuid
import json
import math
import logging
from datetime import datetime
from abc import ABC, abstractmethod

try:
    from assistant.tools import Tool
except ModuleNotFoundError:
    from tools import Tool

logger = logging.getLogger("jarvis.recommendations")

# =====================================================================
# Subsystem Abstraction (Abstractions & Interfaces First)
# =====================================================================

class RecommendationEngine(ABC):
    @abstractmethod
    async def get_recommendations(self) -> dict:
        """Generates, scores, ranks, and logs top recommendations."""
        pass

    @abstractmethod
    async def record_feedback(self, rec_id_or_index: str | int, accepted: bool, auto_execute: bool = True) -> dict:
        """Records user acceptance or rejection of a recommendation."""
        pass

    @abstractmethod
    async def record_outcome(self, rec_id_or_index: str | int, outcome: str) -> dict:
        """Records execution outcome (completed, abandoned, neutral) for an accepted recommendation."""
        pass

    @abstractmethod
    async def tune_weights(self) -> dict:
        """Runs the self-tuning feedback loop, updating weights within bounds and saving to DB."""
        pass

    @abstractmethod
    def get_current_weights(self) -> dict:
        """Returns the current active weights dictionary."""
        pass

    @abstractmethod
    async def run_session_end_feedback_loop(self) -> dict:
        """Processes outcomes and tunes weights at session end or shutdown."""
        pass


# =====================================================================
# Concrete Implementation: JarvisRecommendationEngine
# =====================================================================

class JarvisRecommendationEngine(RecommendationEngine):
    def __init__(self, db_manager, growth_engine=None, profile_manager=None, tool_executor=None, context_store=None):
        self.db = db_manager
        self.growth_engine = growth_engine
        self.profile_manager = profile_manager
        self.tool_executor = tool_executor
        self.context_store = context_store
        self._last_recommendations = []

        # Default weights per Section 32 of phase.md
        self.default_weights = {
            "w_goal": 0.35,
            "w_habit": 0.20,
            "w_recency": 0.15,
            "w_skill_gap": 0.20,
            "w_unfinished": 0.25,
            "w_rejection": 0.30
        }

    def _get_weights(self) -> dict:
        """Loads weights from the preferences table or returns defaults."""
        weights = self.default_weights.copy()
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key, value FROM preferences WHERE key LIKE 'rec_w_%';")
                for row in cursor.fetchall():
                    key = row["key"].replace("rec_", "")
                    try:
                        weights[key] = float(row["value"])
                    except ValueError:
                        pass
        except Exception as e:
            logger.error(f"Error loading recommendation weights from DB: {e}")
        return weights

    def _save_weights(self, weights: dict):
        """Saves updated weights back to the preferences table."""
        try:
            now = int(time.time())
            with self.db.transaction() as conn:
                for key, val in weights.items():
                    conn.execute(
                        "INSERT INTO preferences (key, value, created_at, updated_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;",
                        (f"rec_{key}", str(round(val, 4)), now, now)
                    )
        except Exception as e:
            logger.error(f"Error saving recommendation weights to DB: {e}")

    def get_current_weights(self) -> dict:
        """Public getter for current active recommendation weights."""
        return self._get_weights()

    def _clamp_weight(self, key: str, value: float) -> float:
        """Clamps weight to [default * 0.5, default * 2.0] per Section 32."""
        default_val = self.default_weights.get(key, 0.2)
        min_bound = default_val * 0.5
        max_bound = default_val * 2.0
        return round(max(min_bound, min(max_bound, value)), 4)

    def _calculate_habit_match(self, conn, skill_id: str, current_hour: int) -> float:
        """Heuristic matching time-of-day history for study sessions."""
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT started_at FROM study_sessions WHERE skill_id = ?;", (skill_id,))
            sessions = cursor.fetchall()
            if not sessions:
                return 0.0

            matches = 0
            for row in sessions:
                started_at = row["started_at"]
                session_hour = datetime.fromtimestamp(started_at).hour
                if abs(session_hour - current_hour) <= 2 or abs(session_hour - current_hour) >= 22:
                    matches += 1
            return min(1.0, matches / len(sessions))
        except Exception as e:
            logger.error(f"Error calculating habit match for skill {skill_id}: {e}")
            return 0.0

    def _calculate_rejection_penalty(self, conn, candidate_id: str) -> float:
        """Determines penalty if user has recently rejected recommendations of this candidate."""
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT accepted FROM recommendations WHERE basis LIKE ? ORDER BY generated_at DESC LIMIT 5;",
                (f"%{candidate_id}%",)
            )
            rows = cursor.fetchall()
            if not rows:
                return 0.0

            penalty = 0.0
            for row in rows:
                if row["accepted"] == 0:  # Rejected
                    penalty += 0.5
            return min(1.0, penalty)
        except Exception as e:
            logger.error(f"Error calculating rejection penalty for {candidate_id}: {e}")
            return 0.0

    async def get_recommendations(self) -> dict:
        """Generates, scores, ranks, and logs top 3 recommendations."""
        now = int(time.time())
        current_hour = datetime.fromtimestamp(now).hour
        weights = self._get_weights()

        candidates = []

        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()

                # 1. Fetch active projects
                cursor.execute("SELECT id, name, last_opened_at FROM projects;")
                projects = cursor.fetchall()
                for proj in projects:
                    p_id = proj["id"]
                    p_name = proj["name"]
                    last_opened = proj["last_opened_at"]

                    recency_bonus = 0.0
                    if last_opened:
                        t_diff = now - last_opened
                        recency_bonus = math.exp(-t_diff / (7 * 86400))

                    unfinished_task_bonus = 0.5
                    rejection_penalty = self._calculate_rejection_penalty(conn, p_id)

                    score = (
                        weights["w_recency"] * recency_bonus +
                        weights["w_unfinished"] * unfinished_task_bonus -
                        weights["w_rejection"] * rejection_penalty
                    )

                    candidates.append({
                        "type": "project",
                        "id": p_id,
                        "name": p_name,
                        "text": f"Continue working on project '{p_name}'.",
                        "score": round(score, 4),
                        "basis": {
                            "candidate_id": p_id,
                            "type": "project",
                            "metrics": {
                                "recency_bonus": round(recency_bonus, 4),
                                "unfinished_task_bonus": unfinished_task_bonus,
                                "rejection_penalty": rejection_penalty
                            }
                        }
                    })

                # 2. Fetch active goals
                cursor.execute("SELECT id, title, target_date, progress_pct FROM goals WHERE status = 'active';")
                goals = cursor.fetchall()
                for goal in goals:
                    g_id = goal["id"]
                    g_title = goal["title"]
                    target_date = goal["target_date"]
                    progress = goal["progress_pct"] or 0.0

                    goal_urgency = 0.1
                    if target_date:
                        t_diff = target_date - now
                        if t_diff > 0:
                            goal_urgency = max(0.0, min(1.0, 1.0 - t_diff / (30 * 86400)))
                        else:
                            goal_urgency = 1.0

                    unfinished_task_bonus = 1.0 - (progress / 100.0)
                    rejection_penalty = self._calculate_rejection_penalty(conn, g_id)

                    score = (
                        weights["w_goal"] * goal_urgency +
                        weights["w_unfinished"] * unfinished_task_bonus -
                        weights["w_rejection"] * rejection_penalty
                    )

                    candidates.append({
                        "type": "goal",
                        "id": g_id,
                        "name": g_title,
                        "text": f"Focus on goal: '{g_title}' ({int(progress)}% complete).",
                        "score": round(score, 4),
                        "basis": {
                            "candidate_id": g_id,
                            "type": "goal",
                            "metrics": {
                                "goal_urgency": round(goal_urgency, 4),
                                "unfinished_task_bonus": round(unfinished_task_bonus, 4),
                                "rejection_penalty": rejection_penalty
                            }
                        }
                    })

                # 3. Fetch active skills
                cursor.execute("SELECT id, name, confidence FROM skills;")
                skills = cursor.fetchall()
                for skill in skills:
                    s_id = skill["id"]
                    s_name = skill["name"]
                    conf = skill["confidence"] or 0.5
                    if self.growth_engine:
                        try:
                            conf = self.growth_engine.skill_tracker.get_decayed_confidence(s_name, current_time=now)
                        except Exception:
                            pass

                    skill_gap_relevance = max(0.0, 1.0 - conf)
                    habit_match = self._calculate_habit_match(conn, s_id, current_hour)
                    rejection_penalty = self._calculate_rejection_penalty(conn, s_id)

                    score = (
                        weights["w_skill_gap"] * skill_gap_relevance +
                        weights["w_habit"] * habit_match -
                        weights["w_rejection"] * rejection_penalty
                    )

                    candidates.append({
                        "type": "skill",
                        "id": s_id,
                        "name": s_name,
                        "text": f"Practice skill '{s_name}' (current confidence: {round(conf, 2)}).",
                        "score": round(score, 4),
                        "basis": {
                            "candidate_id": s_id,
                            "type": "skill",
                            "metrics": {
                                "skill_gap_relevance": round(skill_gap_relevance, 4),
                                "habit_match": round(habit_match, 4),
                                "rejection_penalty": rejection_penalty
                            }
                        }
                    })

        except Exception as e:
            logger.error(f"Failed loading recommendation candidates: {e}")

        # 4. Apply Workspace Profile Biasing (Section 8)
        active_profile = None
        if self.profile_manager:
            try:
                active_profile = self.profile_manager.get_active_profile()
            except Exception as pe:
                logger.debug(f"Error getting active profile for recommendations: {pe}")

        if active_profile and active_profile.biased_categories:
            bias_categories = set(active_profile.biased_categories)
            for cand in candidates:
                cand_type = cand.get("type")
                if cand_type in bias_categories or (cand_type == "skill" and "study" in bias_categories):
                    cand["score"] = round(cand["score"] + 0.25, 4)
                    cand["basis"]["metrics"]["profile_bias"] = 0.25
                else:
                    cand["basis"]["metrics"]["profile_bias"] = 0.0

        # 5. Cold-start check
        if not candidates:
            return {
                "status": "success",
                "response": "Tell me one thing you're working on and I'll help you stay on track.",
                "recommendations": [],
                "route": "memory"
            }

        # Sort candidates descending by score
        candidates.sort(key=lambda x: x["score"], reverse=True)
        top_candidates = candidates[:3]

        # Save recommendations to DB
        saved_recs = []
        try:
            with self.db.transaction() as conn:
                for cand in top_candidates:
                    rec_id = f"rec_{cand['type']}_{cand['id']}_{now}_{uuid.uuid4().hex[:6]}"
                    saved_recs.append({
                        "id": rec_id,
                        "text": cand["text"],
                        "basis": cand["basis"],
                        "type": cand["type"],
                        "candidate_id": cand["id"]
                    })
                    conn.execute(
                        "INSERT INTO recommendations (id, recommendation_text, basis, accepted, generated_at, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?);",
                        (
                            rec_id,
                            cand["text"],
                            json.dumps(cand["basis"]),
                            None,
                            now,
                            now,
                            now
                        )
                    )
        except Exception as e:
            logger.error(f"Error persisting recommendation history logs: {e}")
            if not saved_recs:
                saved_recs = [
                    {
                        "id": f"rec_{c['type']}_{c['id']}_{now}_{uuid.uuid4().hex[:6]}",
                        "text": c["text"],
                        "basis": c["basis"],
                        "type": c["type"],
                        "candidate_id": c["id"]
                    }
                    for c in top_candidates
                ]

        self._last_recommendations = saved_recs
        if self.context_store:
            try:
                self.context_store.set("last_recommendations", json.dumps(saved_recs))
            except Exception as ce:
                logger.debug(f"Could not store last_recommendations in active_context: {ce}")

        # Format response string
        bullets = []
        for idx, cand in enumerate(top_candidates, 1):
            bullets.append(f"{idx}. {cand['text']} (Score: {cand['score']})")

        response_text = "Here are my recommendations:\n" + "\n".join(bullets)

        return {
            "status": "success",
            "response": response_text,
            "recommendations": saved_recs,
            "route": "memory"
        }

    def _resolve_recommendation(self, rec_id_or_index: str | int | None) -> tuple[str | None, dict | None]:
        """
        Resolves a recommendation identifier (ID string, positional index 1/2/3,
        or ordinal words 'first', 'second', 'third') to (rec_id, rec_dict).
        """
        raw_val = str(rec_id_or_index or "").strip()
        index_target = None

        # Check for numeric or ordinal indices
        if raw_val.isdigit():
            index_target = int(raw_val) - 1
        elif raw_val.lower() in ("#1", "1st", "first", "the first", "one"):
            index_target = 0
        elif raw_val.lower() in ("#2", "2nd", "second", "the second", "two"):
            index_target = 1
        elif raw_val.lower() in ("#3", "3rd", "third", "the third", "three"):
            index_target = 2
        elif not raw_val or raw_val.lower() in ("default", "latest", "top", "it", "that", "this"):
            index_target = 0

        # Try to resolve from cached last recommendations
        recent_list = list(self._last_recommendations)
        if not recent_list and self.context_store:
            try:
                raw_ctx = self.context_store.get("last_recommendations")
                if raw_ctx:
                    recent_list = json.loads(raw_ctx)
            except Exception:
                pass

        # If still empty, query DB for latest generated recommendations
        if not recent_list:
            try:
                with self.db.transaction() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT * FROM recommendations ORDER BY generated_at DESC LIMIT 3;")
                    rows = cursor.fetchall()
                    recent_list = [dict(r) for r in rows]
            except Exception as e:
                logger.error(f"Error fetching latest recommendations: {e}")

        # If resolving by index
        if index_target is not None:
            if 0 <= index_target < len(recent_list):
                target_rec = recent_list[index_target]
                return target_rec["id"], target_rec

        # Otherwise search by ID or prefix match
        if raw_val:
            for rec in recent_list:
                if rec.get("id") == raw_val or raw_val in rec.get("id", ""):
                    return rec["id"], rec

            # Search directly in DB
            try:
                with self.db.transaction() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT * FROM recommendations WHERE id = ? OR id LIKE ? LIMIT 1;", (raw_val, f"%{raw_val}%"))
                    row = cursor.fetchone()
                    if row:
                        return row["id"], dict(row)
            except Exception as e:
                logger.error(f"Error querying recommendation by ID '{raw_val}': {e}")

        # Fallback to the first available if any exist
        if recent_list:
            return recent_list[0]["id"], recent_list[0]

        return None, None

    async def record_feedback(self, rec_id_or_index: str | int | None, accepted: bool, auto_execute: bool = True) -> dict:
        """
        Logs user acceptance/rejection response to a generated recommendation.
        If accepted and auto_execute is True, invokes the corresponding tool.
        """
        now = int(time.time())
        rec_id, rec_data = self._resolve_recommendation(rec_id_or_index)
        if not rec_id:
            return {
                "status": "failure",
                "message": f"Could not find recommendation matching '{rec_id_or_index}'.",
                "recommendation_id": None
            }

        rec_text = rec_data.get("recommendation_text") or rec_data.get("text") or "Recommendation"
        basis = rec_data.get("basis")
        if isinstance(basis, str):
            try:
                basis = json.loads(basis)
            except Exception:
                basis = {}
        elif not isinstance(basis, dict):
            basis = {}

        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE recommendations SET accepted = ?, responded_at = ?, updated_at = ? WHERE id = ?;",
                    (1 if accepted else 0, now, now, rec_id)
                )
            logger.info(f"Recorded recommendation feedback: {rec_id} -> {'ACCEPTED' if accepted else 'REJECTED'}")
        except Exception as e:
            logger.error(f"Error logging recommendation feedback: {e}")
            return {
                "status": "failure",
                "message": f"Database error recording feedback: {e}",
                "recommendation_id": rec_id
            }

        action_result = None
        executed_action = None

        if accepted:
            cand_type = basis.get("type")
            cand_id = basis.get("candidate_id")

            if auto_execute and self.tool_executor:
                try:
                    if cand_type == "project" and cand_id:
                        executed_action = f"Opening project '{cand_id}'"
                        action_result = await self.tool_executor.execute_tool("open_project", {"project_name": cand_id}, cand_id)
                        if action_result.get("status") == "success":
                            await self.record_outcome(rec_id, "completed")
                    elif cand_type == "skill" and cand_id:
                        executed_action = f"Suggested practice focus for skill '{cand_id}'"
                        action_result = {"status": "success", "response": f"Let's practice {cand_id}. You can start a study session anytime."}
                    elif cand_type == "goal" and cand_id:
                        executed_action = f"Focused on goal '{cand_id}'"
                        action_result = {"status": "success", "response": f"Active focus shifted to goal: {cand_id}."}
                except Exception as ex:
                    logger.error(f"Error executing accepted recommendation action: {ex}")
                    action_result = {"status": "error", "error": str(ex)}

            msg = f"Accepted recommendation: {rec_text}."
            if executed_action:
                msg += f" {executed_action}."

            return {
                "status": "success",
                "message": msg,
                "recommendation_id": rec_id,
                "accepted": True,
                "action_executed": executed_action,
                "tool_result": action_result
            }
        else:
            return {
                "status": "success",
                "message": f"Dismissed recommendation: {rec_text}. I'll avoid suggesting this for now.",
                "recommendation_id": rec_id,
                "accepted": False
            }

    async def record_outcome(self, rec_id_or_index: str | int | None, outcome: str) -> dict:
        """
        Records the final execution outcome ('completed', 'abandoned', 'neutral')
        for an accepted recommendation.
        """
        outcome_clean = (outcome or "").strip().lower()
        if outcome_clean not in ("completed", "abandoned", "neutral"):
            return {
                "status": "failure",
                "message": f"Invalid outcome '{outcome}'. Expected 'completed', 'abandoned', or 'neutral'."
            }

        rec_id, _ = self._resolve_recommendation(rec_id_or_index)
        if not rec_id:
            return {
                "status": "failure",
                "message": f"Could not find recommendation matching '{rec_id_or_index}'."
            }

        now = int(time.time())
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE recommendations SET outcome = ?, updated_at = ? WHERE id = ?;",
                    (outcome_clean, now, rec_id)
                )
            logger.info(f"Recorded outcome for recommendation {rec_id}: {outcome_clean}")
            return {
                "status": "success",
                "recommendation_id": rec_id,
                "outcome": outcome_clean
            }
        except Exception as e:
            logger.error(f"Error updating recommendation outcome: {e}")
            return {
                "status": "failure",
                "message": str(e)
            }

    async def tune_weights(self) -> dict:
        """
        Runs the self-tuning feedback loop:
        1. Inspects accepted recommendations and evaluates outcome (tool_logs, goal_events, study_sessions).
        2. Nudges weights:
           - Accepted + completed: +0.02 * metric_val
           - Accepted + abandoned: -0.01 * metric_val
           - Accepted + neutral: 0.0
           - Rejected: -0.01 * metric_val to contributing weights, +0.02 to w_rejection.
        3. Clamps all weights to [default * 0.5, default * 2.0].
        4. Saves updated weights in preferences table.
        """
        logger.info("Executing recommendation engine weight self-tuning loop...")
        weights = self._get_weights()
        prev_weights = weights.copy()
        now = int(time.time())
        evaluated_count = 0
        nudges_applied = {}

        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()

                cursor.execute(
                    "SELECT id, basis, accepted, outcome, generated_at, responded_at "
                    "FROM recommendations WHERE accepted IS NOT NULL ORDER BY responded_at DESC LIMIT 50;"
                )
                recs = cursor.fetchall()

                for row in recs:
                    rec_id = row["id"]
                    accepted = row["accepted"]
                    outcome = row["outcome"]
                    generated_at = row["generated_at"]
                    responded_at = row["responded_at"] or generated_at

                    try:
                        basis = json.loads(row["basis"]) if row["basis"] else {}
                    except Exception:
                        basis = {}

                    t_type = basis.get("type")
                    cand_id = basis.get("candidate_id")
                    metrics = basis.get("metrics", {})

                    if accepted == 1:
                        if not outcome:
                            if t_type == "project" and cand_id:
                                cursor.execute(
                                    "SELECT success FROM tool_logs WHERE tool_id = 'open_project' "
                                    "AND input LIKE ? AND executed_at >= ?;",
                                    (f"%{cand_id}%", generated_at)
                                )
                                log = cursor.fetchone()
                                if log and log["success"] == 1:
                                    outcome = "completed"
                            elif t_type == "goal" and cand_id:
                                cursor.execute(
                                    "SELECT occurred_at FROM goal_events WHERE goal_id = ? AND occurred_at >= ?;",
                                    (cand_id, generated_at)
                                )
                                event = cursor.fetchone()
                                if event:
                                    outcome = "completed"
                            elif t_type == "skill" and cand_id:
                                cursor.execute(
                                    "SELECT duration_minutes FROM study_sessions WHERE skill_id = ? AND started_at >= ?;",
                                    (cand_id, generated_at)
                                )
                                sess = cursor.fetchone()
                                if sess:
                                    dur = sess["duration_minutes"] or 0
                                    outcome = "completed" if dur >= 10 else "neutral"

                            if not outcome and (now - responded_at) > 43200:
                                outcome = "abandoned"

                            if outcome:
                                conn.execute("UPDATE recommendations SET outcome = ? WHERE id = ?;", (outcome, rec_id))

                        if outcome == "completed":
                            base_nudge = 0.02
                        elif outcome == "abandoned":
                            base_nudge = -0.01
                        else:
                            base_nudge = 0.0

                        for metric_name, val in metrics.items():
                            w_key = self._metric_to_weight_key(metric_name)
                            if w_key and w_key in weights and isinstance(val, (int, float)):
                                delta = round(base_nudge * float(val), 4)
                                weights[w_key] = round(weights[w_key] + delta, 4)
                                nudges_applied[w_key] = round(nudges_applied.get(w_key, 0.0) + delta, 4)

                    elif accepted == 0:
                        for metric_name, val in metrics.items():
                            w_key = self._metric_to_weight_key(metric_name)
                            if w_key and w_key in weights and isinstance(val, (int, float)):
                                delta = round(-0.01 * float(val), 4)
                                weights[w_key] = round(weights[w_key] + delta, 4)
                                nudges_applied[w_key] = round(nudges_applied.get(w_key, 0.0) + delta, 4)

                        weights["w_rejection"] = round(weights["w_rejection"] + 0.02, 4)
                        nudges_applied["w_rejection"] = round(nudges_applied.get("w_rejection", 0.0) + 0.02, 4)

                    evaluated_count += 1

            for key in list(weights.keys()):
                weights[key] = self._clamp_weight(key, weights[key])

            self._save_weights(weights)
            logger.info(f"Self-tuned weights ({evaluated_count} recs evaluated): {weights}")

            return {
                "status": "success",
                "previous_weights": prev_weights,
                "new_weights": weights,
                "nudges_applied": nudges_applied,
                "evaluated_recs_count": evaluated_count
            }
        except Exception as e:
            logger.error(f"Error tuning recommendation weights: {e}")
            return {
                "status": "failure",
                "error": str(e),
                "previous_weights": prev_weights,
                "new_weights": weights
            }

    async def run_session_end_feedback_loop(self) -> dict:
        """Alias for tune_weights called during session end / shutdown."""
        return await self.tune_weights()

    def _metric_to_weight_key(self, metric_name: str) -> str | None:
        mapping = {
            "goal_urgency": "w_goal",
            "habit_match": "w_habit",
            "recency_bonus": "w_recency",
            "skill_gap_relevance": "w_skill_gap",
            "unfinished_task_bonus": "w_unfinished",
            "rejection_penalty": "w_rejection"
        }
        return mapping.get(metric_name)


# =====================================================================
# Dedicated Recommendation Tools
# =====================================================================

class AcceptRecommendationTool(Tool):
    """
    Accepts a suggested recommendation (by index 1/2/3 or ID) and invokes
    the associated capability tool (e.g. open_project).
    """

    def __init__(self, recommendation_engine: RecommendationEngine):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "recommendation_id": {
                        "type": "string",
                        "description": "Index (1, 2, 3) or unique recommendation ID"
                    },
                    "auto_execute": {
                        "type": "boolean",
                        "description": "Whether to auto-execute the recommended action immediately"
                    }
                }
            },
            "side_effects": "Updates recommendation acceptance state and executes recommended tool action",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("accept_recommendation", "reversible", declaration)
        self.recommendation_engine = recommendation_engine

    async def execute(self, executor, **kwargs) -> dict:
        rec_id = kwargs.get("recommendation_id") or kwargs.get("rec_id") or kwargs.get("index") or "1"
        auto_exec = kwargs.get("auto_execute", True)
        if isinstance(auto_exec, str):
            auto_exec = auto_exec.lower() in ("true", "1", "yes")
        res = await self.recommendation_engine.record_feedback(rec_id, accepted=True, auto_execute=auto_exec)
        return res


class RejectRecommendationTool(Tool):
    """
    Rejects or dismisses a suggested recommendation, increasing candidate rejection
    penalty and self-tuning weights.
    """

    def __init__(self, recommendation_engine: RecommendationEngine):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "recommendation_id": {
                        "type": "string",
                        "description": "Index (1, 2, 3) or unique recommendation ID to dismiss"
                    }
                }
            },
            "side_effects": "Updates recommendation rejection state and applies penalty",
            "timeout_ms": 3000,
            "memory_limit_mb": 50
        }
        super().__init__("reject_recommendation", "reversible", declaration)
        self.recommendation_engine = recommendation_engine

    async def execute(self, executor, **kwargs) -> dict:
        rec_id = kwargs.get("recommendation_id") or kwargs.get("rec_id") or kwargs.get("index") or "1"
        res = await self.recommendation_engine.record_feedback(rec_id, accepted=False)
        return res


class TuneRecommendationWeightsTool(Tool):
    """
    Triggers the weight self-tuning algorithm across historical recommendation
    feedback and execution outcomes.
    """

    def __init__(self, recommendation_engine: RecommendationEngine):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {}
            },
            "side_effects": "Updates scoring weights stored in preferences table",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("tune_recommendation_weights", "reversible", declaration)
        self.recommendation_engine = recommendation_engine

    async def execute(self, executor, **kwargs) -> dict:
        res = await self.recommendation_engine.tune_weights()
        return res
