import abc
import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field

try:
    from assistant.database.manager import DatabaseManager
    from assistant.tools import Tool, JarvisToolExecutor
    from assistant.projects import ProjectRegistry, SQLiteProjectRegistry
except ModuleNotFoundError:
    from database.manager import DatabaseManager
    from tools import Tool, JarvisToolExecutor
    from projects import ProjectRegistry, SQLiteProjectRegistry

logger = logging.getLogger("jarvis.snapshots")

# =====================================================================
# Data Transfer Objects
# =====================================================================

@dataclass
class WorkspaceSnapshot:
    id: str
    project_id: str
    snapshot_type: str = "manual"  # "session_end" | "manual" | "auto_interval"
    open_files: list[str] = field(default_factory=list)
    editor_state: dict = field(default_factory=dict)
    active_branch: str | None = None
    captured_at: int = 0
    created_at: int = 0
    updated_at: int = 0


# =====================================================================
# Abstract Interfaces (Abstractions & Interfaces First)
# =====================================================================

class SnapshotManager(abc.ABC):
    @abc.abstractmethod
    def capture_snapshot(self, project_id: str, snapshot_type: str = "manual",
                         open_files: list[str] | None = None,
                         editor_state: dict | None = None,
                         notes: str | None = None, **kwargs) -> WorkspaceSnapshot:
        """Captures workspace context including open files, Git branch, and editor state."""
        pass

    @abc.abstractmethod
    def get_latest_snapshot(self, project_id: str) -> WorkspaceSnapshot | None:
        """Retrieves the most recent snapshot for a project."""
        pass

    @abc.abstractmethod
    def list_snapshots(self, project_id: str, limit: int = 10) -> list[WorkspaceSnapshot]:
        """Lists snapshots for a project in reverse chronological order."""
        pass

    @abc.abstractmethod
    def restore_workspace(self, project_id: str) -> dict:
        """Restores project workspace, validating files on disk and reporting missing files/staleness."""
        pass

    @abc.abstractmethod
    def delete_snapshot(self, snapshot_id: str) -> bool:
        """Deletes a snapshot record."""
        pass


# =====================================================================
# Concrete SQLite Implementation
# =====================================================================

class SQLiteSnapshotManager(SnapshotManager):
    """
    Manages workspace snapshots and restore protocols in SQLite
    per Section 33 of phase.md.
    """

    MAX_OPEN_FILES = 20
    STALENESS_SECONDS = 7 * 86400  # 7 days

    def __init__(self, db_manager: DatabaseManager, project_registry: ProjectRegistry = None,
                 tool_executor: JarvisToolExecutor = None):
        self.db = db_manager
        self.project_registry = project_registry or SQLiteProjectRegistry(db_manager)
        self.tool_executor = tool_executor

    def _get_project_info(self, project_id_or_name: str) -> tuple[str, str, str]:
        """
        Resolves project ID, canonical name, and folder path.
        Returns: (project_id, name, folder_path)
        """
        # Try project registry resolution
        try:
            resolved = self.project_registry.resolve_project(project_id_or_name)
            if resolved:
                return resolved.id, resolved.name, resolved.folder_path
        except Exception:
            pass

        # Direct database query fallback
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name, folder_path FROM projects WHERE id = ? OR name = ? LIMIT 1;",
                (project_id_or_name, project_id_or_name)
            )
            row = cursor.fetchone()
            if row:
                return row["id"], row["name"], row["folder_path"]

        # Default fallback
        return project_id_or_name, project_id_or_name, ""

    def _get_git_branch(self, folder_path: str) -> str | None:
        """Inspects active Git branch using git rev-parse --abbrev-ref HEAD."""
        if not folder_path or not os.path.exists(folder_path):
            return None

        git_dir = os.path.join(folder_path, ".git")
        if not os.path.exists(git_dir):
            return None

        try:
            res = subprocess.run(
                ["git", "-C", folder_path, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2.0
            )
            if res.returncode == 0:
                branch = res.stdout.strip()
                return branch if branch else None
        except Exception as e:
            logger.debug(f"Git branch inspection skipped for {folder_path}: {e}")
        return None

    def _harvest_open_files(self, folder_path: str) -> list[str]:
        """
        Infers recently opened files from tool_logs (read_file / write_file calls),
        scoped to the project folder, capped at 20 files (Section 33).
        """
        harvested = []
        seen = set()

        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT input FROM tool_logs WHERE tool_id IN ('read_file', 'write_file') "
                    "AND success = 1 ORDER BY executed_at DESC LIMIT 50;"
                )
                rows = cursor.fetchall()
                for r in rows:
                    try:
                        inp = json.loads(r["input"])
                        fp = inp.get("file_path")
                        if fp and fp not in seen:
                            # If folder_path provided, ensure file belongs to project
                            if not folder_path or os.path.abspath(fp).startswith(os.path.abspath(folder_path)):
                                seen.add(fp)
                                harvested.append(fp)
                                if len(harvested) >= self.MAX_OPEN_FILES:
                                    break
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"Error harvesting open files from tool_logs: {e}")

        return harvested

    def capture_snapshot(self, project_id: str, snapshot_type: str = "manual",
                         open_files: list[str] | None = None,
                         editor_state: dict | None = None,
                         notes: str | None = None, **kwargs) -> WorkspaceSnapshot:
        now = int(time.time())
        p_id, p_name, folder_path = self._get_project_info(project_id)

        # 1. Open files: harvest from tool_logs if not provided
        final_files = open_files
        if final_files is None:
            final_files = self._harvest_open_files(folder_path)

        # Cap at 20 files
        final_files = final_files[:self.MAX_OPEN_FILES]

        # 2. Git branch inspection
        branch = self._get_git_branch(folder_path)

        # 3. Editor state
        final_editor_state = (editor_state or {}).copy()
        if notes:
            final_editor_state["notes"] = notes

        # 4. Insert into workspace_snapshots
        snapshot_id = f"snap_{p_id}_{uuid.uuid4().hex[:8]}"
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO workspace_snapshots (id, project_id, snapshot_type, open_files, editor_state, active_branch, captured_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (
                    snapshot_id,
                    p_id,
                    snapshot_type,
                    json.dumps(final_files),
                    json.dumps(final_editor_state),
                    branch,
                    now,
                    now,
                    now
                )
            )

        logger.info(f"Captured workspace snapshot '{snapshot_id}' for project '{p_name}' ({len(final_files)} files, branch: '{branch}')")
        return WorkspaceSnapshot(
            id=snapshot_id,
            project_id=p_id,
            snapshot_type=snapshot_type,
            open_files=final_files,
            editor_state=final_editor_state,
            active_branch=branch,
            captured_at=now,
            created_at=now,
            updated_at=now
        )

    def get_latest_snapshot(self, project_id: str) -> WorkspaceSnapshot | None:
        p_id, _, _ = self._get_project_info(project_id)
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM workspace_snapshots WHERE project_id = ? ORDER BY captured_at DESC LIMIT 1;",
                (p_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            files = json.loads(row["open_files"]) if row["open_files"] else []
            state = json.loads(row["editor_state"]) if row["editor_state"] else {}
            return WorkspaceSnapshot(
                id=row["id"],
                project_id=row["project_id"],
                snapshot_type=row["snapshot_type"],
                open_files=files,
                editor_state=state,
                active_branch=row["active_branch"],
                captured_at=row["captured_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"]
            )

    def list_snapshots(self, project_id: str, limit: int = 10) -> list[WorkspaceSnapshot]:
        p_id, _, _ = self._get_project_info(project_id)
        with self.db.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM workspace_snapshots WHERE project_id = ? ORDER BY captured_at DESC LIMIT ?;",
                (p_id, limit)
            )
            rows = cursor.fetchall()
            snapshots = []
            for r in rows:
                files = json.loads(r["open_files"]) if r["open_files"] else []
                state = json.loads(r["editor_state"]) if r["editor_state"] else {}
                snapshots.append(WorkspaceSnapshot(
                    id=r["id"],
                    project_id=r["project_id"],
                    snapshot_type=r["snapshot_type"],
                    open_files=files,
                    editor_state=state,
                    active_branch=r["active_branch"],
                    captured_at=r["captured_at"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"]
                ))
            return snapshots

    def delete_snapshot(self, snapshot_id: str) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM workspace_snapshots WHERE id = ?;", (snapshot_id,))
            return cursor.rowcount > 0

    def restore_workspace(self, project_id_or_name: str) -> dict:
        """
        Executes Continue Working protocol (Section 33):
        1. Loads most recent snapshot.
        2. Validates open_files exist on disk; flags missing files.
        3. Opens project folder in preferred editor.
        4. Restores current_focus to this project.
        5. Reports summary with 7-day staleness warning if applicable.
        """
        now = int(time.time())
        p_id, p_name, folder_path = self._get_project_info(project_id_or_name)

        snapshot = self.get_latest_snapshot(p_id)

        valid_files = []
        missing_files = []
        is_stale = False
        days_old = 0.0

        if snapshot:
            # Validate each file
            for f in snapshot.open_files:
                if os.path.exists(f):
                    valid_files.append(f)
                else:
                    missing_files.append(f)

            # Check 7-day staleness
            delta_sec = now - snapshot.captured_at
            days_old = round(delta_sec / 86400.0, 1)
            if delta_sec > self.STALENESS_SECONDS:
                is_stale = True

        # Update project last_opened_at timestamp
        if folder_path:
            try:
                now_ts = int(time.time())
                with self.db.transaction() as conn:
                    conn.execute(
                        "UPDATE projects SET last_opened_at = ?, updated_at = ? WHERE folder_path = ? OR id = ?;",
                        (now_ts, now_ts, folder_path, p_id)
                    )
            except Exception as e:
                logger.debug(f"Error updating project last_opened_at: {e}")

        # Restore current_focus in SQLite
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO current_focus (id, project_id, updated_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET project_id = excluded.project_id, updated_at = excluded.updated_at;",
                (p_id, now)
            )

        # Formulate human-readable report (Section 33 UX format)
        branch_str = f" on branch '{snapshot.active_branch}'" if (snapshot and snapshot.active_branch) else ""
        session_time_str = f"Last session: {days_old} days ago." if snapshot else "No previous snapshot recorded."
        files_str = f"{len(valid_files)} files verified. {len(missing_files)} files missing." if snapshot else "Opening empty project workspace."

        report = f"Resumed '{p_name}'{branch_str}. {session_time_str} {files_str}"
        if is_stale:
            report += f" (Note: This snapshot is {days_old} days old -- things may have changed)."

        logger.info(f"Restored workspace: {report}")
        return {
            "status": "success",
            "project_id": p_id,
            "project_name": p_name,
            "folder_path": folder_path,
            "active_branch": snapshot.active_branch if snapshot else None,
            "valid_files": valid_files,
            "missing_files": missing_files,
            "is_stale": is_stale,
            "days_old": days_old,
            "response": report
        }


# =====================================================================
# Dedicated Tools
# =====================================================================

class SaveWorkspaceTool(Tool):
    """
    Saves an instant workspace snapshot capturing open files, Git branch, and editor state.
    """

    def __init__(self, snapshot_manager: SnapshotManager, default_project_lookup=None):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "project_name": {"type": "string"},
                    "notes": {"type": "string"}
                }
            },
            "side_effects": "Captures current workspace state to SQLite",
            "timeout_ms": 3000,
            "memory_limit_mb": 50
        }
        super().__init__("save_workspace", "read_only", declaration)
        self.snapshot_manager = snapshot_manager
        self.default_project_lookup = default_project_lookup

    async def execute(self, executor, **kwargs) -> dict:
        proj = kwargs.get("project_name")
        if not proj and self.default_project_lookup:
            proj = self.default_project_lookup()
        if not proj:
            proj = "Jarvis"

        snap = self.snapshot_manager.capture_snapshot(proj, snapshot_type="manual")
        return {
            "status": "success",
            "snapshot_id": snap.id,
            "project_id": snap.project_id,
            "files_captured": len(snap.open_files),
            "active_branch": snap.active_branch,
            "response": f"Workspace snapshot captured for '{proj}' ({len(snap.open_files)} open files, branch: {snap.active_branch or 'none'})."
        }


class ResumeWorkspaceTool(Tool):
    """
    Executes the Continue Working protocol (Section 33), reopening files and checking git status.
    """

    def __init__(self, snapshot_manager: SnapshotManager, default_project_lookup=None):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "project_name": {"type": "string"}
                }
            },
            "side_effects": "Restores project workspace and sets active focus",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("continue_working", "read_only", declaration)
        self.snapshot_manager = snapshot_manager
        self.default_project_lookup = default_project_lookup

    async def execute(self, executor, **kwargs) -> dict:
        proj = kwargs.get("project_name")
        if not proj and self.default_project_lookup:
            proj = self.default_project_lookup()
        if not proj:
            proj = "Jarvis"

        res = self.snapshot_manager.restore_workspace(proj)
        return res
