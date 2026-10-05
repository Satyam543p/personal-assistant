import os
import abc
import json
import uuid
import time
import subprocess
import logging
import fnmatch

logger = logging.getLogger("jarvis.tools")

try:
    from assistant.router import ToolExecutor
except ModuleNotFoundError:
    from router import ToolExecutor

from enum import Enum
from dataclasses import dataclass, field
from pathlib import Path

# =====================================================================
# Tool Contract & Error Standardization (Phase 2 Master Architecture)
# =====================================================================

class ErrorCode(str, Enum):
    NOT_FOUND = "NOT_FOUND"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    PATH_FORBIDDEN = "PATH_FORBIDDEN"
    NETWORK_ERROR = "NETWORK_ERROR"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    DISK_FULL = "DISK_FULL"
    RATE_LIMITED = "RATE_LIMITED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    DRM_PROTECTED = "DRM_PROTECTED"
    TOOL_MISSING = "TOOL_MISSING"
    CAPABILITY_GAP = "CAPABILITY_GAP"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    EXECUTION_FAILED = "EXECUTION_FAILED"

RETRYABLE_ERROR_CODES = {
    ErrorCode.NETWORK_ERROR,
    ErrorCode.TIMEOUT,
    ErrorCode.RATE_LIMITED,
}

@dataclass
class ToolResult:
    ok: bool
    data: dict = field(default_factory=dict)
    message: str = ""
    error_code: ErrorCode | None = None
    retryable: bool | None = None
    alternatives_tried: list[str] = field(default_factory=list)
    undo: dict | None = None
    warnings: list[str] = field(default_factory=list)
    status: str = "success"

    def __post_init__(self):
        if self.retryable is None:
            self.retryable = self.error_code in RETRYABLE_ERROR_CODES
        if not self.ok and self.status == "success":
            self.status = "failure"

    def to_dict(self) -> dict:
        code_val = self.error_code.value if isinstance(self.error_code, ErrorCode) else self.error_code
        res = {
            "ok": self.ok,
            "data": self.data,
            "message": self.message,
            "error_code": code_val,
            "retryable": bool(self.retryable),
            "alternatives_tried": self.alternatives_tried,
            "undo": self.undo,
            "warnings": self.warnings,
            "status": self.status,
            "response": self.message,  # backward compatibility
        }
        for k, v in self.data.items():
            if k not in res:
                res[k] = v
        return res


def normalize_tool_result(raw_result: dict | ToolResult) -> dict:
    """Normalizes any tool output dictionary to the unified ToolResult contract."""
    if isinstance(raw_result, ToolResult):
        return raw_result.to_dict()
    if not isinstance(raw_result, dict):
        return ToolResult(
            ok=True,
            data={"raw": raw_result},
            message=str(raw_result),
            status="success"
        ).to_dict()

    if "ok" in raw_result and "error_code" in raw_result and "message" in raw_result:
        return raw_result

    status = raw_result.get("status", "success")
    ok = raw_result.get("ok", status in ("success", "remediated", "thinking_remediated"))
    message = raw_result.get("message") or raw_result.get("response") or raw_result.get("summary") or ""
    error_code = raw_result.get("error_code")
    retryable = raw_result.get("retryable", error_code in RETRYABLE_ERROR_CODES if error_code else False)
    alternatives_tried = raw_result.get("alternatives_tried", [])
    undo = raw_result.get("undo")
    warnings = raw_result.get("warnings", [])

    data_payload = raw_result.get("data")
    if not isinstance(data_payload, dict):
        data_payload = {
            k: v for k, v in raw_result.items()
            if k not in ("ok", "status", "message", "response", "error_code", "retryable", "alternatives_tried", "undo", "warnings")
        }

    return ToolResult(
        ok=ok,
        data=data_payload,
        message=message,
        error_code=error_code,
        retryable=retryable,
        alternatives_tried=alternatives_tried,
        undo=undo,
        warnings=warnings,
        status=status
    ).to_dict()

# =====================================================================
# Tool Base Class
# =====================================================================

class Tool(abc.ABC):
    def __init__(self, name: str, risk_level: str, declaration: dict):
        self.name = name
        self.risk_level = risk_level
        self.declaration = declaration

    @abc.abstractmethod
    async def execute(self, executor, **kwargs) -> dict:
        """Execute the tool's operation. Returns a dict containing 'ok', 'data', 'message', 'status'."""
        pass

# =====================================================================
# Tool Subclasses
# =====================================================================

class OpenAppTool(Tool):
    def __init__(self):
        declaration = {
            "inputs": {
                "app_name": {
                    "type": "string",
                    "description": "Name of whitelisted application to launch (vscode, notepad, chrome, explorer, terminal)"
                }
            },
            "side_effects": "Spawns graphical user interface application",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("open_app", "reversible", declaration)

    async def execute(self, executor, **kwargs) -> dict:
        app_name = kwargs.get("app_name", "").lower().strip()
        if not app_name:
            raise ValueError("Application name parameter 'app_name' is required.")

        try:
            from assistant.launchers import app_launcher
            res = app_launcher.launch_by_name(app_name)
            return {
                "status": res.status,
                "response": res.message,
                "target": res.target,
                "mode": res.mode
            }
        except Exception as e:
            logger.error(f"Failed to launch app '{app_name}': {e}")
            raise ValueError(f"Could not launch application '{app_name}': {e}")

class OpenProjectTool(Tool):
    def __init__(self, memory_manager = None):
        declaration = {
            "inputs": {
                "project_name": {"type": "string"},
                "folder_path": {"type": "string"}
            },
            "side_effects": "Launches VS Code or Explorer targeting project root folder and loads focus context",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("open_project", "reversible", declaration)
        self.memory_manager = memory_manager

    async def execute(self, executor, **kwargs) -> dict:
        project_name = kwargs.get("project_name", "")
        folder_path = kwargs.get("folder_path", "")
        if not folder_path:
            raise ValueError("Folder path parameter 'folder_path' is required to open a project.")

        # Verify path safety containment
        if not executor.is_path_allowed(folder_path):
            raise PermissionError(f"Access denied to folder path: {folder_path}")

        editor = kwargs.get("preferred_editor", "vscode")
        if editor == "noop":
            logger.info(f"No-op editor specified, skipping external process launch for '{folder_path}'")
        elif editor == "vscode":
            cmd = ["cmd.exe", "/c", "code", folder_path]
            logger.info(f"Opening project folder '{folder_path}' in editor '{editor}'")
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            cmd = ["explorer.exe", folder_path]
            logger.info(f"Opening project folder '{folder_path}' in editor '{editor}'")
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Update last opened timestamp in project registry and current_focus
        project_id = kwargs.get("project_id") or project_name or os.path.basename(folder_path.rstrip(r"\/"))
        loaded_notes_msg = ""
        try:
            now = int(time.time())
            with executor.db.transaction() as conn:
                conn.execute(
                    "UPDATE projects SET last_opened_at = ?, updated_at = ? WHERE folder_path = ?;",
                    (now, now, folder_path)
                )
                conn.execute(
                    "INSERT INTO current_focus (id, project_id, updated_at) VALUES (1, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET project_id = excluded.project_id, updated_at = excluded.updated_at;",
                    (project_id, now)
                )

            # Pre-load project-specific memories into active_context (Section 43)
            mem_mgr = self.memory_manager or getattr(executor, "memory_manager", None)
            c_store = getattr(executor, "context_store", None)
            if mem_mgr and project_id:
                loaded = mem_mgr.load_project_focus_context(project_id, context_store=c_store)
                if loaded:
                    loaded_notes_msg = f" Loaded {len(loaded)} project-specific note(s) into active context."
        except Exception as e:
            logger.error(f"Failed updating project last_opened_at / current_focus: {e}")

        return {
            "status": "success",
            "response": f"Opened project folder '{folder_path}' in editor '{editor}'.{loaded_notes_msg}",
            "project_id": project_id
        }

class SearchFilesTool(Tool):
    def __init__(self):
        declaration = {
            "inputs": {
                "folder_path": {"type": "string"},
                "pattern": {"type": "string", "default": "*"}
            },
            "side_effects": "Scans filesystem subdirectories for matching patterns",
            "timeout_ms": 10000,
            "memory_limit_mb": 100
        }
        super().__init__("search_files", "read_only", declaration)

    async def execute(self, executor, **kwargs) -> dict:
        folder_path = kwargs.get("folder_path", "")
        pattern = kwargs.get("pattern", "*") or "*"
        if not folder_path:
            raise ValueError("Folder path parameter 'folder_path' is required to search.")

        # Containment check
        if not executor.is_path_allowed(folder_path):
            raise PermissionError(f"Access denied to folder path: {folder_path}")

        matches = []
        for root, _, filenames in os.walk(folder_path):
            for filename in fnmatch.filter(filenames, pattern):
                full_path = os.path.join(root, filename)
                matches.append(full_path)
                if len(matches) >= 100:  # Safety cap on result size
                    break
            if len(matches) >= 100:
                break

        return {
            "status": "success",
            "files": matches,
            "response": f"Found {len(matches)} matching files under '{folder_path}'."
        }

class ReadFileTool(Tool):
    def __init__(self):
        declaration = {
            "inputs": {
                "file_path": {"type": "string"}
            },
            "side_effects": "Reads contents of a local file",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("read_file", "read_only", declaration)

    async def execute(self, executor, **kwargs) -> dict:
        file_path = kwargs.get("file_path", "")
        if not file_path:
            raise ValueError("File path parameter 'file_path' is required to read.")

        # Containment check
        if not executor.is_path_allowed(file_path):
            raise PermissionError(f"Access denied to file path: {file_path}")

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        if os.path.isdir(file_path):
            raise IsADirectoryError(f"Path is a directory, not a file: {file_path}")

        # Read first 50KB to protect RAM limits
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(50 * 1024)

        try:
            from assistant.safety import DataSanitizer
            has_injection, inject_warn = DataSanitizer.detect_injection_attempt(content)
            if has_injection:
                logger.warning(f"File '{file_path}' contains potential prompt injection: {inject_warn}")
        except Exception:
            pass

        return {
            "status": "success",
            "content": content,
            "response": f"Read completed. Character length: {len(content)}."
        }

class WriteFileTool(Tool):
    def __init__(self):
        declaration = {
            "inputs": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
                "overwrite": {"type": "boolean", "default": True}
            },
            "side_effects": "Creates or overwrites a local file",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("write_file", "destructive", declaration)

    async def execute(self, executor, **kwargs) -> dict:
        file_path = kwargs.get("file_path", "")
        content = kwargs.get("content", "")
        overwrite = kwargs.get("overwrite", True)

        if not file_path:
            raise ValueError("File path parameter 'file_path' is required to write.")

        # Containment check
        if not executor.is_path_allowed(file_path):
            raise PermissionError(f"Access denied to file path: {file_path}")

        if os.path.exists(file_path) and not overwrite:
            raise FileExistsError(f"File already exists and overwrite is set to False: {file_path}")

        # Pre-overwrite quarantine backup to guarantee zero unrecoverable data loss
        if os.path.exists(file_path):
            try:
                from assistant.file_safety import LocalQuarantineManager
                LocalQuarantineManager().quarantine_file(file_path, action="overwrite")
            except Exception as q_err:
                logger.warning(f"Quarantine backup before overwrite failed: {q_err}")

        # Ensure directory folders exist
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        return {
            "status": "success",
            "response": f"Successfully wrote contents to file '{file_path}'."
        }

class AllowFolderTool(Tool):
    def __init__(self):
        declaration = {
            "inputs": {
                "folder_path": {
                    "type": "string",
                    "description": "Folder path to whitelist for assistant operations (e.g. D:\\Sem5)"
                }
            },
            "side_effects": "Permanently registers folder path in allowed_paths table",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("allow_folder", "reversible", declaration)

    async def execute(self, executor, **kwargs) -> dict:
        folder_path = kwargs.get("folder_path") or kwargs.get("path")
        if not folder_path:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.PERMISSION_DENIED,
                message="Folder path parameter 'folder_path' is required.",
                status="failure"
            ).to_dict()

        target = Path(folder_path).resolve(strict=False)
        if not target.exists():
            return ToolResult(
                ok=False,
                error_code=ErrorCode.NOT_FOUND,
                message=f"Directory '{target}' does not exist on disk.",
                status="failure"
            ).to_dict()

        norm_target_str = os.path.normcase(str(target))
        if norm_target_str in ("c:\\", "d:\\", "e:\\", "c:/", "d:/", "e:/"):
            return ToolResult(
                ok=False,
                error_code=ErrorCode.PERMISSION_DENIED,
                message="Cannot allow root drive directory directly.",
                status="failure"
            ).to_dict()

        try:
            with executor.db.transaction() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS allowed_paths (
                        id TEXT PRIMARY KEY,
                        folder_path TEXT UNIQUE NOT NULL,
                        created_at INTEGER NOT NULL
                    );
                """)
                conn.execute(
                    "INSERT INTO allowed_paths (id, folder_path, created_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(folder_path) DO NOTHING;",
                    (str(uuid.uuid4()), str(target), int(time.time()))
                )
            return ToolResult(
                ok=True,
                data={"folder_path": str(target)},
                message=f"Successfully allowed folder '{target}' for assistant file operations.",
                status="success"
            ).to_dict()
        except Exception as e:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.EXECUTION_FAILED,
                message=f"Failed to record allowed path: {e}",
                status="failure"
            ).to_dict()

# =====================================================================
# Tool Executor Component
# =====================================================================

def is_path_allowed(path: str, db=None) -> bool:
    """
    Validates that a path resides within allowed roots and is not inside blocked system directories.
    Resolves symlinks and Windows junctions, comparing paths case-insensitively.
    """
    if not path:
        return False

    try:
        target = Path(path).resolve(strict=False)
        norm_target_str = os.path.normcase(str(target))
    except Exception:
        return False

    # 1. Block bare drive roots (e.g. "c:\\", "d:\\")
    if target.parent == target or norm_target_str in ("c:\\", "d:\\", "e:\\", "c:/", "d:/", "e:/"):
        return False

    # 2. Block sensitive Windows system roots
    BLOCKED_SYSTEM_DIRS = [
        os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows")),
        os.path.normcase(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")),
        os.path.normcase(os.environ.get("ProgramFiles", r"C:\Program Files")),
        os.path.normcase(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        os.path.normcase(os.environ.get("ProgramData", r"C:\ProgramData")),
        os.path.normcase(os.path.join(str(Path.home()), "AppData", "Roaming", "Microsoft")),
    ]
    for blocked in BLOCKED_SYSTEM_DIRS:
        if blocked and norm_target_str.startswith(blocked):
            return False

    # 3. Standard allowed roots
    import tempfile
    allowed_roots = [
        Path.cwd().resolve(strict=False),
        Path(tempfile.gettempdir()).resolve(strict=False),
        (Path.home() / "Downloads").resolve(strict=False),
        (Path.home() / "Desktop").resolve(strict=False),
        (Path.home() / "Documents").resolve(strict=False),
        (Path.home() / ".gemini" / "antigravity").resolve(strict=False),
    ]

    # Add workspace root from config if available
    try:
        import assistant.config as config
        if hasattr(config, "WORKSPACE_ROOT") and config.WORKSPACE_ROOT:
            allowed_roots.append(Path(config.WORKSPACE_ROOT).resolve(strict=False))
    except Exception:
        pass

    # 4. Check standard roots
    for root in allowed_roots:
        norm_root_str = os.path.normcase(str(root))
        if norm_target_str.startswith(norm_root_str):
            return True

    # 5. Check registered projects and allowed custom paths in SQLite
    if db:
        try:
            with db.transaction() as conn:
                cursor = conn.cursor()
                # Projects table
                cursor.execute("SELECT folder_path FROM projects;")
                for row in cursor.fetchall():
                    p_path = os.path.normcase(str(Path(row["folder_path"]).resolve(strict=False)))
                    if norm_target_str.startswith(p_path):
                        return True
                # Allowed paths table
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='allowed_paths';")
                if cursor.fetchone():
                    cursor.execute("SELECT folder_path FROM allowed_paths;")
                    for row in cursor.fetchall():
                        a_path = os.path.normcase(str(Path(row["folder_path"]).resolve(strict=False)))
                        if norm_target_str.startswith(a_path):
                            return True
        except Exception as e:
            logger.debug(f"Error querying allowed database paths: {e}")

    return False


class JarvisToolExecutor(ToolExecutor):
    def __init__(self, db_manager):
        self.db = db_manager
        
        # Instantiate tool map
        self.tools = {
            "open_app": OpenAppTool(),
            "open_project": OpenProjectTool(),
            "search_files": SearchFilesTool(),
            "read_file": ReadFileTool(),
            "write_file": WriteFileTool()
        }

        # Dynamically load Pre-UI tools locally to avoid circular dependencies
        try:
            from assistant.telemetry import GetSystemTelemetryTool
            from assistant.notifications import SendNotificationTool
            from assistant.git_tools import GitStatusTool, GitDiffTool, GitCommitTool
            from assistant.indexer import IndexCodebaseTool, SearchCodebaseTool
            from assistant.file_safety import DeleteFileTool, RestoreQuarantinedFileTool
        except ModuleNotFoundError:
            from telemetry import GetSystemTelemetryTool
            from notifications import SendNotificationTool
            from git_tools import GitStatusTool, GitDiffTool, GitCommitTool
            from indexer import IndexCodebaseTool, SearchCodebaseTool
            from file_safety import DeleteFileTool, RestoreQuarantinedFileTool

        self.tools["get_system_telemetry"] = GetSystemTelemetryTool()
        self.tools["send_notification"] = SendNotificationTool()
        self.tools["git_status"] = GitStatusTool()
        self.tools["git_diff_summary"] = GitDiffTool()
        self.tools["git_commit"] = GitCommitTool()
        self.tools["index_codebase"] = IndexCodebaseTool()
        self.tools["search_codebase"] = SearchCodebaseTool()
        self.tools["delete_file"] = DeleteFileTool()
        self.tools["restore_quarantined_file"] = RestoreQuarantinedFileTool()
        self.tools["allow_folder"] = AllowFolderTool()
        
        self._seed_tools_registry()

    def register_tool(self, tool_name_or_instance, tool_instance=None):
        """Dynamically registers or updates an active tool in the executor."""
        if tool_instance is None and hasattr(tool_name_or_instance, "name"):
            tool_instance = tool_name_or_instance
            tool_name = tool_instance.name
        else:
            tool_name = tool_name_or_instance
        self.tools[tool_name] = tool_instance
        logger.info(f"Dynamically registered tool '{tool_name}' in JarvisToolExecutor.")

    def _seed_tools_registry(self):
        """Seeds or updates whitelisted tool declarations in SQLite tools table."""
        logger.info("Syncing Core Tools Declarations with SQLite database registry...")
        now = int(time.time())
        try:
            with self.db.transaction() as conn:
                for tool_name, tool in self.tools.items():
                    conn.execute(
                        "INSERT INTO tools (id, name, capability, version, risk_level, declaration, success_rate, last_used_at, created_at, updated_at, status) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(name) DO UPDATE SET declaration = excluded.declaration, risk_level = excluded.risk_level, updated_at = excluded.updated_at;",
                        (
                            tool_name,  # ID matches tool name for base core tools
                            tool_name,
                            tool.declaration.get("side_effects", "core"),
                            "1.0.0",
                            tool.risk_level,
                            json.dumps(tool.declaration),
                            1.0,
                            None,
                            now,
                            now,
                            "active"
                        )
                    )
            logger.info("Core Tools registry successfully seeded.")
        except Exception as e:
            logger.error(f"Failed to seed core tools database registry: {e}")

    def is_path_allowed(self, path: str) -> bool:
        return is_path_allowed(path, self.db)

    async def execute_tool(self, intent: str, entities: dict, resolved_reference: str) -> dict:
        # Map intents to registered core tools
        # (intent could be run_project which uses open_project tool, etc.)
        intent_map = {
            "open_app": "open_app",
            "open_project": "open_project",
            "run_project": "open_project",  # run project launches editor/command on path
            "file_management": "read_file",  # general fallback
            "system_control": "get_system_telemetry"
        }
        if "search_web" not in self.tools:
            intent_map["search_web"] = "search_files"
        
        tool_name = intent_map.get(intent, intent)
        if tool_name not in self.tools:
            # Check if intent name itself matches a tool
            if intent in self.tools:
                tool_name = intent
            else:
                raise ValueError(f"No executable tool matches intent/tool name '{intent}'")

        tool = self.tools[tool_name]
        logger.info(f"Executing tool '{tool_name}' for intent '{intent}'...")

        # Parse inputs by merging entities and resolved reference
        kwargs = {}
        if entities:
            # Map standard entity keys to tool expected arguments
            kwargs.update(entities)
            
        if resolved_reference:
            if tool_name == "open_app":
                kwargs["app_name"] = resolved_reference
            elif tool_name == "open_project":
                kwargs["folder_path"] = resolved_reference
            elif tool_name in ("search_files", "read_file", "write_file"):
                kwargs["file_path"] = resolved_reference
                kwargs["folder_path"] = resolved_reference
            elif tool_name == "store_memory":
                kwargs["content"] = kwargs.get("content") or resolved_reference
            elif tool_name == "query_memory":
                kwargs["query"] = kwargs.get("query") or resolved_reference
            elif tool_name == "memory_history":
                kwargs["memory_id"] = kwargs.get("memory_id") or resolved_reference
            elif tool_name == "search_youtube":
                kwargs["query"] = kwargs.get("query") or resolved_reference
            elif tool_name == "watch_video":
                kwargs["query"] = kwargs.get("query") or kwargs.get("video_id") or kwargs.get("url") or resolved_reference
            elif tool_name == "finish_video":
                if resolved_reference and resolved_reference != "finish_video":
                    kwargs["session_id"] = kwargs.get("session_id") or resolved_reference
            elif tool_name == "research_topic":
                kwargs["topic"] = kwargs.get("topic") or kwargs.get("query") or resolved_reference
            elif tool_name == "search_web":
                kwargs["query"] = kwargs.get("query") or kwargs.get("topic") or resolved_reference
            elif tool_name == "summarize_sources":
                kwargs["topic"] = kwargs.get("topic") or resolved_reference
            elif tool_name == "store_project_memory":
                kwargs["content"] = kwargs.get("content") or resolved_reference
            elif tool_name == "list_project_memories":
                kwargs["project_id"] = kwargs.get("project_id") or resolved_reference
            elif tool_name == "switch_project_focus":
                kwargs["project_id"] = kwargs.get("project_id") or resolved_reference
            elif tool_name == "switch_project":
                kwargs["project_name"] = kwargs.get("project_name") or kwargs.get("project_id") or resolved_reference
            elif tool_name == "list_active_projects":
                kwargs["max_days"] = kwargs.get("max_days", 30)
            elif tool_name == "project_catchup_brief":
                kwargs["project_id"] = kwargs.get("project_id") or kwargs.get("project_name") or resolved_reference
            elif tool_name in ("extract_subtitles", "extract_audio", "get_media_info", "download_video", "convert_media"):
                kwargs["source"] = kwargs.get("source") or kwargs.get("url") or kwargs.get("file_ref") or resolved_reference
            elif tool_name == "inspect_playlist":
                kwargs["url"] = kwargs.get("url") or kwargs.get("source") or resolved_reference
            elif tool_name == "transcribe_audio":
                kwargs["audio_path"] = kwargs.get("audio_path") or kwargs.get("source") or kwargs.get("file_ref") or resolved_reference
            elif tool_name == "speak_text":
                kwargs["text"] = kwargs.get("text") or kwargs.get("message") or resolved_reference
            elif tool_name == "toggle_voice_mode":
                if "enabled" not in kwargs:
                    kwargs["enabled"] = True
            elif tool_name == "crawl_website":
                kwargs["start_url"] = kwargs.get("start_url") or kwargs.get("url") or kwargs.get("source") or resolved_reference
            elif tool_name == "fetch_dynamic_page":
                kwargs["url"] = kwargs.get("url") or kwargs.get("source") or resolved_reference
            elif tool_name == "run_scheduled_job":
                kwargs["name"] = kwargs.get("name") or kwargs.get("job_name") or resolved_reference
            elif tool_name == "schedule_job":
                kwargs["name"] = kwargs.get("name") or resolved_reference
            elif tool_name == "toggle_job_status":
                kwargs["name"] = kwargs.get("name") or kwargs.get("job_name") or resolved_reference
            elif tool_name == "get_proactive_briefing":
                kwargs["feed_type"] = kwargs.get("feed_type") or resolved_reference or "morning_briefing"
            elif tool_name == "get_system_telemetry":
                if "check_safety" not in kwargs and resolved_reference in ("check_safety", "true", "True"):
                    kwargs["check_safety"] = True
            elif tool_name == "send_notification":
                kwargs["message"] = kwargs.get("message") or resolved_reference
            elif tool_name == "git_status":
                kwargs["repo_path"] = kwargs.get("repo_path") or resolved_reference or "."
            elif tool_name == "git_diff_summary":
                kwargs["repo_path"] = kwargs.get("repo_path") or resolved_reference or "."
            elif tool_name == "git_commit":
                kwargs["message"] = kwargs.get("message") or resolved_reference
            elif tool_name == "index_codebase":
                kwargs["folder_path"] = kwargs.get("folder_path") or resolved_reference or "."
            elif tool_name == "search_codebase":
                kwargs["query"] = kwargs.get("query") or resolved_reference
            elif tool_name == "delete_file":
                kwargs["file_path"] = kwargs.get("file_path") or resolved_reference
            elif tool_name == "restore_quarantined_file":
                kwargs["backup_id"] = kwargs.get("backup_id") or resolved_reference
            elif tool_name == "allow_folder":
                kwargs["folder_path"] = kwargs.get("folder_path") or resolved_reference

        # Remove keys with None values so tool defaults can apply
        kwargs = {k: v for k, v in kwargs.items() if v is not None}

        start_time = time.time()
        success = 1
        error_msg = None
        result = {}

        try:
            result = await tool.execute(self, **kwargs)
        except Exception as e:
            success = 0
            error_msg = str(e)
            logger.error(f"Tool '{tool_name}' failed during execution: {e}")
            raise e
        finally:
            duration_ms = int((time.time() - start_time) * 1000)
            
            # Log execution to SQLite tool_logs table
            log_id = str(uuid.uuid4())
            now = int(time.time())
            try:
                with self.db.transaction() as conn:
                    conn.execute(
                        "INSERT INTO tool_logs (id, tool_id, plan_step_id, input, output, success, error, duration_ms, executed_at, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                        (
                            log_id,
                            tool_name,
                            None,
                            json.dumps(kwargs),
                            json.dumps(result),
                            success,
                            error_msg,
                            duration_ms,
                            now,
                            now
                        )
                    )
                    
                    # Update tool metadata success rate and last used timestamp
                    cursor = conn.cursor()
                    cursor.execute("SELECT AVG(success) as avg_success FROM tool_logs WHERE tool_id = ?;", (tool_name,))
                    avg_row = cursor.fetchone()
                    avg_success = avg_row["avg_success"] if (avg_row and avg_row["avg_success"] is not None) else 1.0
                    
                    conn.execute(
                        "UPDATE tools SET success_rate = ?, last_used_at = ?, updated_at = ? WHERE id = ?;",
                        (avg_success, now, now, tool_name)
                    )
            except Exception as log_err:
                logger.error(f"Failed to record tool execution logs: {log_err}")

        return normalize_tool_result(result)
