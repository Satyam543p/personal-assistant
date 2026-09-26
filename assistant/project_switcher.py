"""
Multi-Project Context Switching Subsystem (Section 44 & 45 of phase.md)
Provides graceful active project context switching with workspace snapshotting,
focus transition, working memory pre-warming, pronoun reference reset,
context bleed prevention, 30-day active awareness, and >48h catch-up briefs.
"""

import abc
import json
import logging
import os
import time
from dataclasses import dataclass, field

try:
    from assistant.database.manager import DatabaseManager
    from assistant.projects import ProjectRegistry, SQLiteProjectRegistry
    from assistant.snapshots import SnapshotManager, SQLiteSnapshotManager, WorkspaceSnapshot
    from assistant.memory import MemoryRepository, JarvisMemoryManager
    from assistant.router import SQLiteContextStore
    from assistant.tools import Tool, JarvisToolExecutor
except ModuleNotFoundError:
    from database.manager import DatabaseManager
    from projects import ProjectRegistry, SQLiteProjectRegistry
    from snapshots import SnapshotManager, SQLiteSnapshotManager, WorkspaceSnapshot
    from memory import MemoryRepository, JarvisMemoryManager
    from router import SQLiteContextStore
    from tools import Tool, JarvisToolExecutor

logger = logging.getLogger("jarvis.project_switcher")


# =====================================================================
# Abstract Interface (Abstractions & Interfaces First)
# =====================================================================

class ProjectContextSwitcher(abc.ABC):
    """
    Abstract interface for multi-project context switching per Section 44 of phase.md.
    """

    @abc.abstractmethod
    async def switch_project(
        self,
        target_project: str,
        preferred_editor: str | None = None,
        auto_snapshot_previous: bool = True
    ) -> dict:
        """
        Executes graceful 7-step project switch sequence:
        1. Snapshot current workspace (section 33)
        2. Update current_focus.project_id to new project
        3. Load new project's most recent workspace snapshot
        4. Load new project's project-specific memories into working context
        5. Update active_context.current_pronoun_ref to null (references do not carry over)
        6. Open project in editor (via open_project tool)
        7. Report: "Switched to [project X]. Last session: [date]. Restoring [N] files."
        """
        pass

    @abc.abstractmethod
    def list_active_projects(self, max_days: int = 30) -> dict:
        """
        Lists projects categorized into active (opened within max_days) and archived.
        """
        pass

    @abc.abstractmethod
    def get_catchup_brief(self, project_id: str, force: bool = False) -> dict | None:
        """
        Synthesizes a 3-5 bullet catch-up brief when returning after a >48 hour gap (Section 45).
        """
        pass


# =====================================================================
# Concrete Implementation
# =====================================================================

class JarvisProjectContextSwitcher(ProjectContextSwitcher):
    """
    Concrete project context switching orchestrator managing workspace snapshots,
    working context memory injection, and cross-project isolation.
    """

    LONG_GAP_SECONDS = 48 * 3600  # 48 hours per Section 45
    ACTIVE_WINDOW_SECONDS = 30 * 86400  # 30 days per Section 44

    def __init__(
        self,
        db_manager: DatabaseManager,
        project_registry: ProjectRegistry | None = None,
        snapshot_manager: SnapshotManager | None = None,
        memory_manager: MemoryRepository | None = None,
        context_store: SQLiteContextStore | None = None,
        tool_executor: JarvisToolExecutor | None = None
    ):
        self.db = db_manager
        self.project_registry = project_registry or SQLiteProjectRegistry(db_manager)
        self.snapshot_manager = snapshot_manager or SQLiteSnapshotManager(db_manager, project_registry=self.project_registry)
        self.memory_manager = memory_manager or JarvisMemoryManager(db_manager)
        self.context_store = context_store or SQLiteContextStore(db_manager)
        self.tool_executor = tool_executor

    def _get_current_focus_project(self) -> tuple[str | None, int | None]:
        """Reads current focus project ID and timestamp from SQLite."""
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT project_id, updated_at FROM current_focus WHERE id = 1;")
                row = cursor.fetchone()
                if row:
                    return row["project_id"], row["updated_at"]
        except Exception as e:
            logger.error(f"Error querying current_focus in project switcher: {e}")
        return None, None

    def _resolve_project(self, project_identifier: str) -> tuple[str, str, str, str | None, int | None]:
        """
        Resolves project identifier into (id, name, folder_path, preferred_editor, last_opened_at).
        """
        p_id = project_identifier.strip()
        p_name = p_id
        folder_path = ""
        preferred_editor = None
        last_opened_at = None

        try:
            resolved = self.project_registry.resolve_project(p_id)
            if resolved:
                return (
                    resolved.id,
                    resolved.name,
                    resolved.folder_path,
                    resolved.preferred_editor,
                    resolved.last_opened_at
                )
        except Exception:
            pass

        # Query projects table directly
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, name, folder_path, preferred_editor, last_opened_at "
                    "FROM projects WHERE id = ? OR name = ? "
                    "OR LOWER(id) = LOWER(?) OR LOWER(name) = LOWER(?) "
                    "OR id LIKE ? OR name LIKE ? LIMIT 1;",
                    (p_id, p_id, p_id, p_id, f"%{p_id}%", f"%{p_id}%")
                )
                row = cursor.fetchone()
                if row:
                    return (
                        row["id"],
                        row["name"],
                        row["folder_path"],
                        row["preferred_editor"],
                        row["last_opened_at"]
                    )
        except Exception as e:
            logger.error(f"Error resolving project '{project_identifier}' in DB: {e}")

        return p_id, p_name, folder_path, preferred_editor, last_opened_at

    async def switch_project(
        self,
        target_project: str,
        preferred_editor: str | None = None,
        auto_snapshot_previous: bool = True
    ) -> dict:
        """
        Executes Section 44 switch sequence:
        1. Snapshot current workspace
        2. Update current_focus.project_id to new project
        3. Load new project's most recent workspace snapshot
        4. Load new project's project-specific memories into working context
        5. Update active_context.current_pronoun_ref to null (references do not carry over)
        6. Open project in editor (via open_project tool)
        7. Report: "Switched to [project X]. Last session: [date]. Restoring [N] files."
        """
        now = int(time.time())
        current_pid, current_focus_time = self._get_current_focus_project()

        target_id, target_name, folder_path, default_editor, last_opened_at = self._resolve_project(target_project)
        editor = preferred_editor or default_editor or "vscode"

        # -------------------------------------------------------------
        # Step 1: Snapshot current workspace (Section 33) & Inspect Git Status
        # -------------------------------------------------------------
        auto_snapshot = None
        git_dirty_warning = None
        if current_pid and current_pid != target_id:
            # Check for uncommitted changes in previous project
            try:
                from assistant.git_tools import LocalGitService
                _, _, curr_fpath, _, _ = self._resolve_project(current_pid)
                if curr_fpath and os.path.exists(curr_fpath):
                    git_stat = LocalGitService().get_status(curr_fpath)
                    if git_stat.get("is_git") and git_stat.get("is_dirty"):
                        git_dirty_warning = (
                            f"⚠ Note: Previous project '{current_pid}' has uncommitted changes "
                            f"({git_stat['staged_count']} staged, {git_stat['unstaged_count']} unstaged files)."
                        )
                        logger.warning(git_dirty_warning)
            except Exception as git_err:
                logger.debug(f"Git pre-switch check skipped: {git_err}")

            if auto_snapshot_previous:
                try:
                    auto_snapshot = self.snapshot_manager.capture_snapshot(
                        project_id=current_pid,
                        snapshot_type="switch_auto",
                        notes=f"Auto-saved prior to switching to {target_name}"
                    )
                    logger.info(f"Step 1: Auto-saved workspace snapshot for '{current_pid}'.")
                except Exception as e:
                    logger.error(f"Failed capturing switch snapshot for '{current_pid}': {e}")

        # Check for long gap (>48 hours) for catch-up brief
        catchup_brief = None
        if last_opened_at and (now - last_opened_at) > self.LONG_GAP_SECONDS:
            catchup_brief = self.get_catchup_brief(target_id, force=True)

        # -------------------------------------------------------------
        # Step 2: Update current_focus.project_id to new project
        # -------------------------------------------------------------
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO current_focus (id, project_id, updated_at) VALUES (1, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET project_id = excluded.project_id, updated_at = excluded.updated_at;",
                    (target_id, now)
                )
                if folder_path:
                    conn.execute(
                        "UPDATE projects SET last_opened_at = ?, updated_at = ? WHERE id = ?;",
                        (now, now, target_id)
                    )
            logger.info(f"Step 2: Updated current_focus to '{target_id}'.")
        except Exception as e:
            logger.error(f"Failed updating current_focus table: {e}")

        # -------------------------------------------------------------
        # Step 3: Load new project's most recent workspace snapshot
        # -------------------------------------------------------------
        latest_snapshot = self.snapshot_manager.get_latest_snapshot(target_id)
        restored_files = []
        missing_files = []
        last_session_date = "No previous session"
        if latest_snapshot:
            last_session_date = time.strftime("%Y-%m-%d %H:%M", time.localtime(latest_snapshot.captured_at))
            for fpath in latest_snapshot.open_files:
                if os.path.exists(fpath):
                    restored_files.append(fpath)
                else:
                    missing_files.append(fpath)
        logger.info(f"Step 3: Loaded snapshot for '{target_id}' ({len(restored_files)} files).")

        # -------------------------------------------------------------
        # Step 4: Load new project's project memories into working context
        # -------------------------------------------------------------
        loaded_memories = []
        if self.memory_manager:
            try:
                loaded_memories = self.memory_manager.load_project_focus_context(
                    target_id,
                    context_store=self.context_store
                )
                logger.info(f"Step 4: Loaded {len(loaded_memories)} memories for '{target_id}'.")
            except Exception as e:
                logger.error(f"Failed loading project focus context for '{target_id}': {e}")

        # -------------------------------------------------------------
        # Step 5: Update active_context.current_pronoun_ref to null (Context Bleed Prevention)
        # -------------------------------------------------------------
        if self.context_store:
            try:
                self.context_store.delete("current_pronoun_ref")
                self.context_store.set("current_focus_project", target_id)
                self.context_store.set("active_project_folder", folder_path or "")
                logger.info(f"Step 5: Cleared pronoun references and updated active_project_folder for '{target_id}'.")
            except Exception as e:
                logger.error(f"Failed resetting active_context references: {e}")

        # -------------------------------------------------------------
        # Step 6: Open project in editor (via open_project tool / executor)
        # -------------------------------------------------------------
        opened_in_editor = False
        editor_msg = ""
        if folder_path and self.tool_executor:
            try:
                open_tool = self.tool_executor.tools.get("open_project")
                if open_tool:
                    await open_tool.execute(
                        self.tool_executor,
                        project_id=target_id,
                        project_name=target_name,
                        folder_path=folder_path,
                        preferred_editor=editor
                    )
                    opened_in_editor = True
                    editor_msg = f" Opened in {editor}."
            except Exception as e:
                logger.warning(f"Step 6: Editor launch skipped or failed for '{folder_path}': {e}")

        # -------------------------------------------------------------
        # Step 7: Build Report
        # -------------------------------------------------------------
        # Format: "Switched to [project X]. Last session: [date]. Restoring [N] files."
        report_lines = [
            f"Switched to {target_name}. Last session: {last_session_date}. Restoring {len(restored_files)} files.{editor_msg}"
        ]

        if loaded_memories:
            notes_preview = ", ".join([f'"{m.content[:30]}..."' for m in loaded_memories[:2]])
            report_lines.append(f"Loaded {len(loaded_memories)} project memories into working context ({notes_preview}).")

        if missing_files:
            report_lines.append(f"Note: {len(missing_files)} file(s) from last session no longer exist on disk.")

        if git_dirty_warning:
            report_lines.append(git_dirty_warning)

        if catchup_brief:
            report_lines.append("\n" + catchup_brief["formatted_brief"])

        response_text = "\n".join(report_lines)
        logger.info(f"Step 7: Context switch complete: {response_text[:80]}...")

        return {
            "status": "success",
            "project_id": target_id,
            "project_name": target_name,
            "previous_project": current_pid,
            "folder_path": folder_path,
            "last_session": last_session_date,
            "restored_files": restored_files,
            "restored_files_count": len(restored_files),
            "missing_files": missing_files,
            "memories_loaded_count": len(loaded_memories),
            "opened_in_editor": opened_in_editor,
            "catchup_brief": catchup_brief,
            "git_dirty_warning": git_dirty_warning,
            "response": response_text
        }

    def list_active_projects(self, max_days: int = 30) -> dict:
        """
        Multi-project awareness (Section 44):
        'active' means last_opened_at within max_days (default 30).
        Older projects are categorized as archived in the registry but not deleted.
        """
        now = int(time.time())
        cutoff = now - (max_days * 86400)
        active_list = []
        archived_list = []

        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, name, folder_path, preferred_editor, last_opened_at, created_at "
                    "FROM projects ORDER BY last_opened_at DESC, created_at DESC;"
                )
                rows = cursor.fetchall()
                for row in rows:
                    last_op = row["last_opened_at"]
                    p_info = {
                        "id": row["id"],
                        "name": row["name"],
                        "folder_path": row["folder_path"],
                        "preferred_editor": row["preferred_editor"],
                        "last_opened_at": last_op,
                        "last_opened_date": time.strftime("%Y-%m-%d %H:%M", time.localtime(last_op)) if last_op else "Never"
                    }
                    if last_op and last_op >= cutoff:
                        p_info["status"] = "active"
                        active_list.append(p_info)
                    else:
                        p_info["status"] = "archived"
                        archived_list.append(p_info)
        except Exception as e:
            logger.error(f"Error querying active projects: {e}")

        # Build text summary
        lines = [f"Project Status (Active window: {max_days} days):"]
        lines.append(f"Active Projects ({len(active_list)}):")
        if active_list:
            for p in active_list:
                lines.append(f"  - {p['name']} ({p['id']}): Last opened {p['last_opened_date']}")
        else:
            lines.append("  (None)")

        if archived_list:
            lines.append(f"\nArchived Projects ({len(archived_list)}):")
            for p in archived_list:
                lines.append(f"  - {p['name']} ({p['id']}): Last opened {p['last_opened_date']}")

        return {
            "status": "success",
            "active_count": len(active_list),
            "archived_count": len(archived_list),
            "active_projects": active_list,
            "archived_projects": archived_list,
            "response": "\n".join(lines)
        }

    def get_catchup_brief(self, project_id: str, force: bool = False) -> dict | None:
        """
        Long-Term Project Context (Section 45):
        When project resumed after >48 hours gap:
        1. Load project-specific memories
        2. Load most recent workspace snapshot
        3. Local model / heuristic synthesizes 3-5 bullet catch-up brief:
           - Last worked on: [X days ago]
           - Last action: [Summary from snapshot / memory]
           - Open blocker: [From blocker category memory]
           - Next step: [From decision / goal memory]
           - Files from last session: [list]
        """
        now = int(time.time())
        p_id, p_name, folder_path, _, last_opened_at = self._resolve_project(project_id)

        if not last_opened_at and not force:
            return None

        delta_sec = now - (last_opened_at or now)
        if delta_sec < self.LONG_GAP_SECONDS and not force:
            return None

        days_ago = max(1, int(delta_sec / 86400)) if last_opened_at else 0
        time_desc = f"{days_ago} days ago" if days_ago > 0 else "recently"

        # 1. Inspect latest snapshot
        snapshot = self.snapshot_manager.get_latest_snapshot(p_id)
        files_str = ", ".join([os.path.basename(f) for f in snapshot.open_files[:5]]) if snapshot and snapshot.open_files else "None recorded"
        branch_str = f" (Branch: {snapshot.active_branch})" if snapshot and snapshot.active_branch else ""

        # 2. Inspect project memories for blockers, conventions, and next steps
        mems = self.memory_manager.get_project_memories(p_id, limit=10) if self.memory_manager else []
        blocker = "No open blockers recorded"
        next_step = "Continue with pending roadmap tasks"
        last_action = f"Worked in branch '{snapshot.active_branch}'" if snapshot and snapshot.active_branch else "Edited workspace files"

        for m in mems:
            content_lower = m.content.lower()
            if m.category == "blocker" or "blocker" in content_lower or "waiting on" in content_lower:
                blocker = m.content
            elif "next step" in content_lower or "todo" in content_lower or m.category == "goal":
                next_step = m.content
            elif "last action" in content_lower or "completed" in content_lower or m.category == "decision":
                last_action = m.content

        # 3. Format catch-up brief
        brief_bullets = [
            f"- Last worked on: {time_desc}{branch_str}",
            f"- Last action: {last_action}",
            f"- Open blocker: {blocker}",
            f"- Next step: {next_step}",
            f"- Files from last session: {files_str}"
        ]
        formatted = "Catch-up Brief:\n" + "\n".join(brief_bullets)

        return {
            "project_id": p_id,
            "project_name": p_name,
            "days_ago": days_ago,
            "time_description": time_desc,
            "last_action": last_action,
            "open_blocker": blocker,
            "next_step": next_step,
            "files": files_str,
            "bullets": brief_bullets,
            "formatted_brief": formatted
        }


# =====================================================================
# Dedicated Tools (Section 44 & 45)
# =====================================================================

class SwitchProjectTool(Tool):
    """
    Executes graceful 7-step multi-project context switch per Section 44 of phase.md.
    """

    def __init__(self, switcher: ProjectContextSwitcher):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "project_name": {
                        "type": "string",
                        "description": "Name or ID of target project to switch to"
                    },
                    "preferred_editor": {
                        "type": "string",
                        "description": "Preferred editor (vscode, explorer, noop)"
                    }
                },
                "required": ["project_name"]
            },
            "side_effects": "Snapshots departing workspace, switches current_focus, restores workspace files and project memories",
            "timeout_ms": 6000,
            "memory_limit_mb": 50
        }
        super().__init__("switch_project", "reversible", declaration)
        self.switcher = switcher

    async def execute(self, executor, **kwargs) -> dict:
        target = kwargs.get("project_name") or kwargs.get("project_id") or ""
        editor = kwargs.get("preferred_editor")
        if not target:
            return {"status": "failure", "message": "Missing required 'project_name' parameter."}

        res = await self.switcher.switch_project(target, preferred_editor=editor)
        return res


class ListActiveProjectsTool(Tool):
    """
    Lists projects categorized as active (opened within 30 days) vs archived per Section 44.
    """

    def __init__(self, switcher: ProjectContextSwitcher):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "max_days": {
                        "type": "integer",
                        "default": 30,
                        "description": "Active window threshold in days"
                    }
                }
            },
            "side_effects": "none",
            "timeout_ms": 3000,
            "memory_limit_mb": 50
        }
        super().__init__("list_active_projects", "read_only", declaration)
        self.switcher = switcher

    async def execute(self, executor, **kwargs) -> dict:
        max_days = int(kwargs.get("max_days", 30))
        return self.switcher.list_active_projects(max_days=max_days)


class ProjectCatchupBriefTool(Tool):
    """
    Generates a 3-5 bullet catch-up brief for a project resumed after >48 hours per Section 45.
    """

    def __init__(self, switcher: ProjectContextSwitcher):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project ID or name (defaults to current project)"
                    },
                    "force": {
                        "type": "boolean",
                        "default": False,
                        "description": "Generate brief even if under 48 hours"
                    }
                }
            },
            "side_effects": "none",
            "timeout_ms": 3000,
            "memory_limit_mb": 50
        }
        super().__init__("project_catchup_brief", "read_only", declaration)
        self.switcher = switcher

    async def execute(self, executor, **kwargs) -> dict:
        p_id = kwargs.get("project_id") or kwargs.get("project_name")
        force_val = kwargs.get("force", True)
        if isinstance(force_val, str):
            force = force_val.lower() not in ("false", "0", "no")
        else:
            force = bool(force_val)

        if not p_id:
            # Fallback to current focus
            if hasattr(self.switcher, "_get_current_focus_project"):
                p_id, _ = self.switcher._get_current_focus_project()

        if not p_id:
            return {"status": "failure", "message": "No active project in focus."}

        brief = self.switcher.get_catchup_brief(p_id, force=force)
        if not brief:
            return {
                "status": "success",
                "response": f"Project '{p_id}' was worked on recently (under 48 hours ago). No catch-up brief needed.",
                "project_id": p_id
            }

        return {
            "status": "success",
            "project_id": p_id,
            "response": brief["formatted_brief"],
            "brief": brief
        }
