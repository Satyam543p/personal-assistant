import abc
import json
import os
import time
import logging

try:
    from assistant.projects import ProjectRegistry, ProjectMetadata, AmbiguousProjectError
    from assistant.safety import get_action_description, is_confirmation, is_negation, check_voice_safety_requirement
except ModuleNotFoundError:
    from projects import ProjectRegistry, ProjectMetadata, AmbiguousProjectError
    from safety import get_action_description, is_confirmation, is_negation, check_voice_safety_requirement

try:
    from assistant.apps_registry import app_registry
    from assistant.desktop_tools import MediaLauncher, WindowControl, ScreenshotTool, AudioMediaControl, SystemControl, FolderOrganizer, SongDownloader, PDFReportGenerator
    from assistant.self_healing import thinking_engine
except ImportError:
    pass

try:
    from assistant.context_engine import context_engine
except ModuleNotFoundError:
    try:
        from context_engine import context_engine
    except Exception:
        context_engine = None

logger = logging.getLogger("jarvis.router")


# =====================================================================
# Subsystem Abstractions (Abstractions & Interfaces First)
# =====================================================================

class MemoryManager(abc.ABC):
    @abc.abstractmethod
    async def query_memory(self, query: str, intent_data: dict) -> dict:
        """Query personal facts, preferences, goals, or recommendations from SQLite."""
        pass

class ToolExecutor(abc.ABC):
    @abc.abstractmethod
    async def execute_tool(self, intent: str, entities: dict, resolved_reference: str) -> dict:
        """Execute a safe, scoped capability tool."""
        pass

class PlanExecutor(abc.ABC):
    @abc.abstractmethod
    async def execute_plan(self, intent: str, entities: dict) -> dict:
        """Decompose a multi-step task and run sequentially."""
        pass

class CloudClient(abc.ABC):
    @abc.abstractmethod
    async def escalate(self, query: str, intent_data: dict) -> dict:
        """Escalate to high-reasoning cloud model."""
        pass

class ConversationHandler(abc.ABC):
    @abc.abstractmethod
    async def respond(self, query: str, intent_data: dict) -> dict:
        """Respond to conversation, coding help, or summarization tasks."""
        pass

class Router(abc.ABC):
    @abc.abstractmethod
    async def route(self, query: str, interpretation: dict) -> dict:
        """Route parsed intent structure to corresponding execution modules."""
        pass

class SQLiteContextStore:
    def __init__(self, db_manager):
        self.db = db_manager

    def get(self, key: str) -> str | None:
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM active_context WHERE key = ?;", (key,))
                row = cursor.fetchone()
                return row["value"] if row else None
        except Exception as e:
            logger.error(f"Error reading context key {key}: {e}")
            return None

    def set(self, key: str, value: str):
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO active_context (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;",
                    (key, value, int(time.time()))
                )
        except Exception as e:
            logger.error(f"Error setting context key {key} to {value}: {e}")

    def delete(self, key: str):
        try:
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM active_context WHERE key = ?;", (key,))
        except Exception as e:
            logger.error(f"Error deleting context key {key}: {e}")

# =====================================================================
# Concrete Router Engine Implementation
# =====================================================================

class JarvisRouter(Router):
    def __init__(self, db_manager, context_store, project_registry, memory_manager, 
                 tool_executor, plan_executor, cloud_client, conversation_handler, 
                 config=None, recommendation_engine=None, tool_lifecycle=None, growth_engine=None,
                 snapshot_manager=None, profile_manager=None, project_switcher=None,
                 media_extractor=None, voice_subsystem=None,
                 crawler=None, dynamic_fetcher=None,
                 scheduler=None, proactive_engine=None):
        self.db = db_manager
        self.context_store = context_store
        self.project_registry = project_registry
        self.memory_manager = memory_manager
        self.tool_executor = tool_executor
        self.plan_executor = plan_executor
        self.cloud_client = cloud_client
        self.conversation_handler = conversation_handler
        self.recommendation_engine = recommendation_engine
        self.tool_lifecycle = tool_lifecycle
        self.growth_engine = growth_engine
        self.snapshot_manager = snapshot_manager
        self.profile_manager = profile_manager
        self.project_switcher = project_switcher
        self.media_extractor = media_extractor
        self.voice_subsystem = voice_subsystem
        self.crawler = crawler
        self.dynamic_fetcher = dynamic_fetcher
        self.scheduler = scheduler
        self.proactive_engine = proactive_engine
        
        # Load routing thresholds (with fallbacks if configuration class is missing values)
        self.threshold_low = getattr(config, "THRESHOLD_LOW", 0.55)
        self.threshold_high = getattr(config, "THRESHOLD_HIGH", 0.82)
        self.cloud_threshold = getattr(config, "CLOUD_THRESHOLD", 0.65)
        self.max_clarify_rounds = getattr(config, "MAX_CLARIFY_ROUNDS", 2)

    async def route(self, query: str, interpretation: dict) -> dict:
        intent = interpretation.get("intent", "conversation")
        confidence = interpretation.get("confidence", 0.5)
        logger.info(f"Routing query: '{query}' -> parsed intent: '{intent}' (Confidence: {confidence})")

        # -------------------------------------------------------------
        # Confirmation Loop Resolution
        # -------------------------------------------------------------
        pending_action_str = self.context_store.get("pending_action")
        if pending_action_str:
            logger.info("Active confirmation pending. Checking user response.")
            try:
                pending_action = json.loads(pending_action_str)
                if is_confirmation(query):
                    self.context_store.delete("pending_action")
                    intent = pending_action["intent"]
                    entities = pending_action["entities"]
                    resolved_ref = pending_action["resolved_reference"]
                    
                    try:
                        return await self._execute_tool_safely(intent, entities, resolved_ref, bypass_confirm=True)
                    except Exception as e:
                        return await self._handle_routing_failure(e, f"tool:{intent}", query, interpretation)
                elif is_negation(query):
                    self.context_store.delete("pending_action")
                    logger.info("User cancelled the action.")
                    return {
                        "status": "failure",
                        "response": "Action cancelled.",
                        "route": "confirmation_cancelled"
                    }
                else:
                    action_desc = pending_action.get("action_description", "execute this action")
                    return {
                        "status": "confirm",
                        "response": f"⚠ This will {action_desc}. Confirm? (yes/no)",
                        "route": "confirmation_prompt"
                    }
            except Exception as e:
                logger.error(f"Error handling pending action confirmation: {e}")
                self.context_store.delete("pending_action")

        # -------------------------------------------------------------
        # Clarification Loop Merging Heuristics
        # -------------------------------------------------------------
        pending_intent_str = self.context_store.get("pending_intent")
        if pending_intent_str:
            try:
                pending_intent = json.loads(pending_intent_str)
                logger.info("Active clarification prompt found. Attempting resolution with user input.")

                # If user asks a new independent query (e.g. conversation, battery, git, web search), cancel stale pending intent
                if intent in ("conversation", "get_system_telemetry", "git_status", "search_web") or (intent != pending_intent.get("intent") and confidence >= 0.75):
                    logger.info(f"New independent intent '{intent}' cancels stale pending clarification. Clearing pending_intent.")
                    self.context_store.delete("pending_intent")
                    self.context_store.delete("clarify_rounds")
                else:
                    resolved = self._attempt_clarification_resolution(pending_intent, query, interpretation)
                    if resolved:
                        self.context_store.delete("pending_intent")
                        self.context_store.delete("clarify_rounds")
                        interpretation = resolved
                        intent = interpretation.get("intent")
                        confidence = interpretation.get("confidence", 0.9)
                        logger.info(f"Clarification successfully resolved. Routing merged intent: {intent}")
                    else:
                        rounds_str = self.context_store.get("clarify_rounds")
                    rounds = int(rounds_str) if rounds_str else 1
                    if rounds >= self.max_clarify_rounds:
                        self.context_store.delete("pending_intent")
                        self.context_store.delete("clarify_rounds")
                        logger.warning(f"Clarification failed. MAX_CLARIFY_ROUNDS ({self.max_clarify_rounds}) reached.")
                        return {
                            "status": "failure",
                            "response": "I couldn't clarify the command. What else would you like to do?",
                            "route": "clarification_failed"
                        }
                    else:
                        self.context_store.set("clarify_rounds", str(rounds + 1))
                        question = pending_intent.get("clarification_question") or "Please clarify your command."
                        return {
                            "status": "clarify",
                            "response": question,
                            "route": "clarification_pending"
                        }
            except Exception as e:
                logger.error(f"Error handling pending intent resolution: {e}")
                self.context_store.delete("pending_intent")
                self.context_store.delete("clarify_rounds")

        # -------------------------------------------------------------
        # 1. Confidence < THRESHOLD_LOW -> Trigger Clarification Prompt
        # -------------------------------------------------------------
        if confidence < self.threshold_low or interpretation.get("requires_clarification") or interpretation.get("needs_clarification"):
            logger.info(f"Low confidence ({confidence} < {self.threshold_low}). Triggering clarification.")
            question = interpretation.get("clarification_question") or "I'm not sure what you mean. Could you please clarify?"
            
            # Save pending state in active context
            self.context_store.set("pending_intent", json.dumps(interpretation))
            self.context_store.set("clarify_rounds", "1")
            
            return {
                "status": "clarify",
                "response": question,
                "route": "clarification_prompt"
            }

        # -------------------------------------------------------------
        # 2. Conversation Intent (Casual Chat / Open-ended Questions)
        # -------------------------------------------------------------
        if intent == "conversation":
            logger.info("Routing conversation intent directly to ConversationHandler.")
            try:
                res = await self.conversation_handler.respond(query, interpretation)
                self._write_routing_state(intent, "conversation")
                if isinstance(res, dict):
                    res["route"] = res.get("route") or "local_model"
                return res
            except Exception as e:
                return await self._handle_routing_failure(e, "conversation", query, interpretation)

        # -------------------------------------------------------------
        # 3 & 4. Direct Tool Intents
        # -------------------------------------------------------------
        DIRECT_TOOL_INTENTS = {
            "play_media", "multi_task", "open_project", "open_app", "run_project", "search_web", "write_file",
            "read_file", "search_files", "file_management", "execute_tool",
            "growth_dashboard", "save_workspace", "continue_working", "resume_workspace",
            "switch_profile", "current_profile",
            "accept_recommendation", "reject_recommendation", "tune_recommendation_weights",
            "store_memory", "query_memory", "memory_history", "prune_memories",
            "search_youtube", "watch_video", "finish_video",
            "research_topic", "summarize_sources",
            "store_project_memory", "list_project_memories", "switch_project_focus",
            "switch_project", "list_active_projects", "project_catchup_brief",
            "extract_subtitles", "extract_audio", "get_media_info",
            "download_video", "download_content", "download_song", "download_music",
            "convert_media", "inspect_playlist",
            "take_screenshot", "show_desktop", "switch_window",
            "control_media", "control_volume", "lock_workstation",
            "organize_folder", "generate_pdf",
            "transcribe_audio", "speak_text", "toggle_voice_mode",
            "crawl_website", "fetch_dynamic_page",
            "list_scheduled_jobs", "schedule_job", "run_scheduled_job",
            "get_proactive_briefing", "toggle_job_status",
            "get_system_telemetry", "system_control", "send_notification",
            "git_status", "git_diff_summary", "git_commit",
            "index_codebase", "search_codebase",
            "delete_file", "restore_quarantined_file"
        }
        if intent in DIRECT_TOOL_INTENTS:
            resolved_ref = interpretation.get("resolved_reference")
            entities = interpretation.get("entities", {})
            
            if not resolved_ref:
                if intent in ("open_project", "run_project"):
                    p_name = entities.get("project_name")
                    try:
                        resolved_proj = self.project_registry.resolve_project(p_name)
                        resolved_ref = resolved_proj.folder_path if resolved_proj else None
                        interpretation["resolved_reference"] = resolved_ref
                    except AmbiguousProjectError as err:
                        names = [p.name for p in err.candidates]
                        question = f"I found multiple matches: {', '.join(names)}. Which one did you mean?"
                        interpretation["clarification_question"] = question
                        self.context_store.set("pending_intent", json.dumps(interpretation))
                        self.context_store.set("clarify_rounds", "1")
                        return {
                            "status": "clarify",
                            "response": question,
                            "route": "clarification_prompt"
                        }
                elif intent == "play_media":
                    resolved_ref = entities.get("title") or query
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "multi_task":
                    resolved_ref = "multi_task"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "open_app":
                    a_name = entities.get("app_name")
                    if a_name:
                        resolved_ref = a_name
                        interpretation["resolved_reference"] = resolved_ref
                elif intent in ("write_file", "read_file", "search_files", "file_management"):
                    file_path = entities.get("file_ref") or entities.get("file_path")
                    if file_path:
                        # Context Bleed Prevention (Section 44):
                        # If path is relative or bare filename, resolve within active project folder
                        if not os.path.isabs(file_path):
                            active_folder = self.context_store.get("active_project_folder")
                            if not active_folder:
                                try:
                                    with self.db.transaction() as conn:
                                        cur = conn.cursor()
                                        cur.execute("SELECT p.folder_path FROM current_focus cf JOIN projects p ON cf.project_id = p.id WHERE cf.id = 1;")
                                        cf_row = cur.fetchone()
                                        if cf_row and cf_row["folder_path"]:
                                            active_folder = cf_row["folder_path"]
                                except Exception:
                                    pass
                            if active_folder:
                                candidate = os.path.join(active_folder, file_path)
                                if os.path.exists(candidate) or intent == "write_file":
                                    file_path = candidate
                        resolved_ref = file_path
                        interpretation["resolved_reference"] = resolved_ref
                elif intent == "growth_dashboard":
                    resolved_ref = "growth_dashboard"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent in ("save_workspace", "continue_working", "resume_workspace"):
                    p_name = entities.get("project_name")
                    if not p_name or p_name in ("", "unknown"):
                        # Fallback to current focus in SQLite
                        try:
                            with self.db.transaction() as conn:
                                cursor = conn.cursor()
                                cursor.execute("SELECT project_id FROM current_focus WHERE id = 1;")
                                row = cursor.fetchone()
                                p_name = row["project_id"] if row else "Jarvis"
                        except Exception:
                            p_name = "Jarvis"
                    entities["project_name"] = p_name
                    resolved_ref = p_name
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "switch_profile":
                    prof_name = entities.get("profile_name") or "coding"
                    resolved_ref = prof_name
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "current_profile":
                    resolved_ref = "current_profile"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent in ("accept_recommendation", "reject_recommendation"):
                    rec_id = entities.get("recommendation_id") or entities.get("rec_id") or "1"
                    resolved_ref = str(rec_id)
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "tune_recommendation_weights":
                    resolved_ref = "tune_recommendation_weights"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "store_memory":
                    content = entities.get("content") or query
                    resolved_ref = content
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "query_memory":
                    q = entities.get("query") or entities.get("topic") or query
                    resolved_ref = q
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "memory_history":
                    topic = entities.get("topic") or entities.get("query") or entities.get("memory_id") or query
                    resolved_ref = str(topic)
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "prune_memories":
                    resolved_ref = "prune_memories"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "search_youtube":
                    q = entities.get("query") or entities.get("topic") or query
                    resolved_ref = q
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "watch_video":
                    target = entities.get("query") or entities.get("video_id") or entities.get("url") or entities.get("topic") or query
                    resolved_ref = target
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "finish_video":
                    resolved_ref = "finish_video"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "research_topic":
                    target = entities.get("topic") or entities.get("query") or query
                    resolved_ref = target
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "search_web":
                    q = entities.get("query") or entities.get("topic") or query
                    resolved_ref = q
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "summarize_sources":
                    resolved_ref = "summarize_sources"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "store_project_memory":
                    p_id = entities.get("project_id", "current")
                    if p_id == "current":
                        try:
                            with self.db.transaction() as conn:
                                cursor = conn.cursor()
                                cursor.execute("SELECT project_id FROM current_focus WHERE id = 1;")
                                row = cursor.fetchone()
                                p_id = row["project_id"] if row and row["project_id"] else "default"
                        except Exception:
                            p_id = "default"
                    resolved_proj = self.project_registry.resolve_project(p_id)
                    actual_pid = resolved_proj.name if resolved_proj else p_id
                    entities["project_id"] = actual_pid
                    content = entities.get("content") or query
                    resolved_ref = content
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "list_project_memories":
                    p_id = entities.get("project_id", "current")
                    if p_id == "current":
                        try:
                            with self.db.transaction() as conn:
                                cursor = conn.cursor()
                                cursor.execute("SELECT project_id FROM current_focus WHERE id = 1;")
                                row = cursor.fetchone()
                                p_id = row["project_id"] if row and row["project_id"] else "default"
                        except Exception:
                            p_id = "default"
                    resolved_proj = self.project_registry.resolve_project(p_id)
                    actual_pid = resolved_proj.name if resolved_proj else p_id
                    entities["project_id"] = actual_pid
                    resolved_ref = actual_pid
                    interpretation["resolved_reference"] = resolved_ref
                elif intent in ("switch_project_focus", "switch_project"):
                    p_id = entities.get("project_name") or entities.get("project_id", "current")
                    resolved_proj = self.project_registry.resolve_project(p_id)
                    actual_pid = resolved_proj.name if resolved_proj else p_id
                    entities["project_name"] = actual_pid
                    entities["project_id"] = actual_pid
                    resolved_ref = actual_pid
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "list_active_projects":
                    resolved_ref = "list_active_projects"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "project_catchup_brief":
                    p_id = entities.get("project_name") or entities.get("project_id") or "current"
                    resolved_ref = p_id
                    interpretation["resolved_reference"] = resolved_ref
                elif intent in ("extract_subtitles", "extract_audio", "get_media_info", "download_video", "convert_media", "inspect_playlist"):
                    resolved_ref = entities.get("source") or entities.get("url") or entities.get("file_ref") or "unknown"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent in ("download_content", "download_song", "download_music"):
                    q = entities.get("query") or entities.get("title") or entities.get("topic") or entities.get("source") or query
                    resolved_ref = q
                    interpretation["resolved_reference"] = resolved_ref
                    if intent in ("download_song", "download_music"):
                        intent = "download_song"
                        interpretation["intent"] = "download_song"
                    else:
                        interpretation["intent"] = "download_video"
                        intent = "download_video"
                elif intent == "transcribe_audio":
                    resolved_ref = entities.get("audio_path") or entities.get("source") or entities.get("file_ref") or "unknown"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "speak_text":
                    resolved_ref = entities.get("text") or entities.get("message") or "spoken message"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "toggle_voice_mode":
                    resolved_ref = "voice_mode"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent in ("crawl_website", "fetch_dynamic_page"):
                    resolved_ref = entities.get("start_url") or entities.get("url") or entities.get("source") or "unknown"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent in ("run_scheduled_job", "toggle_job_status", "schedule_job"):
                    resolved_ref = entities.get("job_name") or entities.get("name") or "unknown"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "list_scheduled_jobs":
                    resolved_ref = "scheduled_jobs"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "get_proactive_briefing":
                    resolved_ref = entities.get("feed_type") or "morning_briefing"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent in ("get_system_telemetry", "system_control"):
                    resolved_ref = "get_system_telemetry"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "send_notification":
                    resolved_ref = entities.get("message") or entities.get("title") or "Jarvis Notification"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent in ("git_status", "git_diff_summary"):
                    resolved_ref = entities.get("repo_path") or entities.get("folder_path") or "."
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "git_commit":
                    resolved_ref = entities.get("message") or "Update"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "index_codebase":
                    resolved_ref = entities.get("folder_path") or entities.get("path") or "."
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "search_codebase":
                    resolved_ref = entities.get("query") or entities.get("search_query") or "query"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "delete_file":
                    resolved_ref = entities.get("file_path") or entities.get("path") or "file"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "restore_quarantined_file":
                    resolved_ref = entities.get("backup_id") or "backup_id"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent in ("take_screenshot", "show_desktop", "lock_workstation", "organize_folder"):
                    resolved_ref = intent
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "switch_window":
                    resolved_ref = entities.get("window_title") or "app"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "control_media":
                    resolved_ref = entities.get("action") or "play_pause"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "control_volume":
                    resolved_ref = entities.get("action") or "toggle_mute"
                    interpretation["resolved_reference"] = resolved_ref
                elif intent == "generate_pdf":
                    resolved_ref = entities.get("content") or query
                    interpretation["resolved_reference"] = resolved_ref

            # High confidence & resolved -> call tool safely
            if resolved_ref and confidence >= self.threshold_high:
                logger.info(f"Direct tool execution with resolved reference: {resolved_ref}")
                try:
                    return await self._execute_tool_safely(intent, entities, resolved_ref, metadata=interpretation.get("metadata"))
                except Exception as e:
                    return await self._handle_routing_failure(e, f"tool:{intent}", query, interpretation)
            
            # Unresolved reference or medium confidence -> try database/context fallback
            else:
                logger.info(f"Direct tool intent has unresolved reference or medium confidence. Fetching defaults.")
                fallback_found = False
                
                if intent in ("open_project", "run_project") and not resolved_ref:
                    p_name = entities.get("project_name")
                    if not p_name or p_name in ("", "unknown"):
                        # Lookup current active focus project
                        try:
                            with self.db.transaction() as conn:
                                cursor = conn.cursor()
                                cursor.execute("SELECT project_id FROM current_focus WHERE id = 1;")
                                row = cursor.fetchone()
                                focus_project_id = row["project_id"] if row else None
                                if focus_project_id:
                                    cursor.execute("SELECT folder_path FROM projects WHERE id = ?;", (focus_project_id,))
                                    proj_row = cursor.fetchone()
                                    if proj_row:
                                        resolved_ref = proj_row["folder_path"]
                                        entities["project_name"] = focus_project_id
                                        interpretation["resolved_reference"] = resolved_ref
                                        fallback_found = True
                                        logger.info(f"Fallback project resolved from current focus: {focus_project_id} -> {resolved_ref}")
                        except Exception as e:
                            logger.error(f"Error querying fallback current_focus: {e}")
                
                if resolved_ref:
                    # Executing tool with fallback
                    try:
                        return await self._execute_tool_safely(intent, entities, resolved_ref)
                    except Exception as e:
                        return await self._handle_routing_failure(e, f"tool:{intent}", query, interpretation)
                else:
                    # Unresolvable. Issue clarification.
                    question = f"Which project would you like to run?" if intent in ("open_project", "run_project") else "Please clarify which program or file you want to open."
                    self.context_store.set("pending_intent", json.dumps(interpretation))
                    self.context_store.set("clarify_rounds", "1")
                    return {
                        "status": "clarify",
                        "response": question,
                        "route": "clarification_prompt"
                    }

        # -------------------------------------------------------------
        # 5. Multi-Step Task -> Planner
        # -------------------------------------------------------------
        if intent == "multi_step_task":
            logger.info("Routing multi_step_task to PlanExecutor.")
            try:
                entities = dict(interpretation.get("entities", {}))
                if not entities.get("goal"):
                    entities["goal"] = query
                res = await self.plan_executor.execute_plan(intent, entities)
                self._write_routing_state(intent, "planner")
                if isinstance(res, dict):
                    res["route"] = res.get("route") or "planner"
                return res
            except Exception as e:
                return await self._handle_routing_failure(e, "planner", query, interpretation)

        # -------------------------------------------------------------
        # 5b. Tool Lifecycle / Capability Discovery
        # -------------------------------------------------------------
        if intent == "create_tool":
            logger.info("Routing create_tool intent to ToolLifecycleManager.")
            try:
                if not self.tool_lifecycle:
                    return {"status": "failure", "error": "ToolLifecycleManager is not configured.", "route": "tool_lifecycle"}
                gaps = self.tool_lifecycle.discover_capability_gaps()
                res = {
                    "status": "success",
                    "response": f"Tool Lifecycle Manager active. Capability gaps detected: {len(gaps)}.",
                    "gaps": gaps,
                    "route": "tool_lifecycle"
                }
                self._write_routing_state(intent, "tool_lifecycle")
                return res
            except Exception as e:
                return await self._handle_routing_failure(e, "tool_lifecycle", query, interpretation)

        # -------------------------------------------------------------
        # 5c. Growth Engine Query
        # -------------------------------------------------------------
        if intent == "growth_query":
            logger.info("Routing growth_query intent to GrowthEngine.")
            try:
                if not self.growth_engine:
                    return {"status": "failure", "error": "GrowthEngine is not configured.", "route": "growth"}
                dashboard = self.growth_engine.generate_growth_dashboard()
                res = {
                    "status": "success",
                    "response": dashboard["formatted_dashboard"],
                    "dashboard": dashboard,
                    "route": "growth"
                }
                self._write_routing_state(intent, "growth")
                return res
            except Exception as e:
                return await self._handle_routing_failure(e, "growth", query, interpretation)

        # -------------------------------------------------------------
        # 6. Cloud Escalation Heuristics (research or complex queries)
        # -------------------------------------------------------------
        cloud_score = 0.0
        if intent == "research":
            cloud_score = 1.0
        elif len(query.split()) > 15 and intent in ("coding_help", "multi_step_task"):
            # Multi-word queries in complex coding tasks trigger cloud score scaling
            cloud_score = 0.75

        if intent == "research" or cloud_score > self.cloud_threshold:
            logger.info(f"Routing to CloudClient. Calculated complexity score: {cloud_score}")
            try:
                res = await self.cloud_client.escalate(query, interpretation)
                self._write_routing_state(intent, "cloud")
                if isinstance(res, dict):
                    res["route"] = res.get("route") or "cloud"
                return res
            except Exception as e:
                return await self._handle_routing_failure(e, "cloud", query, interpretation)

        # 7. Memory Queries & Recommendations
        # -------------------------------------------------------------
        if intent in ("memory_query", "store_memory", "recommend_next_action"):
            logger.info("Routing memory queries to MemoryManager.")
            try:
                if intent == "recommend_next_action" and self.recommendation_engine:
                    res = await self.recommendation_engine.get_recommendations()
                    if isinstance(res, dict) and "recommendations" in res:
                        try:
                            self.context_store.set("last_recommendations", json.dumps(res["recommendations"]))
                        except Exception:
                            pass
                else:
                    mem_res = await self.memory_manager.query_memory(query, interpretation)

                    # Identity / personal queries → synthesize through LLM with memory context
                    _IDENTITY_PATTERNS = (
                        "who am i", "tell me about myself", "what do you know about me",
                        "my profile", "what do you know about satyam", "about me",
                        "my identity", "who is satyam", "describe me", "introduce me",
                        "what have you remembered about me",
                    )
                    query_lower = query.lower().strip()
                    is_identity_query = any(p in query_lower for p in _IDENTITY_PATTERNS)

                    if is_identity_query and mem_res.get("memories"):
                        # Build a memory context string from retrieved facts
                        mem_facts = "\n".join(
                            f"• {m['content']}" for m in mem_res["memories"][:10]
                        )
                        synthesis_query = (
                            f"The user asked: \"{query}\"\n\n"
                            f"Here are the facts I have stored about Satyam:\n{mem_facts}\n\n"
                            "Please respond warmly and naturally, in first person, introducing what you know about Satyam. "
                            "Do not just list the facts mechanically — weave them into a friendly, conversational reply."
                        )
                        logger.info("Identity query detected — routing to ConversationHandler for LLM synthesis with memory context.")
                        synthesis_interpretation = dict(interpretation)
                        synthesis_interpretation["intent"] = "conversation"
                        synthesis_interpretation["memory_context"] = mem_facts
                        res = await self.conversation_handler.respond(synthesis_query, synthesis_interpretation)
                        self._write_routing_state(intent, "memory_synthesis")
                        if isinstance(res, dict):
                            res["route"] = "memory_synthesis"
                        return res
                    else:
                        res = mem_res

                self._write_routing_state(intent, "memory")
                if isinstance(res, dict):
                    res["route"] = res.get("route") or "memory"
                return res
            except Exception as e:
                return await self._handle_routing_failure(e, "memory", query, interpretation)

        # -------------------------------------------------------------
        # 8. Local Model Generation fallback (coding_help / summarize / conversation)
        # -------------------------------------------------------------
        if intent in ("coding_help", "summarize", "conversation"):
            logger.info("Routing local-model based text tasks to ConversationHandler.")
            try:
                res = await self.conversation_handler.respond(query, interpretation)
                self._write_routing_state(intent, "local_model")
                if isinstance(res, dict):
                    res["route"] = res.get("route") or "local_model"
                return res
            except Exception as e:
                return await self._handle_routing_failure(e, "local_model", query, interpretation)

        # Final default fallback route
        logger.warning(f"No specific route matches intent '{intent}'. Routing to ConversationHandler as fallback.")
        try:
            res = await self.conversation_handler.respond(query, interpretation)
            self._write_routing_state(intent, "fallback_local_model")
            if isinstance(res, dict):
                res["route"] = res.get("route") or "fallback_local_model"
            return res
        except Exception as e:
            return await self._handle_routing_failure(e, "fallback_local_model", query, interpretation)

    # -----------------------------------------------------------------
    # Helper & Internal Methods
    # -----------------------------------------------------------------
    def _attempt_clarification_resolution(self, pending_intent: dict, query: str, interpretation: dict) -> dict | None:
        query_lower = query.lower().strip()
        intent = pending_intent.get("intent")
        
        # 1. Quick check if user typed exactly a matching registry project/alias
        if intent in ("open_project", "run_project"):
            try:
                resolved_proj = self.project_registry.resolve_project(query)
                path = resolved_proj.folder_path if resolved_proj else None
                if path:
                    pending_intent["entities"]["project_name"] = resolved_proj.name
                    pending_intent["resolved_reference"] = path
                    pending_intent["confidence"] = 0.95
                    return pending_intent
            except AmbiguousProjectError:
                pass
                
        # 2. Check if clarification was for media/music or known app
        elif intent == "play_media":
            pending_intent["entities"]["title"] = query.strip()
            pending_intent["resolved_reference"] = query.strip()
            pending_intent["confidence"] = 0.95
            return pending_intent
        elif intent == "open_app":
            apps = ["chrome", "browser", "vscode", "vs code", "notepad", "explorer", "terminal"]
            for app in apps:
                if app in query_lower:
                    pending_intent["entities"]["app_name"] = app
                    pending_intent["resolved_reference"] = app
                    pending_intent["confidence"] = 0.95
                    return pending_intent

        # 3. Merge new entities extracted by the interpreter from the clarification query
        new_entities = interpretation.get("entities", {})
        merged_entities = pending_intent.get("entities", {}).copy()
        
        entity_merged = False
        for k, v in new_entities.items():
            if v is not None:
                merged_entities[k] = v
                entity_merged = True
                
        if entity_merged:
            pending_intent["entities"] = merged_entities
            
            # Re-verify if it resolves now
            if intent in ("open_project", "run_project"):
                p_name = merged_entities.get("project_name")
                if p_name:
                    try:
                        resolved_proj = self.project_registry.resolve_project(p_name)
                        path = resolved_proj.folder_path if resolved_proj else None
                        if path:
                            pending_intent["entities"]["project_name"] = resolved_proj.name
                            pending_intent["resolved_reference"] = path
                            pending_intent["confidence"] = 0.9
                            return pending_intent
                    except AmbiguousProjectError:
                        pass
            elif intent == "play_media":
                title = merged_entities.get("title") or query.strip()
                pending_intent["entities"]["title"] = title
                pending_intent["resolved_reference"] = title
                pending_intent["confidence"] = 0.95
                return pending_intent
            elif intent == "open_app":
                a_name = merged_entities.get("app_name")
                if a_name:
                    pending_intent["resolved_reference"] = a_name
                    pending_intent["confidence"] = 0.9
                    return pending_intent

        return None

    def _write_routing_state(self, intent: str, route: str):
        self.context_store.set("last_intent", intent)
        self.context_store.set("last_route", route)
        # Clear clarification loops upon successful routing
        self.context_store.delete("pending_intent")
        self.context_store.delete("clarify_rounds")

    async def _handle_routing_failure(self, error: Exception, failed_route: str, query: str, interpretation: dict) -> dict:
        logger.error(f"Route '{failed_route}' failed with exception: {error}")
        
        # Log to SQLite system_errors table
        try:
            import uuid
            error_id = str(uuid.uuid4())
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO system_errors (id, component, severity, message, context, occurred_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?);",
                    (
                        error_id,
                        "router",
                        "error",
                        str(error),
                        json.dumps({"failed_route": failed_route, "query": query, "interpretation": interpretation}),
                        int(time.time()),
                        int(time.time())
                    )
                )
        except Exception as db_err:
            logger.error(f"Failed logging routing error to system_errors table: {db_err}")

        # Trigger Kate's Autonomous Thinking & Self-Healing Mode before falling back to raw error
        try:
            from assistant.self_healing import thinking_engine
            remedy = thinking_engine.think_and_remediate(query, interpretation.get("intent", ""), str(error), interpretation)
            if remedy and remedy.get("status") in ("success", "remediated", "permission_required", "thinking_remediated"):
                return remedy
        except Exception as think_err:
            logger.debug(f"Self-healing error: {think_err}")

        # Failure Cascade: fallback to ConversationHandler if a tool route failed
        if failed_route.startswith("tool"):
            logger.info("Cascading to ConversationHandler for error explanation.")
            try:
                res = await self.conversation_handler.respond(
                    query,
                    {
                        "intent": "conversation",
                        "confidence": 1.0,
                        "response_override": f"I tried to run your command but the execution failed: {error}."
                    }
                )
                self.context_store.set("last_intent", "conversation")
                self.context_store.set("last_route", "fallback_conversation")
                if isinstance(res, dict):
                    res["route"] = res.get("route") or "fallback_conversation"
                return res
            except Exception as fallback_err:
                logger.error(f"Cascade fallback to ConversationHandler also failed: {fallback_err}")
                
        return {
            "status": "failure",
            "response": f"Encountered a system error running your request: {error}.",
            "route": "failed"
        }

    async def _execute_tool_safely(self, intent: str, entities: dict, resolved_ref: str, bypass_confirm: bool = False, metadata: dict | None = None) -> dict:
        intent_map = {
            "open_app": "open_app",
            "open_project": "open_project",
            "run_project": "open_project",
            "search_web": "search_files",
            "file_management": "read_file",
            "save_workspace": "save_workspace",
            "continue_working": "continue_working",
            "resume_workspace": "continue_working",
            "switch_profile": "switch_profile",
            "current_profile": "current_profile",
            "accept_recommendation": "accept_recommendation",
            "reject_recommendation": "reject_recommendation",
            "tune_recommendation_weights": "tune_recommendation_weights",
            "store_memory": "store_memory",
            "query_memory": "query_memory",
            "memory_history": "memory_history",
            "prune_memories": "prune_memories",
            "search_youtube": "search_youtube",
            "watch_video": "watch_video",
            "finish_video": "finish_video",
            "research_topic": "research_topic",
            "search_web": "search_web",
            "summarize_sources": "summarize_sources",
            "store_project_memory": "store_project_memory",
            "list_project_memories": "list_project_memories",
            "switch_project_focus": "switch_project_focus",
            "switch_project": "switch_project",
            "list_active_projects": "list_active_projects",
            "project_catchup_brief": "project_catchup_brief",
            "extract_subtitles": "extract_subtitles",
            "extract_audio": "extract_audio",
            "get_media_info": "get_media_info",
            "download_video": "download_video",
            "convert_media": "convert_media",
            "inspect_playlist": "inspect_playlist",
            "transcribe_audio": "transcribe_audio",
            "speak_text": "speak_text",
            "toggle_voice_mode": "toggle_voice_mode",
            "crawl_website": "crawl_website",
            "fetch_dynamic_page": "fetch_dynamic_page",
            "system_control": "get_system_telemetry"
        }
        tool_name = intent_map.get(intent, intent)
        if tool_name not in self.tool_executor.tools and intent in self.tool_executor.tools:
            tool_name = intent
            
        tool_obj = self.tool_executor.tools.get(tool_name)
        is_dry_run = bool(entities.get("dry_run"))

        # Large crawl safety guard (> 25 pages)
        if not bypass_confirm and intent == "crawl_website" and int(entities.get("max_pages", 0)) > 25 and not is_dry_run:
            action_desc = f"crawl up to {entities.get('max_pages')} pages starting from '{resolved_ref}'"
            pending_action = {
                "intent": intent,
                "entities": entities,
                "resolved_reference": resolved_ref,
                "action_description": action_desc
            }
            self.context_store.set("pending_action", json.dumps(pending_action))
            return {
                "status": "confirm",
                "response": f"⚠ This will {action_desc}. Confirm large crawl? (yes/no)",
                "route": "confirmation_prompt"
            }

        # Voice-specific safety guard (prompt.md Section 12 & 15)
        if not bypass_confirm and not is_dry_run:
            voice_req, voice_prompt = check_voice_safety_requirement(intent, metadata)
            if voice_req:
                pending_action = {
                    "intent": intent,
                    "entities": entities,
                    "resolved_reference": resolved_ref,
                    "action_description": f"execute '{intent}' via voice"
                }
                self.context_store.set("pending_action", json.dumps(pending_action))
                return {
                    "status": "confirm",
                    "response": voice_prompt,
                    "route": "confirmation_prompt"
                }

        if not bypass_confirm and tool_obj and tool_obj.risk_level == "destructive" and not is_dry_run:
            action_desc = get_action_description(intent, resolved_ref)
            pending_action = {
                "intent": intent,
                "entities": entities,
                "resolved_reference": resolved_ref,
                "action_description": action_desc
            }
            self.context_store.set("pending_action", json.dumps(pending_action))
            return {
                "status": "confirm",
                "response": f"⚠ This will {action_desc}. Confirm? (yes/no)",
                "route": "confirmation_prompt"
            }
            
        # ── Kate Desktop & Media Integrations ──
        if intent == "play_media":
            title = entities.get("title") or resolved_ref or query
            platform = entities.get("platform", "youtube")
            browser = entities.get("browser", "brave")
            res = MediaLauncher.play_media(title, platform=platform, browser=browser)
            self._write_routing_state(intent, "tool:play_media")
            if context_engine:
                context_engine.active_entities["last_media_title"] = title
                context_engine.active_entities["last_media_platform"] = platform
                if browser:
                    context_engine.active_entities["last_browser"] = browser
            return {
                "status": "success",
                "message": res.get("message", f"Playing '{title}' on {platform.title()}, Satyam!"),
                "response": res.get("message", f"Playing '{title}' on {platform.title()}, Satyam!"),
                "route": "tool:play_media",
                "target": res.get("target")
            }

        elif intent == "multi_task":
            tasks = entities.get("tasks", [])
            logger.info(f"JarvisRouter: Executing {len(tasks)} multi-tasks simultaneously: {tasks}")
            import asyncio
            from assistant.interpreter import RuleBasedInterpreter

            interpreter = RuleBasedInterpreter()
            async def _run_subtask(t_query: str):
                t_interp = await interpreter.interpret(t_query)
                return await self.route(t_query, t_interp)

            results = await asyncio.gather(*[_run_subtask(t) for t in tasks], return_exceptions=True)

            messages = []
            for idx, r in enumerate(results):
                if isinstance(r, Exception):
                    messages.append(f"Sub-task '{tasks[idx]}' failed: {r}")
                elif isinstance(r, dict):
                    msg = r.get("message") or r.get("response") or "done"
                    messages.append(msg)
                else:
                    messages.append(str(r))

            if len(messages) == 1:
                combined_msg = messages[0]
            elif len(messages) == 2:
                combined_msg = f"{messages[0]} and {messages[1]}"
            else:
                combined_msg = ", ".join(messages[:-1]) + f", and {messages[-1]}"

            self._write_routing_state(intent, "tool:multi_task")
            return {
                "status": "success",
                "message": combined_msg,
                "response": combined_msg,
                "route": "tool:multi_task",
                "sub_results": [r if not isinstance(r, Exception) else str(r) for r in results]
            }

        elif intent == "open_app":
            res = app_registry.launch(resolved_ref, browser=entities.get("browser"))
            self._write_routing_state(intent, "tool:open_app")
            return res

        elif intent == "take_screenshot":
            res = ScreenshotTool.capture()
            self._write_routing_state(intent, "tool:take_screenshot")
            return res

        elif intent == "show_desktop":
            res = WindowControl.minimize_all()
            self._write_routing_state(intent, "tool:show_desktop")
            return res

        elif intent == "switch_window":
            target = resolved_ref or entities.get("window_title", "")
            res = WindowControl.switch_to_window(target)
            self._write_routing_state(intent, "tool:switch_window")
            return res

        elif intent == "control_media":
            action = entities.get("action", "play_pause")
            if action == "stop":
                res = AudioMediaControl.stop()
            else:
                res = AudioMediaControl.play_pause()
            self._write_routing_state(intent, "tool:control_media")
            return res

        elif intent == "control_volume":
            action = entities.get("action", "toggle_mute")
            if action == "up":
                res = AudioMediaControl.volume_step("up")
            elif action == "down":
                res = AudioMediaControl.volume_step("down")
            else:
                res = AudioMediaControl.toggle_mute()
            self._write_routing_state(intent, "tool:control_volume")
            return res

        elif intent == "lock_workstation":
            res = SystemControl.lock_pc()
            self._write_routing_state(intent, "tool:lock_workstation")
            return res

        elif intent == "organize_folder":
            res = FolderOrganizer.organize()
            self._write_routing_state(intent, "tool:organize_folder")
            return res

        elif intent == "download_song":
            song_title = resolved_ref or entities.get("title") or entities.get("query")
            res = await SongDownloader.download_song(song_title)
            self._write_routing_state(intent, "tool:download_song")
            return res

        elif intent == "generate_pdf":
            content = resolved_ref or entities.get("content") or query
            res = PDFReportGenerator.create_pdf("Research Document", content)
            self._write_routing_state(intent, "tool:generate_pdf")
            return res

        # Proceed normally
        res = await self.tool_executor.execute_tool(intent, entities, resolved_ref)
        self._write_routing_state(intent, f"tool:{intent}")
        if isinstance(res, dict):
            res["route"] = res.get("route") or f"tool:{intent}"
        return res

