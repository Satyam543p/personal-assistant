import abc
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Any

try:
    from assistant.database.manager import DatabaseManager
    from assistant.tools import Tool, JarvisToolExecutor
except ModuleNotFoundError:
    from database.manager import DatabaseManager
    from tools import Tool, JarvisToolExecutor

logger = logging.getLogger("jarvis.profiles")

# =====================================================================
# Data Transfer Objects
# =====================================================================

@dataclass
class WorkspaceProfile:
    name: str
    display_name: str
    description: str = ""
    default_project_id: str | None = None
    biased_tools: list[str] = field(default_factory=list)
    biased_categories: list[str] = field(default_factory=list)
    prewarm_resources: list[str] = field(default_factory=list)
    created_at: int = 0
    updated_at: int = 0


# =====================================================================
# Abstract Interfaces (Abstractions & Interfaces First)
# =====================================================================

class ProfileManager(abc.ABC):
    @abc.abstractmethod
    def get_profile(self, name: str) -> WorkspaceProfile | None:
        """Retrieves a workspace profile by name."""
        pass

    @abc.abstractmethod
    def list_profiles(self) -> list[WorkspaceProfile]:
        """Lists all configured workspace profiles."""
        pass

    @abc.abstractmethod
    def get_active_profile(self) -> WorkspaceProfile:
        """Returns the currently active workspace profile."""
        pass

    @abc.abstractmethod
    def switch_profile(self, name: str) -> dict:
        """
        Switches the active workspace profile.
        Updates current_focus.workspace_mode and triggers on_profile_switch callback.
        """
        pass

    @abc.abstractmethod
    def update_profile(self, name: str, default_project_id: str | None = None) -> bool:
        """Updates default project for a profile."""
        pass


# =====================================================================
# Concrete SQLite Implementation
# =====================================================================

class SQLiteProfileManager(ProfileManager):
    """
    Manages workspace profiles backed by SQLite workspace_profiles table
    and current_focus table per Section 8 of phase.md.
    """

    DEFAULT_PROFILE = "coding"

    def __init__(self, db_manager: DatabaseManager, on_profile_switch: Callable[[str, str], Any] | None = None):
        self.db = db_manager
        self.on_profile_switch = on_profile_switch
        self._ensure_defaults_seeded()

    def _ensure_defaults_seeded(self):
        """Ensures the 4 canonical profiles exist in SQLite."""
        now = int(time.time())
        canonical_defaults = [
            ("coding", "Coding Mode", "Focus on codebase navigation, file operations, and project execution.", "Jarvis",
             json.dumps(["open_project", "read_file", "write_file", "search_files", "save_workspace"]),
             json.dumps(["project", "task", "coding"]), json.dumps([])),
            ("study", "Study Mode", "Focus on learning goals, skill development, and study tracking.", None,
             json.dumps(["growth_dashboard", "continue_working"]),
             json.dumps(["goal", "skill", "study"]), json.dumps(["growth_engine"])),
            ("research", "Research Mode", "Focus on deep inquiry, topic exploration, and literature synthesis.", None,
             json.dumps(["search_files", "read_file"]),
             json.dumps(["research", "exploration", "topic"]), json.dumps(["cloud_client"])),
            ("project", "Project Mode", "Focus on multi-step workflows, planning, and task execution.", "Jarvis",
             json.dumps(["continue_working", "save_workspace"]),
             json.dumps(["project", "workflow", "plan"]), json.dumps(["planner"]))
        ]
        try:
            with self.db.transaction() as conn:
                for row in canonical_defaults:
                    conn.execute(
                        "INSERT OR IGNORE INTO workspace_profiles "
                        "(name, display_name, description, default_project_id, biased_tools, biased_categories, prewarm_resources, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
                        (row[0], row[1], row[2], row[3], row[4], row[5], row[6], now, now)
                    )
        except Exception as e:
            logger.debug(f"Profiles seeding note: {e}")

    def get_profile(self, name: str) -> WorkspaceProfile | None:
        name_clean = (name or "").strip().lower()
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM workspace_profiles WHERE name = ? OR display_name LIKE ? LIMIT 1;",
                    (name_clean, f"%{name_clean}%")
                )
                row = cursor.fetchone()
                if not row:
                    return None
                return self._row_to_profile(row)
        except Exception as e:
            logger.error(f"Error fetching profile '{name}': {e}")
            return None

    def list_profiles(self) -> list[WorkspaceProfile]:
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM workspace_profiles ORDER BY name ASC;")
                rows = cursor.fetchall()
                return [self._row_to_profile(r) for r in rows]
        except Exception as e:
            logger.error(f"Error listing profiles: {e}")
            return []

    def get_active_profile(self) -> WorkspaceProfile:
        """Queries current_focus.workspace_mode; defaults to 'coding' mode."""
        active_name = self.DEFAULT_PROFILE
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT workspace_mode FROM current_focus WHERE id = 1;")
                row = cursor.fetchone()
                if row and row["workspace_mode"]:
                    active_name = row["workspace_mode"]
        except Exception as e:
            logger.debug(f"Error reading active profile from current_focus: {e}")

        prof = self.get_profile(active_name)
        if not prof:
            prof = self.get_profile(self.DEFAULT_PROFILE)
        if not prof:
            prof = WorkspaceProfile(
                name="coding",
                display_name="Coding Mode",
                description="Default coding mode",
                biased_tools=["open_project", "read_file", "write_file", "search_files", "save_workspace"],
                biased_categories=["project", "task", "coding"]
            )
        return prof

    def switch_profile(self, name: str) -> dict:
        target_name = (name or "").strip().lower()
        # Normalization of synonyms
        if target_name in ("code", "developer", "dev"):
            target_name = "coding"
        elif target_name in ("learn", "learning", "growth"):
            target_name = "study"
        elif target_name in ("explore", "web"):
            target_name = "research"
        elif target_name in ("workflow", "tasks", "task"):
            target_name = "project"

        target_prof = self.get_profile(target_name)
        if not target_prof:
            avail = [p.name for p in self.list_profiles()]
            return {
                "status": "failure",
                "message": f"Unknown profile '{name}'. Available profiles: {', '.join(avail)}."
            }

        old_prof = self.get_active_profile()
        old_name = old_prof.name if old_prof else self.DEFAULT_PROFILE
        now = int(time.time())

        # Update current_focus table
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO current_focus (id, workspace_mode, updated_at) VALUES (1, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET workspace_mode = excluded.workspace_mode, updated_at = excluded.updated_at;",
                    (target_prof.name, now)
                )
                # If target profile has a default project and current_focus has no project, set it
                if target_prof.default_project_id:
                    conn.execute(
                        "UPDATE current_focus SET project_id = ? WHERE id = 1 AND (project_id IS NULL OR project_id = '');",
                        (target_prof.default_project_id,)
                    )
        except Exception as e:
            logger.error(f"Error persisting profile switch in SQLite: {e}")

        logger.info(f"Workspace profile switched: '{old_name}' -> '{target_prof.name}'")

        # Invoke callback for resource pre-warming / tool lifecycle
        prewarmed = []
        if self.on_profile_switch:
            try:
                res = self.on_profile_switch(old_name, target_prof.name)
                if isinstance(res, list):
                    prewarmed = res
            except Exception as cb_err:
                logger.error(f"Error executing on_profile_switch callback: {cb_err}")

        return {
            "status": "success",
            "previous_profile": old_name,
            "active_profile": target_prof.name,
            "display_name": target_prof.display_name,
            "description": target_prof.description,
            "biased_tools": target_prof.biased_tools,
            "biased_categories": target_prof.biased_categories,
            "prewarm_resources": target_prof.prewarm_resources,
            "response": f"Switched to {target_prof.display_name}. {target_prof.description}"
        }

    def update_profile(self, name: str, default_project_id: str | None = None) -> bool:
        now = int(time.time())
        try:
            with self.db.transaction() as conn:
                cursor = conn.execute(
                    "UPDATE workspace_profiles SET default_project_id = ?, updated_at = ? WHERE name = ?;",
                    (default_project_id, now, name)
                )
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error updating profile {name}: {e}")
            return False

    def _row_to_profile(self, row) -> WorkspaceProfile:
        tools = json.loads(row["biased_tools"]) if row["biased_tools"] else []
        categories = json.loads(row["biased_categories"]) if row["biased_categories"] else []
        prewarm = json.loads(row["prewarm_resources"]) if row["prewarm_resources"] else []
        return WorkspaceProfile(
            name=row["name"],
            display_name=row["display_name"],
            description=row["description"] or "",
            default_project_id=row["default_project_id"],
            biased_tools=tools,
            biased_categories=categories,
            prewarm_resources=prewarm,
            created_at=row["created_at"],
            updated_at=row["updated_at"]
        )


# =====================================================================
# Dedicated Profile Tools
# =====================================================================

class SwitchProfileTool(Tool):
    """
    Switches active workspace profile mode (e.g. coding, study, research, project),
    biasing recommendation ranking and pre-warming relevant subsystems.
    """

    def __init__(self, profile_manager: ProfileManager):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "profile_name": {
                        "type": "string",
                        "description": "Name of target profile: coding, study, research, project"
                    }
                },
                "required": ["profile_name"]
            },
            "side_effects": "Updates current_focus workspace_mode and triggers resource pre-warming",
            "timeout_ms": 3000,
            "memory_limit_mb": 50
        }
        super().__init__("switch_profile", "reversible", declaration)
        self.profile_manager = profile_manager

    async def execute(self, executor, **kwargs) -> dict:
        profile_name = kwargs.get("profile_name", "coding")
        res = self.profile_manager.switch_profile(profile_name)
        return res


class GetProfileTool(Tool):
    """
    Inspects the currently active workspace profile and lists available modes.
    """

    def __init__(self, profile_manager: ProfileManager):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {}
            },
            "side_effects": "Reads active workspace profile from SQLite",
            "timeout_ms": 2000,
            "memory_limit_mb": 50
        }
        super().__init__("current_profile", "read_only", declaration)
        self.profile_manager = profile_manager

    async def execute(self, executor, **kwargs) -> dict:
        active = self.profile_manager.get_active_profile()
        all_profiles = self.profile_manager.list_profiles()

        profiles_summary = [f"{p.name} ({p.display_name})" for p in all_profiles]
        rep = (
            f"Active profile: {active.display_name} ({active.name}).\n"
            f"Description: {active.description}\n"
            f"Available profiles: {', '.join(profiles_summary)}."
        )

        return {
            "status": "success",
            "active_profile": active.name,
            "display_name": active.display_name,
            "description": active.description,
            "biased_tools": active.biased_tools,
            "biased_categories": active.biased_categories,
            "available_profiles": [p.name for p in all_profiles],
            "response": rep
        }
