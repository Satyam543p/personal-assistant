import os
import sys
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
        """Execute the tool's operation. Returns a dict containing 'status' and results."""
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

        ALLOWED_APPS = {"vscode", "notepad", "chrome", "explorer", "terminal", "browser", "youtube", "google", "calculator", "calc"}
        if app_name not in ALLOWED_APPS:
            raise ValueError(f"Application '{app_name}' is not in the allowed whitelist.")

        # Determine launch command mapping on Windows
        if app_name == "vscode":
            cmd = ["cmd.exe", "/c", "code"]
        elif app_name == "notepad":
            cmd = ["notepad.exe"]
        elif app_name in ("chrome", "browser", "google"):
            cmd = ["explorer.exe", "https://www.google.com"]
        elif app_name == "youtube":
            cmd = ["explorer.exe", "https://www.youtube.com"]
        elif app_name in ("calculator", "calc"):
            cmd = ["calc.exe"]
        elif app_name == "explorer":
            cmd = ["explorer.exe"]
        elif app_name == "terminal":
            cmd = ["powershell.exe"]
        else:
            raise ValueError(f"Launch command not configured for whitelisted app '{app_name}'")

        logger.info(f"Launching whitelisted app '{app_name}' with command: {cmd}")
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {
            "status": "success",
            "response": f"Launched whitelisted application '{app_name}'."
        }

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

# =====================================================================
# Tool Executor Component
# =====================================================================

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
        """Validates that a path resides within allowed workspace directories or active project directories."""
        if not path:
            return False
            
        abs_path = os.path.abspath(path).lower()
        
        # 1. Check workspace root containment
        workspace_root = os.path.abspath("c:/Users/Satyam Pandey/Desktop/personal assistent").lower()
        if abs_path.startswith(workspace_root):
            return True
            
        # 2. Check app data directory containment
        app_data_dir = os.path.abspath("C:/Users/Satyam Pandey/.gemini/antigravity").lower()
        if abs_path.startswith(app_data_dir):
            return True
            
        # 3. Check registered project directory containment
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT folder_path FROM projects;")
                for row in cursor.fetchall():
                    proj_path = os.path.abspath(row["folder_path"]).lower()
                    if abs_path.startswith(proj_path):
                        return True
        except Exception as e:
            logger.error(f"Error querying allowed project paths for prefix check: {e}")
            
        return False

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

        return result
