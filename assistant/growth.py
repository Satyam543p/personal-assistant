import abc
import json
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass, field

try:
    from assistant.database.manager import DatabaseManager
    from assistant.tools import Tool, JarvisToolExecutor
except ModuleNotFoundError:
    from database.manager import DatabaseManager
    from tools import Tool, JarvisToolExecutor

logger = logging.getLogger("jarvis.growth")

# =====================================================================
# Data Transfer Objects
# =====================================================================

@dataclass
class Goal:
    id: str
    title: str
    description: str = ""
    category: str = "study"       # "skill" | "project" | "habit" | "career" | "study"
    status: str = "active"        # "active" | "paused" | "completed" | "abandoned"
    target_date: int | None = None
    progress_pct: float = 0.0     # 0.0 - 100.0
    created_at: int = 0
    updated_at: int = 0

@dataclass
class GoalEvent:
    id: str
    goal_id: str
    event_type: str               # "progress_update" | "milestone" | "note" | "completed"
    value: float | None = None
    note: str = ""
    occurred_at: int = 0
    created_at: int = 0

@dataclass
class Skill:
    id: str
    name: str
    category: str = "programming" # "programming" | "tool" | "domain" | "language"
    level: str = "beginner"       # "beginner" | "intermediate" | "advanced" | "expert"
    confidence: float = 0.5       # 0.1 - 1.0 (0.1 is the floor per Section 23)
    last_practiced_at: int | None = None
    related_goal_ids: list[str] = field(default_factory=list)
    created_at: int = 0
    updated_at: int = 0

@dataclass
class StudySession:
    id: str
    topic: str
    skill_id: str | None = None
    resource_url: str = ""
    resource_type: str = "hands_on" # "video" | "article" | "book" | "course" | "hands_on"
    duration_minutes: int = 0
    notes: str = ""
    quality: float = 1.0            # 0.0 - 1.0
    started_at: int = 0
    ended_at: int | None = None
    created_at: int = 0
    updated_at: int = 0


# =====================================================================
# Abstract Interfaces (Abstractions & Interfaces First)
# =====================================================================

class GoalManager(abc.ABC):
    @abc.abstractmethod
    def create_goal(self, title: str, description: str = "", category: str = "study",
                    target_date: int | None = None, initial_progress: float = 0.0) -> Goal:
        pass

    @abc.abstractmethod
    def get_goal(self, goal_id: str) -> Goal | None:
        pass

    @abc.abstractmethod
    def list_goals(self, status: str = "active") -> list[Goal]:
        pass

    @abc.abstractmethod
    def update_progress(self, goal_id: str, progress_pct: float, note: str = "") -> Goal:
        pass

    @abc.abstractmethod
    def log_goal_event(self, goal_id: str, event_type: str, value: float | None = None, note: str = "") -> GoalEvent:
        pass

    @abc.abstractmethod
    def set_goal_status(self, goal_id: str, status: str) -> Goal:
        pass


class SkillTracker(abc.ABC):
    @abc.abstractmethod
    def register_skill(self, name: str, category: str = "programming", level: str = "beginner",
                       initial_confidence: float = 0.5, related_goal_ids: list[str] = None) -> Skill:
        pass

    @abc.abstractmethod
    def get_skill(self, skill_id_or_name: str) -> Skill | None:
        pass

    @abc.abstractmethod
    def list_skills(self) -> list[Skill]:
        pass

    @abc.abstractmethod
    def record_practice(self, skill_id_or_name: str, quality: float = 1.0, duration_minutes: int = 30) -> Skill:
        pass

    @abc.abstractmethod
    def get_decayed_confidence(self, skill_id_or_name: str, current_time: int | None = None) -> float:
        pass

    @abc.abstractmethod
    def decay_all_skills(self, current_time: int | None = None) -> list[dict]:
        pass


class StudySessionLogger(abc.ABC):
    @abc.abstractmethod
    def start_session(self, topic: str, resource_url: str = "", resource_type: str = "hands_on",
                      skill_id: str | None = None, started_at: int | None = None) -> StudySession:
        pass

    @abc.abstractmethod
    def end_session(self, session_id: str, notes: str = "", quality: float = 1.0,
                    ended_at: int | None = None) -> StudySession | None:
        pass

    @abc.abstractmethod
    def get_session(self, session_id: str) -> StudySession | None:
        pass

    @abc.abstractmethod
    def list_sessions(self, limit: int = 20) -> list[StudySession]:
        pass

    @abc.abstractmethod
    def recover_interrupted_sessions(self, max_age_hours: float = 2.0) -> list[dict]:
        pass


class GrowthEngine(abc.ABC):
    @abc.abstractmethod
    def generate_growth_dashboard(self) -> dict:
        pass

    @abc.abstractmethod
    def get_active_goals_summary(self) -> list[dict]:
        pass

    @abc.abstractmethod
    def get_skill_gaps_for_goals(self) -> list[dict]:
        pass


# =====================================================================
# Concrete SQLite Implementations
# =====================================================================

class SQLiteGoalManager(GoalManager):
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    def create_goal(self, title: str, description: str = "", category: str = "study",
                    target_date: int | None = None, initial_progress: float = 0.0) -> Goal:
        now = int(time.time())
        clean_title = "".join(c if c.isalnum() else "_" for c in title.lower())[:20]
        goal_id = f"goal_{clean_title}_{uuid.uuid4().hex[:6]}"
        initial_progress = max(0.0, min(100.0, float(initial_progress)))

        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO goals (id, title, description, category, status, target_date, progress_pct, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?);",
                (goal_id, title, description, category, target_date, initial_progress, now, now)
            )

        logger.info(f"Created goal '{title}' ({goal_id}). Target: {target_date}, Progress: {initial_progress}%")
        return Goal(
            id=goal_id, title=title, description=description, category=category,
            status="active", target_date=target_date, progress_pct=initial_progress,
            created_at=now, updated_at=now
        )

    def get_goal(self, goal_id: str) -> Goal | None:
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM goals WHERE id = ? OR title LIKE ? LIMIT 1;", (goal_id, f"%{goal_id}%"))
            row = cursor.fetchone()
            if not row:
                return None
            return Goal(
                id=row["id"], title=row["title"], description=row["description"] or "",
                category=row["category"] or "study", status=row["status"],
                target_date=row["target_date"], progress_pct=row["progress_pct"],
                created_at=row["created_at"], updated_at=row["updated_at"]
            )

    def list_goals(self, status: str = "active") -> list[Goal]:
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            if status == "all":
                cursor.execute("SELECT * FROM goals ORDER BY created_at DESC;")
            else:
                cursor.execute("SELECT * FROM goals WHERE status = ? ORDER BY created_at DESC;", (status,))
            rows = cursor.fetchall()
            return [
                Goal(
                    id=r["id"], title=r["title"], description=r["description"] or "",
                    category=r["category"] or "study", status=r["status"],
                    target_date=r["target_date"], progress_pct=r["progress_pct"],
                    created_at=r["created_at"], updated_at=r["updated_at"]
                ) for r in rows
            ]

    def update_progress(self, goal_id: str, progress_pct: float, note: str = "") -> Goal:
        now = int(time.time())
        progress_pct = max(0.0, min(100.0, float(progress_pct)))
        new_status = "completed" if progress_pct >= 100.0 else "active"

        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE goals SET progress_pct = ?, status = ?, updated_at = ? WHERE id = ?;",
                (progress_pct, new_status, now, goal_id)
            )

        event_type = "completed" if new_status == "completed" else "progress_update"
        self.log_goal_event(goal_id, event_type, value=progress_pct, note=note)
        logger.info(f"Updated goal '{goal_id}' progress to {progress_pct}% (Status: {new_status})")
        return self.get_goal(goal_id)

    def log_goal_event(self, goal_id: str, event_type: str, value: float | None = None, note: str = "") -> GoalEvent:
        now = int(time.time())
        event_id = f"gevent_{uuid.uuid4().hex[:8]}"

        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO goal_events (id, goal_id, event_type, value, note, occurred_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?);",
                (event_id, goal_id, event_type, value, note, now, now)
            )

        return GoalEvent(
            id=event_id, goal_id=goal_id, event_type=event_type,
            value=value, note=note, occurred_at=now, created_at=now
        )

    def set_goal_status(self, goal_id: str, status: str) -> Goal:
        now = int(time.time())
        with self.db.transaction() as conn:
            conn.execute("UPDATE goals SET status = ?, updated_at = ? WHERE id = ?;", (status, now, goal_id))
        self.log_goal_event(goal_id, "status_change", note=f"Status set to {status}")
        return self.get_goal(goal_id)


class SQLiteSkillTracker(SkillTracker):
    """
    Skill Tracker managing skills in SQLite with half-life decay.
    Half-life: 90 days (7,776,000 seconds).
    Minimum confidence floor: 0.1 (Section 23).
    """
    HALF_LIFE_SECONDS = 90 * 86400  # 90 days in seconds
    MIN_CONFIDENCE_FLOOR = 0.1

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    def register_skill(self, name: str, category: str = "programming", level: str = "beginner",
                       initial_confidence: float = 0.5, related_goal_ids: list[str] = None) -> Skill:
        now = int(time.time())
        skill_id = f"skill_{name.lower().replace(' ', '_')}"
        initial_confidence = max(self.MIN_CONFIDENCE_FLOOR, min(1.0, float(initial_confidence)))
        goals_json = json.dumps(related_goal_ids or [])

        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO skills (id, name, category, level, confidence, last_practiced_at, related_goal_ids, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET category = excluded.category, level = excluded.level, confidence = excluded.confidence, last_practiced_at = excluded.last_practiced_at, related_goal_ids = excluded.related_goal_ids, updated_at = excluded.updated_at;",
                (skill_id, name, category, level, initial_confidence, now, goals_json, now, now)
            )

        logger.info(f"Registered skill '{name}' ({skill_id}) with confidence {initial_confidence}")
        return Skill(
            id=skill_id, name=name, category=category, level=level,
            confidence=initial_confidence, last_practiced_at=now,
            related_goal_ids=related_goal_ids or [], created_at=now, updated_at=now
        )

    def get_skill(self, skill_id_or_name: str) -> Skill | None:
        target_id = skill_id_or_name if skill_id_or_name.startswith("skill_") else f"skill_{skill_id_or_name.lower().replace(' ', '_')}"
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM skills WHERE id = ? OR id = ? OR LOWER(name) = LOWER(?) LIMIT 1;",
                (skill_id_or_name, target_id, skill_id_or_name)
            )
            row = cursor.fetchone()
            if not row:
                return None
            goals_list = json.loads(row["related_goal_ids"]) if row["related_goal_ids"] else []
            return Skill(
                id=row["id"], name=row["name"], category=row["category"] or "programming",
                level=row["level"] or "beginner", confidence=row["confidence"],
                last_practiced_at=row["last_practiced_at"], related_goal_ids=goals_list,
                created_at=row["created_at"], updated_at=row["updated_at"]
            )

    def list_skills(self) -> list[Skill]:
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM skills ORDER BY confidence DESC;")
            rows = cursor.fetchall()
            skills = []
            for r in rows:
                goals_list = json.loads(r["related_goal_ids"]) if r["related_goal_ids"] else []
                skills.append(Skill(
                    id=r["id"], name=r["name"], category=r["category"] or "programming",
                    level=r["level"] or "beginner", confidence=r["confidence"],
                    last_practiced_at=r["last_practiced_at"], related_goal_ids=goals_list,
                    created_at=r["created_at"], updated_at=r["updated_at"]
                ))
            return skills

    def get_decayed_confidence(self, skill_id_or_name: str, current_time: int | None = None) -> float:
        skill = self.get_skill(skill_id_or_name)
        if not skill:
            return self.MIN_CONFIDENCE_FLOOR

        now = current_time or int(time.time())
        last_t = skill.last_practiced_at or skill.created_at
        delta_sec = max(0, now - last_t)

        decay_factor = math.exp(-(math.log(2) / self.HALF_LIFE_SECONDS) * delta_sec)
        decayed = skill.confidence * decay_factor
        return round(max(self.MIN_CONFIDENCE_FLOOR, min(1.0, decayed)), 4)

    def record_practice(self, skill_id_or_name: str, quality: float = 1.0, duration_minutes: int = 30) -> Skill:
        skill = self.get_skill(skill_id_or_name)
        if not skill:
            skill = self.register_skill(skill_id_or_name)

        now = int(time.time())
        # First calculate decayed confidence up to this practice moment
        curr_conf = self.get_decayed_confidence(skill.name, current_time=now)

        # Confidence boost proportional to practice duration and quality
        # Max boost: 0.15 per session
        boost = 0.05 * max(0.0, min(1.0, quality)) * min(1.0, duration_minutes / 45.0)
        new_conf = round(min(1.0, curr_conf + boost), 4)

        # Re-evaluate level
        if new_conf < 0.4:
            new_level = "beginner"
        elif new_conf < 0.7:
            new_level = "intermediate"
        elif new_conf < 0.9:
            new_level = "advanced"
        else:
            new_level = "expert"

        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE skills SET confidence = ?, level = ?, last_practiced_at = ?, updated_at = ? WHERE id = ?;",
                (new_conf, new_level, now, now, skill.id)
            )

        logger.info(f"Recorded practice for skill '{skill.name}': confidence {curr_conf} -> {new_conf} ({new_level})")
        return self.get_skill(skill.id)

    def decay_all_skills(self, current_time: int | None = None) -> list[dict]:
        now = current_time or int(time.time())
        all_skills = self.list_skills()
        updates = []

        with self.db.transaction() as conn:
            for s in all_skills:
                decayed = self.get_decayed_confidence(s.name, current_time=now)
                if abs(decayed - s.confidence) > 0.001:
                    # Update level if crossed boundary
                    if decayed < 0.4:
                        lvl = "beginner"
                    elif decayed < 0.7:
                        lvl = "intermediate"
                    elif decayed < 0.9:
                        lvl = "advanced"
                    else:
                        lvl = "expert"

                    conn.execute(
                        "UPDATE skills SET confidence = ?, level = ?, updated_at = ? WHERE id = ?;",
                        (decayed, lvl, now, s.id)
                    )
                    updates.append({"skill": s.name, "old_confidence": s.confidence, "new_confidence": decayed, "level": lvl})

        return updates


class SQLiteStudySessionLogger(StudySessionLogger):
    """
    Manages study sessions with Section 31 rules:
    - Discards sessions < 2 minutes (accidental opens)
    - Records start, end, quality, resource_url
    - Recovers interrupted sessions older than 2 hours (Section 23)
    """

    def __init__(self, db_manager: DatabaseManager, skill_tracker: SkillTracker = None):
        self.db = db_manager
        self.skill_tracker = skill_tracker

    def start_session(self, topic: str, resource_url: str = "", resource_type: str = "hands_on",
                      skill_id: str | None = None, started_at: int | None = None) -> StudySession:
        now = started_at or int(time.time())
        session_id = f"session_{uuid.uuid4().hex[:8]}"

        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO study_sessions (id, topic, skill_id, resource_url, resource_type, duration_minutes, notes, quality, started_at, ended_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 0, '', 1.0, ?, NULL, ?, ?);",
                (session_id, topic, skill_id, resource_url, resource_type, now, now, now)
            )

        logger.info(f"Started study session '{session_id}' for topic: '{topic}'")
        return StudySession(
            id=session_id, topic=topic, skill_id=skill_id, resource_url=resource_url,
            resource_type=resource_type, duration_minutes=0, notes="", quality=1.0,
            started_at=now, ended_at=None, created_at=now, updated_at=now
        )

    def end_session(self, session_id: str, notes: str = "", quality: float = 1.0,
                    ended_at: int | None = None) -> StudySession | None:
        now = ended_at or int(time.time())
        session = self.get_session(session_id)
        if not session:
            logger.warning(f"Cannot end study session '{session_id}': not found")
            return None

        duration_sec = max(0, now - session.started_at)
        duration_minutes = duration_sec // 60

        # Section 31 Rule: If duration is under 2 minutes, the session is discarded
        if duration_minutes < 2:
            logger.info(f"Study session '{session_id}' duration is {duration_sec}s (< 2 min). Discarding session per Section 31.")
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM study_sessions WHERE id = ?;", (session_id,))
            return None

        quality = max(0.0, min(1.0, float(quality)))

        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE study_sessions SET duration_minutes = ?, notes = ?, quality = ?, ended_at = ?, updated_at = ? WHERE id = ?;",
                (duration_minutes, notes, quality, now, now, session_id)
            )

        # Notify skill tracker if skill_id is linked
        if self.skill_tracker and session.skill_id:
            try:
                self.skill_tracker.record_practice(session.skill_id, quality=quality, duration_minutes=duration_minutes)
            except Exception as e:
                logger.error(f"Error updating skill after study session: {e}")

        logger.info(f"Completed study session '{session_id}': {duration_minutes} mins (Quality: {quality})")
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> StudySession | None:
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM study_sessions WHERE id = ? LIMIT 1;", (session_id,))
            r = cursor.fetchone()
            if not r:
                return None
            return StudySession(
                id=r["id"], topic=r["topic"], skill_id=r["skill_id"],
                resource_url=r["resource_url"] or "", resource_type=r["resource_type"] or "hands_on",
                duration_minutes=r["duration_minutes"] or 0, notes=r["notes"] or "",
                quality=r["quality"] or 1.0, started_at=r["started_at"],
                ended_at=r["ended_at"], created_at=r["created_at"], updated_at=r["updated_at"]
            )

    def list_sessions(self, limit: int = 20) -> list[StudySession]:
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM study_sessions ORDER BY started_at DESC LIMIT ?;", (limit,))
            rows = cursor.fetchall()
            return [
                StudySession(
                    id=r["id"], topic=r["topic"], skill_id=r["skill_id"],
                    resource_url=r["resource_url"] or "", resource_type=r["resource_type"] or "hands_on",
                    duration_minutes=r["duration_minutes"] or 0, notes=r["notes"] or "",
                    quality=r["quality"] or 1.0, started_at=r["started_at"],
                    ended_at=r["ended_at"], created_at=r["created_at"], updated_at=r["updated_at"]
                ) for r in rows
            ]

    def recover_interrupted_sessions(self, max_age_hours: float = 2.0) -> list[dict]:
        """
        Section 23 crash recovery:
        Detects study sessions started_at set, ended_at null, older than max_age_hours.
        Auto-closes or flags them to preserve database integrity.
        """
        cutoff = int(time.time()) - int(max_age_hours * 3600)
        recovered = []

        with self.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM study_sessions WHERE ended_at IS NULL AND started_at < ?;", (cutoff,))
            rows = cursor.fetchall()
            for r in rows:
                session_id = r["id"]
                duration = 30  # Fallback assumption for interrupted sessions
                conn.execute(
                    "UPDATE study_sessions SET ended_at = started_at + 1800, duration_minutes = 30, "
                    "notes = 'Auto-closed after restart / crash recovery', updated_at = ? WHERE id = ?;",
                    (int(time.time()), session_id)
                )
                recovered.append({
                    "session_id": session_id,
                    "topic": r["topic"],
                    "started_at": r["started_at"],
                    "action": "auto_closed_crash_recovery"
                })
                logger.info(f"Crash recovery: auto-closed interrupted study session '{session_id}' ({r['topic']}).")

        return recovered


# =====================================================================
# Learning Graph Component
# =====================================================================

class LearningGraph:
    """
    Logical layer mapping goals to required skill clusters and prerequisite paths
    defined in assistant/learning_graph.json (Section 31).
    """

    def __init__(self, config_path: str = None):
        self.config_path = config_path or os.path.abspath(
            os.path.join(os.path.dirname(__file__), "learning_graph.json")
        )
        self.data = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.config_path):
            logger.warning(f"learning_graph.json not found at {self.config_path}")
            return {"goals": {}, "skill_prerequisites": {}}
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading learning_graph.json: {e}")
            return {"goals": {}, "skill_prerequisites": {}}

    def get_goal_requirements(self, goal_key: str) -> dict:
        goals = self.data.get("goals", {})
        # Find by exact key or title match
        if goal_key in goals:
            return goals[goal_key]
        for k, v in goals.items():
            if goal_key.lower() in k.lower() or goal_key.lower() in v.get("title", "").lower():
                return v
        return {}

    def get_skill_gaps(self, goal_key: str, user_skills: dict[str, Skill]) -> list[dict]:
        req = self.get_goal_requirements(goal_key)
        if not req:
            return []

        required_skills = req.get("required_skills", [])
        thresholds = req.get("skill_thresholds", {})
        level_order = {"beginner": 1, "intermediate": 2, "advanced": 3, "expert": 4}

        gaps = []
        for s_name in required_skills:
            target_level = thresholds.get(s_name, "intermediate")
            current_skill = user_skills.get(s_name.lower())
            curr_level = current_skill.level if current_skill else "none"
            curr_val = level_order.get(curr_level, 0)
            target_val = level_order.get(target_level, 2)

            if curr_val < target_val:
                gaps.append({
                    "skill": s_name,
                    "current_level": curr_level,
                    "target_level": target_level,
                    "gap_severity": round((target_val - curr_val) / target_val, 2),
                    "confidence": current_skill.confidence if current_skill else 0.0
                })

        return gaps


# =====================================================================
# Jarvis Growth Engine Aggregator Subsystem
# =====================================================================

class JarvisGrowthEngine(GrowthEngine):
    """
    Master coordinator combining GoalManager, SkillTracker,
    StudySessionLogger, and LearningGraph.
    """

    def __init__(self, db_manager: DatabaseManager, learning_graph_path: str = None):
        self.db = db_manager
        self.goal_manager = SQLiteGoalManager(db_manager)
        self.skill_tracker = SQLiteSkillTracker(db_manager)
        self.study_logger = SQLiteStudySessionLogger(db_manager, skill_tracker=self.skill_tracker)
        self.learning_graph = LearningGraph(learning_graph_path)

        # On startup: run interrupted study session recovery
        self.study_logger.recover_interrupted_sessions()

    # SkillTracker delegation
    def register_skill(self, name: str, category: str = "programming", level: str = "beginner",
                       initial_confidence: float = 0.5, related_goal_ids: list[str] = None) -> Skill:
        return self.skill_tracker.register_skill(name, category, level, initial_confidence, related_goal_ids)

    def get_skill(self, skill_id_or_name: str) -> Skill | None:
        return self.skill_tracker.get_skill(skill_id_or_name)

    def list_skills(self) -> list[Skill]:
        return self.skill_tracker.list_skills()

    def record_practice(self, skill_id_or_name: str, quality: float = 1.0, duration_minutes: int = 30) -> Skill:
        return self.skill_tracker.record_practice(skill_id_or_name, quality, duration_minutes)

    def get_decayed_confidence(self, skill_id_or_name: str, current_time: int | None = None) -> float:
        return self.skill_tracker.get_decayed_confidence(skill_id_or_name, current_time)

    def decay_all_skills(self, current_time: int | None = None) -> list[dict]:
        return self.skill_tracker.decay_all_skills(current_time)

    # StudySessionLogger delegation
    def start_session(self, topic: str, resource_url: str = "", resource_type: str = "hands_on",
                      skill_id: str | None = None, started_at: int | None = None) -> StudySession:
        return self.study_logger.start_session(topic, resource_url, resource_type, skill_id, started_at)

    def end_session(self, session_id: str, notes: str = "", quality: float = 1.0,
                    ended_at: int | None = None) -> StudySession | None:
        return self.study_logger.end_session(session_id, notes, quality, ended_at)

    def get_session(self, session_id: str) -> StudySession | None:
        return self.study_logger.get_session(session_id)

    def list_sessions(self, limit: int = 20) -> list[StudySession]:
        return self.study_logger.list_sessions(limit)

    # GoalManager delegation
    def create_goal(self, title: str, description: str = "", category: str = "study",
                    target_date: int | None = None, initial_progress: float = 0.0) -> Goal:
        return self.goal_manager.create_goal(title, description, category, target_date, initial_progress)

    def get_goal(self, goal_id: str) -> Goal | None:
        return self.goal_manager.get_goal(goal_id)

    def list_goals(self, status: str = "active") -> list[Goal]:
        return self.goal_manager.list_goals(status)

    def update_progress(self, goal_id: str, progress_pct: float, note: str = "") -> Goal:
        return self.goal_manager.update_progress(goal_id, progress_pct, note)

    def generate_growth_dashboard(self) -> dict:
        now = int(time.time())
        goals = self.goal_manager.list_goals(status="active")
        skills = self.skill_tracker.list_skills()
        sessions = self.study_logger.list_sessions(limit=10)

        # Calculate study time in past 7 days
        week_ago = now - 7 * 86400
        recent_sessions = [s for s in sessions if s.started_at >= week_ago]
        total_week_minutes = sum(s.duration_minutes for s in recent_sessions)
        total_week_hours = round(total_week_minutes / 60.0, 1)

        # Build skills map and calculate decayed confidences
        skills_map = {s.name.lower(): s for s in skills}
        skills_summary = []
        for s in skills:
            decayed = self.skill_tracker.get_decayed_confidence(s.name, current_time=now)
            days_practiced = None
            if s.last_practiced_at:
                days_practiced = round((now - s.last_practiced_at) / 86400.0, 1)
            skills_summary.append({
                "name": s.name,
                "level": s.level,
                "confidence": decayed,
                "category": s.category,
                "days_since_practiced": days_practiced
            })

        # Calculate goal milestones and gaps
        goals_summary = []
        all_gaps = []
        for g in goals:
            days_left = None
            if g.target_date:
                days_left = round((g.target_date - now) / 86400.0, 1)

            # Check gaps for this goal
            goal_gaps = self.learning_graph.get_skill_gaps(g.title, skills_map)
            all_gaps.extend(goal_gaps)

            goals_summary.append({
                "id": g.id,
                "title": g.title,
                "category": g.category,
                "progress_pct": g.progress_pct,
                "status": g.status,
                "days_left": days_left,
                "gaps": goal_gaps
            })

        # Generate human-readable summary text
        lines = [
            "==================================================",
            "             JARVIS GROWTH DASHBOARD              ",
            "==================================================",
            f"Study Time (Last 7 Days): {total_week_hours} hours ({len(recent_sessions)} sessions)",
            "",
            "Active Goals:"
        ]
        if not goals_summary:
            lines.append("  No active goals set. (Tell me a goal to begin tracking!)")
        else:
            for g in goals_summary:
                deadline_str = f", target in {g['days_left']} days" if g['days_left'] is not None else ""
                lines.append(f"  * {g['title']}: {g['progress_pct']}% complete{deadline_str}")

        lines.append("")
        lines.append("Skills Overview:")
        if not skills_summary:
            lines.append("  No skills registered yet.")
        else:
            for s in skills_summary:
                practiced_str = f" (practiced {s['days_since_practiced']}d ago)" if s['days_since_practiced'] is not None else ""
                lines.append(f"  * {s['name']}: {s['level'].capitalize()} (Confidence: {int(s['confidence']*100)}%){practiced_str}")

        if all_gaps:
            lines.append("")
            lines.append("Identified Skill Gaps for Goals:")
            for gap in all_gaps:
                lines.append(f"  ! {gap['skill']}: currently {gap['current_level']} (target: {gap['target_level']})")

        lines.append("==================================================")
        readable_text = "\n".join(lines)

        return {
            "status": "success",
            "formatted_dashboard": readable_text,
            "weekly_study_hours": total_week_hours,
            "goals": goals_summary,
            "skills": skills_summary,
            "skill_gaps": all_gaps
        }

    def get_active_goals_summary(self) -> list[dict]:
        now = int(time.time())
        goals = self.goal_manager.list_goals(status="active")
        summary = []
        for g in goals:
            days_left = round((g.target_date - now) / 86400.0, 1) if g.target_date else 999.0
            summary.append({
                "id": g.id,
                "title": g.title,
                "category": g.category,
                "progress_pct": g.progress_pct,
                "days_left": days_left
            })
        return summary

    def get_skill_gaps_for_goals(self) -> list[dict]:
        goals = self.goal_manager.list_goals(status="active")
        skills = self.skill_tracker.list_skills()
        skills_map = {s.name.lower(): s for s in skills}
        gaps = []
        for g in goals:
            gaps.extend(self.learning_graph.get_skill_gaps(g.title, skills_map))
        return gaps


# =====================================================================
# Scoped Tool: GrowthDashboardTool
# =====================================================================

class GrowthDashboardTool(Tool):
    """
    Standard Jarvis capability tool exposing the personal growth dashboard.
    """

    def __init__(self, growth_engine: JarvisGrowthEngine):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": ["text", "json"], "default": "text"}
                }
            },
            "side_effects": "none",
            "timeout_ms": 3000,
            "memory_limit_mb": 50
        }
        super().__init__("growth_dashboard", "read_only", declaration)
        self.growth_engine = growth_engine

    async def execute(self, executor, **kwargs) -> dict:
        fmt = kwargs.get("format", "text")
        data = self.growth_engine.generate_growth_dashboard()
        if fmt == "json":
            return {"status": "success", "response": json.dumps(data, indent=2), "data": data}
        return {"status": "success", "response": data["formatted_dashboard"], "data": data}
