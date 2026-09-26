import asyncio
import logging
import os
import sys
import time

# Ensure workspace root is in sys.path
_parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

try:
    from assistant.config import HOST, PORT, IDLE_UNLOAD_TIMEOUT, LOG_PATH
    from assistant.timers import IdleTimerManager
    from assistant.ipc import IPCHandler, HTTPIPCServer
    from assistant.context_engine import context_engine
    from assistant.interpreter import get_interpreter
    from assistant.database.manager import DatabaseManager
    from assistant.router import (
        JarvisRouter, SQLiteContextStore,
        MemoryManager, PlanExecutor, CloudClient, ConversationHandler
    )
    from assistant.projects import SQLiteProjectRegistry
    from assistant.tools import JarvisToolExecutor
    from assistant.recommendations import (
        JarvisRecommendationEngine,
        AcceptRecommendationTool,
        RejectRecommendationTool,
        TuneRecommendationWeightsTool
    )
    from assistant.memory import (
        JarvisMemoryManager,
        StoreMemoryTool,
        QueryMemoryTool,
        MemoryHistoryTool,
        PruneMemoriesTool,
        StoreProjectMemoryTool,
        ListProjectMemoriesTool,
        SwitchProjectFocusTool
    )
    from assistant.planner import JarvisPlanner
    from assistant.cloud import JarvisCloudClient
    from assistant.tool_lifecycle import ToolLifecycleManager
    from assistant.growth import JarvisGrowthEngine, GrowthDashboardTool
    from assistant.snapshots import SQLiteSnapshotManager, SaveWorkspaceTool, ResumeWorkspaceTool
    from assistant.profiles import SQLiteProfileManager, SwitchProfileTool, GetProfileTool
    from assistant.youtube import (
        JarvisYouTubePipeline,
        SearchYouTubeTool,
        WatchYouTubeTool,
        FinishVideoTool
    )
    from assistant.research import (
        JarvisWebResearchPipeline,
        ResearchTopicTool,
        SearchWebTool,
        SummarizeSourcesTool
    )
    from assistant.project_switcher import (
        JarvisProjectContextSwitcher,
        SwitchProjectTool,
        ListActiveProjectsTool,
        ProjectCatchupBriefTool
    )
    from assistant.media_tools import (
        JarvisMediaExtractor,
        ExtractSubtitlesTool,
        ExtractAudioTool,
        GetMediaInfoTool,
        DownloadVideoTool,
        ConvertMediaTool,
        InspectPlaylistTool
    )
    from assistant.voice import (
        JarvisVoiceSubsystem,
        WhisperSTTEngine,
        WindowsNativeTTSEngine,
        TranscribeAudioTool,
        SpeakTextTool,
        ToggleVoiceModeTool
    )
    from assistant.crawler import (
        JarvisWebCrawler,
        PlaywrightDynamicFetcher,
        CrawlWebsiteTool,
        FetchDynamicPageTool
    )
    from assistant.scheduler import (
        JarvisBackgroundScheduler,
        JarvisProactiveEngine,
        NightlyMaintenanceJob,
        ProactiveBriefingJob,
        ResourceWatchdogJob,
        StudySessionWatchdogJob,
        ListScheduledJobsTool,
        ScheduleJobTool,
        RunScheduledJobNowTool,
        GetProactiveBriefingTool,
        ToggleJobStatusTool
    )
    from assistant.telemetry import WindowsSystemTelemetry, GetSystemTelemetryTool
    from assistant.notifications import WindowsNotificationService, SendNotificationTool
    from assistant.git_tools import LocalGitService, GitStatusTool, GitDiffTool, GitCommitTool
    from assistant.indexer import LocalCodebaseIndexer, IndexCodebaseTool, SearchCodebaseTool
    from assistant.file_safety import LocalQuarantineManager, DeleteFileTool, RestoreQuarantinedFileTool
    import assistant.config as config_module
except ModuleNotFoundError:
    from config import HOST, PORT, IDLE_UNLOAD_TIMEOUT, LOG_PATH
    from timers import IdleTimerManager
    from ipc import IPCHandler, HTTPIPCServer
    try:
        from context_engine import context_engine
    except Exception:
        context_engine = None
    from interpreter import get_interpreter
    from database.manager import DatabaseManager
    from router import (
        JarvisRouter, SQLiteContextStore,
        MemoryManager, PlanExecutor, CloudClient, ConversationHandler
    )
    from projects import SQLiteProjectRegistry
    from tools import JarvisToolExecutor
    from recommendations import (
        JarvisRecommendationEngine,
        AcceptRecommendationTool,
        RejectRecommendationTool,
        TuneRecommendationWeightsTool
    )
    from memory import (
        JarvisMemoryManager,
        StoreMemoryTool,
        QueryMemoryTool,
        MemoryHistoryTool,
        PruneMemoriesTool,
        StoreProjectMemoryTool,
        ListProjectMemoriesTool,
        SwitchProjectFocusTool
    )
    from planner import JarvisPlanner
    from cloud import JarvisCloudClient
    from tool_lifecycle import ToolLifecycleManager
    from growth import JarvisGrowthEngine, GrowthDashboardTool
    from snapshots import SQLiteSnapshotManager, SaveWorkspaceTool, ResumeWorkspaceTool
    from profiles import SQLiteProfileManager, SwitchProfileTool, GetProfileTool
    from youtube import (
        JarvisYouTubePipeline,
        SearchYouTubeTool,
        WatchYouTubeTool,
        FinishVideoTool
    )
    from research import (
        JarvisWebResearchPipeline,
        ResearchTopicTool,
        SearchWebTool,
        SummarizeSourcesTool
    )
    from project_switcher import (
        JarvisProjectContextSwitcher,
        SwitchProjectTool,
        ListActiveProjectsTool,
        ProjectCatchupBriefTool
    )
    from media_tools import (
        JarvisMediaExtractor,
        ExtractSubtitlesTool,
        ExtractAudioTool,
        GetMediaInfoTool,
        DownloadVideoTool,
        ConvertMediaTool,
        InspectPlaylistTool
    )
    from voice import (
        JarvisVoiceSubsystem,
        WhisperSTTEngine,
        WindowsNativeTTSEngine,
        TranscribeAudioTool,
        SpeakTextTool,
        ToggleVoiceModeTool
    )
    from crawler import (
        JarvisWebCrawler,
        PlaywrightDynamicFetcher,
        CrawlWebsiteTool,
        FetchDynamicPageTool
    )
    from scheduler import (
        JarvisBackgroundScheduler,
        JarvisProactiveEngine,
        NightlyMaintenanceJob,
        ProactiveBriefingJob,
        ResourceWatchdogJob,
        StudySessionWatchdogJob,
        ListScheduledJobsTool,
        ScheduleJobTool,
        RunScheduledJobNowTool,
        GetProactiveBriefingTool,
        ToggleJobStatusTool
    )
    from telemetry import WindowsSystemTelemetry, GetSystemTelemetryTool
    from notifications import WindowsNotificationService, SendNotificationTool
    from git_tools import LocalGitService, GitStatusTool, GitDiffTool, GitCommitTool
    from indexer import LocalCodebaseIndexer, IndexCodebaseTool, SearchCodebaseTool
    from file_safety import LocalQuarantineManager, DeleteFileTool, RestoreQuarantinedFileTool
    import config as config_module

# Configure logging to console and file
log_handlers = [logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")]
if sys.stdout is not None:
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=log_handlers
)
logger = logging.getLogger("jarvis.daemon")

# =====================================================================
# Mock Subsystem Implementations for Routing Targets
# =====================================================================



class MockMemoryManager(MemoryManager):
    async def query_memory(self, query: str, intent_data: dict) -> dict:
        logger.info(f"MockMemoryManager: Querying memory for '{query}'")
        return {
            "status": "success",
            "response": f"Successfully retrieved facts from memory matching intent '{intent_data.get('intent')}'.",
            "route": "memory"
        }

class MockPlanExecutor(PlanExecutor):
    async def execute_plan(self, intent: str, entities: dict) -> dict:
        logger.info(f"MockPlanExecutor: Planning workflow for '{intent}'")
        return {
            "status": "success",
            "response": f"Successfully planned and executed multi-step workflow for intent '{intent}'.",
            "route": "planner"
        }

class MockCloudClient(CloudClient):
    async def escalate(self, query: str, intent_data: dict) -> dict:
        logger.info(f"MockCloudClient: Escalating query '{query}'")
        return {
            "status": "success",
            "response": f"Escalated to cloud model. Response simulation for query: '{query}'.",
            "route": "cloud"
        }

class MockConversationHandler(ConversationHandler):
    async def respond(self, query: str, intent_data: dict) -> dict:
        override = intent_data.get("response_override")
        if override:
            return {"status": "success", "response": override, "route": "local_model"}
        return {"status": "success", "response": f"This is a simulated conversation response for query: '{query}'.", "route": "local_model"}

class JarvisConversationHandler(ConversationHandler):
    """
    Intelligent conversation handler powered by the Cascading Brain:
    Tier 1: Ollama Cloud (gemma4:31b-cloud)
    Tier 2: Cloud APIs (Gemini / OpenRouter)
    Tier 3: Local Micro-Model (qwen2.5:0.5b offline)
    """
    def __init__(self, cloud_client):
        self.cloud_client = cloud_client

    async def respond(self, query: str, intent_data: dict) -> dict:
        logger.info(f"JarvisConversationHandler: Generating response for '{query}'...")
        override = intent_data.get("response_override")
        if override:
            return {
                "status": "success",
                "response": override,
                "route": "local_model",
                "tier": "⚡ Local Fast Rule"
            }

        try:
            ctx_summary = context_engine.get_dialogue_summary(max_turns=3) if context_engine else ""
            res = await self.cloud_client.escalate(query, intent_data={"intent": "conversation", "context": {"dialogue_summary": ctx_summary}})
            tier_name = res.get("tier") or res.get("model") or "Local Model"
            if res.get("status") == "success":
                return {
                    "status": "success",
                    "response": res.get("response", ""),
                    "route": "local_model",
                    "tier": tier_name,
                    "model": res.get("model", "")
                }
            else:
                return {
                    "status": "success",
                    "response": res.get("response", "I am here! How can I assist you?"),
                    "route": "local_model",
                    "tier": "💻 Local Micro-Model"
                }
        except Exception as e:
            logger.warning(f"JarvisConversationHandler error: {e}")
            return {
                "status": "success",
                "response": "Hello! I am Jarvis, your personal assistant. How can I help you today?",
                "route": "local_model",
                "tier": "💻 Local Micro-Model"
            }

# =====================================================================
# Jarvis Daemon Controller
# =====================================================================

class JarvisDaemon(IPCHandler):
    def __init__(self):
        self.host = HOST
        self.port = PORT
        self.timer_manager = IdleTimerManager(default_timeout=IDLE_UNLOAD_TIMEOUT)
        self.db = DatabaseManager()
        self.interpreter = get_interpreter()
        self.ipc_server = None
        self.is_running = False
        self.start_time = time.time()
        self.active_project = "Jarvis"
        self.loaded_resources = set()

        # Database repositories/wrappers
        self.context_store = SQLiteContextStore(self.db)
        self.project_registry = SQLiteProjectRegistry(self.db)

        # Downstream dependencies
        self.tool_executor = JarvisToolExecutor(self.db)
        self.tool_lifecycle = ToolLifecycleManager(self.db, tool_executor=self.tool_executor)
        self.growth_engine = JarvisGrowthEngine(self.db)
        self.growth_dashboard_tool = GrowthDashboardTool(self.growth_engine)
        self.tool_executor.register_tool(self.growth_dashboard_tool)

        # Workspace Snapshot Subsystem
        self.snapshot_manager = SQLiteSnapshotManager(
            self.db,
            project_registry=self.project_registry,
            tool_executor=self.tool_executor
        )
        self.save_workspace_tool = SaveWorkspaceTool(
            self.snapshot_manager,
            default_project_lookup=lambda: self.active_project
        )
        self.resume_workspace_tool = ResumeWorkspaceTool(
            self.snapshot_manager,
            default_project_lookup=lambda: self.active_project
        )
        self.tool_executor.register_tool(self.save_workspace_tool)
        self.tool_executor.register_tool(self.resume_workspace_tool)

        # Workspace Profiles Subsystem
        self.profile_manager = SQLiteProfileManager(
            self.db,
            on_profile_switch=self._on_profile_switch
        )
        self.switch_profile_tool = SwitchProfileTool(self.profile_manager)
        self.get_profile_tool = GetProfileTool(self.profile_manager)
        self.tool_executor.register_tool(self.switch_profile_tool)
        self.tool_executor.register_tool(self.get_profile_tool)

        self.memory_manager = JarvisMemoryManager(
            self.db,
            timer_manager=self.timer_manager,
            on_state_change=self._on_resource_state_change
        )
        self.store_memory_tool = StoreMemoryTool(self.memory_manager)
        self.query_memory_tool = QueryMemoryTool(self.memory_manager)
        self.memory_history_tool = MemoryHistoryTool(self.memory_manager)
        self.prune_memories_tool = PruneMemoriesTool(self.memory_manager)
        self.store_project_memory_tool = StoreProjectMemoryTool(self.memory_manager)
        self.list_project_memories_tool = ListProjectMemoriesTool(self.memory_manager)
        self.switch_project_focus_tool = SwitchProjectFocusTool(self.memory_manager)
        self.tool_executor.register_tool(self.store_memory_tool)
        self.tool_executor.register_tool(self.query_memory_tool)
        self.tool_executor.register_tool(self.memory_history_tool)
        self.tool_executor.register_tool(self.prune_memories_tool)
        self.tool_executor.register_tool(self.store_project_memory_tool)
        self.tool_executor.register_tool(self.list_project_memories_tool)
        self.tool_executor.register_tool(self.switch_project_focus_tool)

        # Wire memory_manager & context_store to OpenProjectTool & executor for context pre-warming
        if "open_project" in self.tool_executor.tools:
            self.tool_executor.tools["open_project"].memory_manager = self.memory_manager
        self.tool_executor.memory_manager = self.memory_manager
        self.tool_executor.context_store = self.context_store
        self.plan_executor = JarvisPlanner(
            self.db,
            tool_executor=self.tool_executor,
            project_registry=self.project_registry
        )
        self.cloud_client = JarvisCloudClient(self.db)
        self.conversation_handler = JarvisConversationHandler(self.cloud_client)
        self.recommendation_engine = JarvisRecommendationEngine(
            self.db,
            growth_engine=self.growth_engine,
            profile_manager=self.profile_manager,
            tool_executor=self.tool_executor,
            context_store=self.context_store
        )
        self.accept_recommendation_tool = AcceptRecommendationTool(self.recommendation_engine)
        self.reject_recommendation_tool = RejectRecommendationTool(self.recommendation_engine)
        self.tune_recommendation_weights_tool = TuneRecommendationWeightsTool(self.recommendation_engine)
        self.tool_executor.register_tool(self.accept_recommendation_tool)
        self.tool_executor.register_tool(self.reject_recommendation_tool)
        self.tool_executor.register_tool(self.tune_recommendation_weights_tool)

        # YouTube Learning Pipeline Subsystem (Section 41)
        self.youtube_pipeline = JarvisYouTubePipeline(
            db_manager=self.db,
            growth_engine=self.growth_engine,
            memory_manager=self.memory_manager,
            context_store=self.context_store
        )
        self.search_youtube_tool = SearchYouTubeTool(self.youtube_pipeline)
        self.watch_youtube_tool = WatchYouTubeTool(self.youtube_pipeline)
        self.finish_video_tool = FinishVideoTool(self.youtube_pipeline)
        self.tool_executor.register_tool(self.search_youtube_tool)
        self.tool_executor.register_tool(self.watch_youtube_tool)
        self.tool_executor.register_tool(self.finish_video_tool)

        # Autonomous Web Research Workflow Subsystem (Section 42)
        self.research_pipeline = JarvisWebResearchPipeline(
            db_manager=self.db,
            cloud_client=self.cloud_client,
            growth_engine=self.growth_engine
        )
        self.research_topic_tool = ResearchTopicTool(self.research_pipeline)
        self.search_web_tool = SearchWebTool(self.research_pipeline)
        self.summarize_sources_tool = SummarizeSourcesTool(self.research_pipeline)
        self.tool_executor.register_tool(self.research_topic_tool)
        self.tool_executor.register_tool(self.search_web_tool)
        self.tool_executor.register_tool(self.summarize_sources_tool)
 
        # Multi-Project Context Switching Subsystem (Section 44 & 45)
        self.project_switcher = JarvisProjectContextSwitcher(
            db_manager=self.db,
            project_registry=self.project_registry,
            snapshot_manager=self.snapshot_manager,
            memory_manager=self.memory_manager,
            context_store=self.context_store,
            tool_executor=self.tool_executor
        )
        self.switch_project_tool = SwitchProjectTool(self.project_switcher)
        self.list_active_projects_tool = ListActiveProjectsTool(self.project_switcher)
        self.project_catchup_brief_tool = ProjectCatchupBriefTool(self.project_switcher)
        self.tool_executor.register_tool(self.switch_project_tool)
        self.tool_executor.register_tool(self.list_active_projects_tool)
        self.tool_executor.register_tool(self.project_catchup_brief_tool)

        # Lawful Media Tools Subsystem (Section 47)
        self.media_extractor = JarvisMediaExtractor(db_manager=self.db)
        self.download_video_tool = DownloadVideoTool(self.media_extractor)
        self.extract_audio_tool = ExtractAudioTool(self.media_extractor)
        self.extract_subtitles_tool = ExtractSubtitlesTool(self.media_extractor)
        self.get_media_info_tool = GetMediaInfoTool(self.media_extractor)
        self.convert_media_tool = ConvertMediaTool(self.media_extractor)
        self.inspect_playlist_tool = InspectPlaylistTool(self.media_extractor)
        self.tool_executor.register_tool(self.download_video_tool)
        self.tool_executor.register_tool(self.extract_audio_tool)
        self.tool_executor.register_tool(self.extract_subtitles_tool)
        self.tool_executor.register_tool(self.get_media_info_tool)
        self.tool_executor.register_tool(self.convert_media_tool)
        self.tool_executor.register_tool(self.inspect_playlist_tool)

        # Voice Input/Output Subsystem (Section 48 / Milestone 19)
        self.voice_subsystem = JarvisVoiceSubsystem(
            stt_engine=WhisperSTTEngine(),
            tts_engine=WindowsNativeTTSEngine()
        )
        self.transcribe_audio_tool = TranscribeAudioTool(self.voice_subsystem)
        self.speak_text_tool = SpeakTextTool(self.voice_subsystem)
        self.toggle_voice_mode_tool = ToggleVoiceModeTool(self.voice_subsystem)
        self.tool_executor.register_tool(self.transcribe_audio_tool)
        self.tool_executor.register_tool(self.speak_text_tool)
        self.tool_executor.register_tool(self.toggle_voice_mode_tool)

        # Advanced Web Crawler & Dynamic Browser Engine (Milestone 20)
        self.dynamic_fetcher = PlaywrightDynamicFetcher()
        self.crawler = JarvisWebCrawler(dynamic_fetcher=self.dynamic_fetcher)
        self.crawl_website_tool = CrawlWebsiteTool(self.crawler)
        self.fetch_dynamic_page_tool = FetchDynamicPageTool(self.dynamic_fetcher)
        self.tool_executor.register_tool(self.crawl_website_tool)
        self.tool_executor.register_tool(self.fetch_dynamic_page_tool)

        # Autonomous Background Scheduler & Proactive Cron Engine (Milestone 21)
        self.scheduler = JarvisBackgroundScheduler(self.db)
        self.proactive_engine = JarvisProactiveEngine(
            self.db,
            growth_engine=self.growth_engine,
            recommendation_engine=self.recommendation_engine,
            context_store=self.context_store
        )
        self.nightly_maintenance_job = NightlyMaintenanceJob(self.growth_engine)
        self.proactive_briefing_job = ProactiveBriefingJob(self.proactive_engine)
        self.resource_watchdog_job = ResourceWatchdogJob(self.timer_manager, self.voice_subsystem)
        self.study_watchdog_job = StudySessionWatchdogJob()

        self.scheduler.register_job(self.nightly_maintenance_job)
        self.scheduler.register_job(self.proactive_briefing_job)
        self.scheduler.register_job(self.resource_watchdog_job)
        self.scheduler.register_job(self.study_watchdog_job)

        self.list_scheduled_jobs_tool = ListScheduledJobsTool(self.scheduler)
        self.schedule_job_tool = ScheduleJobTool(self.scheduler)
        self.run_scheduled_job_tool = RunScheduledJobNowTool(self.scheduler)
        self.get_proactive_briefing_tool = GetProactiveBriefingTool(self.proactive_engine)
        self.toggle_job_status_tool = ToggleJobStatusTool(self.scheduler)

        self.tool_executor.register_tool(self.list_scheduled_jobs_tool)
        self.tool_executor.register_tool(self.schedule_job_tool)
        self.tool_executor.register_tool(self.run_scheduled_job_tool)
        self.tool_executor.register_tool(self.get_proactive_briefing_tool)
        self.tool_executor.register_tool(self.toggle_job_status_tool)

        # Pre-UI Hardening Subsystems
        self.telemetry = WindowsSystemTelemetry()
        self.notifications = WindowsNotificationService()
        self.git_service = LocalGitService()
        self.codebase_indexer = LocalCodebaseIndexer(self.db, embedding_manager=getattr(self.memory_manager, 'embedding_manager', None))
        self.quarantine_manager = LocalQuarantineManager()

        self.get_system_telemetry_tool = GetSystemTelemetryTool(self.telemetry)
        self.send_notification_tool = SendNotificationTool(self.notifications)
        self.git_status_tool = GitStatusTool(self.git_service)
        self.git_diff_tool = GitDiffTool(self.git_service)
        self.git_commit_tool = GitCommitTool(self.git_service)
        self.index_codebase_tool = IndexCodebaseTool(self.codebase_indexer)
        self.search_codebase_tool = SearchCodebaseTool(self.codebase_indexer)
        self.delete_file_tool = DeleteFileTool(self.quarantine_manager)
        self.restore_quarantined_file_tool = RestoreQuarantinedFileTool(self.quarantine_manager)

        self.tool_executor.codebase_indexer = self.codebase_indexer
        self.tool_executor.register_tool(self.get_system_telemetry_tool)
        self.tool_executor.register_tool(self.send_notification_tool)
        self.tool_executor.register_tool(self.git_status_tool)
        self.tool_executor.register_tool(self.git_diff_tool)
        self.tool_executor.register_tool(self.git_commit_tool)
        self.tool_executor.register_tool(self.index_codebase_tool)
        self.tool_executor.register_tool(self.search_codebase_tool)
        self.tool_executor.register_tool(self.delete_file_tool)
        self.tool_executor.register_tool(self.restore_quarantined_file_tool)


        # Wire dependencies to Router
        self.router = JarvisRouter(
            db_manager=self.db,
            context_store=self.context_store,
            project_registry=self.project_registry,
            memory_manager=self.memory_manager,
            tool_executor=self.tool_executor,
            plan_executor=self.plan_executor,
            cloud_client=self.cloud_client,
            conversation_handler=self.conversation_handler,
            config=config_module,
            recommendation_engine=self.recommendation_engine,
            tool_lifecycle=self.tool_lifecycle,
            growth_engine=self.growth_engine,
            snapshot_manager=self.snapshot_manager,
            profile_manager=self.profile_manager,
            project_switcher=self.project_switcher,
            media_extractor=self.media_extractor,
            voice_subsystem=self.voice_subsystem,
            crawler=self.crawler,
            dynamic_fetcher=self.dynamic_fetcher,
            scheduler=self.scheduler,
            proactive_engine=self.proactive_engine
        )

    def _on_profile_switch(self, old_mode: str, new_mode: str):
        logger.info(f"Daemon reacting to profile switch: '{old_mode}' -> '{new_mode}'")
        target_prof = self.profile_manager.get_profile(new_mode)
        prewarmed = []
        if target_prof and target_prof.prewarm_resources:
            for res_name in target_prof.prewarm_resources:
                self.loaded_resources.add(res_name)
                prewarmed.append(res_name)
                logger.info(f"Pre-warmed lazy resource '{res_name}' for profile '{new_mode}'.")
        return prewarmed

    def _on_resource_state_change(self, name: str, is_active: bool):
        if is_active:
            self.loaded_resources.add(name)
            logger.info(f"Daemon resource state updated: '{name}' is now ACTIVE")
        else:
            self.loaded_resources.discard(name)
            logger.info(f"Daemon resource state updated: '{name}' is now IDLE")

    async def start(self):
        self.is_running = True
        logger.info("Initializing Jarvis Daemon skeleton...")
        
        # Instantiate and start HTTPIPCServer, passing self as the handler
        self.ipc_server = HTTPIPCServer(self, self.host, self.port)
        await self.ipc_server.start()

        # Start autonomous background scheduler
        try:
            await self.scheduler.start()
            logger.info("Autonomous Background Scheduler started.")
        except Exception as e:
            logger.error(f"Error starting scheduler: {e}")

    async def stop(self):
        logger.info("Shutting down daemon gracefully...")
        
        # Stop background scheduler
        try:
            if hasattr(self, "scheduler") and self.scheduler:
                await self.scheduler.stop()
                logger.info("Autonomous Background Scheduler stopped.")
        except Exception as e:
            logger.error(f"Error stopping scheduler: {e}")

        # Capture automatic session-end snapshot if an active project exists
        try:
            if hasattr(self, "snapshot_manager") and self.snapshot_manager and getattr(self, "active_project", None):
                self.snapshot_manager.capture_snapshot(
                    project_id=self.active_project,
                    snapshot_type="session_end",
                    notes="Auto-saved snapshot on daemon shutdown."
                )
                logger.info(f"Auto-saved workspace snapshot for '{self.active_project}' on shutdown.")
        except Exception as e:
            logger.error(f"Error capturing auto-snapshot on daemon stop: {e}")

        # Run recommendation feedback loop before exiting
        try:
            await self.recommendation_engine.run_session_end_feedback_loop()
        except Exception as e:
            logger.error(f"Error running session-end feedback loop on stop: {e}")

        # Auto-close stale YouTube study sessions
        try:
            if hasattr(self, "youtube_pipeline") and self.youtube_pipeline:
                self.youtube_pipeline.auto_close_stale_sessions()
        except Exception as e:
            logger.error(f"Error auto-closing YouTube study sessions on stop: {e}")
            
        # Cancel all timers and force unload resources
        await self.timer_manager.cancel_all()
        
        # Stop IPC server
        if self.ipc_server:
            await self.ipc_server.stop()
            
        self.is_running = False
        logger.info("Daemon shutdown completed.")

    # IPCHandler Interface Implementation
    async def handle_health(self) -> dict:
        # Determine memory footprint
        memory_mb = 35.0
        try:
            import psutil
            process = psutil.Process(os.getpid())
            memory_mb = round(process.memory_info().rss / (1024 * 1024), 2)
        except ImportError:
            pass
            
        # Check SQLite database status and schema version
        sqlite_status = "ok"
        applied_version = 0
        try:
            conn = self.db.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT version FROM schema_meta ORDER BY version DESC LIMIT 1;")
            version_row = cursor.fetchone()
            applied_version = version_row["version"] if version_row else 0
            conn.close()
        except Exception as e:
            sqlite_status = f"error: {e}"
            
        return {
            "status": "ok",
            "interpreter": "ready",
            "sqlite": sqlite_status,
            "applied_schema_version": applied_version,
            "embedding_model": "active" if "embeddings" in self.loaded_resources else "idle",
            "cloud_reachable": True,
            "active_project": self.active_project,
            "memory_mb": memory_mb,
            "uptime_seconds": round(time.time() - self.start_time, 2),
            "loaded_resources": list(self.loaded_resources)
        }

    async def handle_query(self, query: str) -> dict:
        logger.info(f"Processing query: '{query}'")
        
        # 1. Run local model interpretation and record latency metrics
        start_t = time.time()
        context = context_engine.get_context() if context_engine else None
        interpretation = await self.interpreter.interpret(query, context=context)
        latency_ms = round((time.time() - start_t) * 1000, 2)
        
        provider = interpretation.get("metadata", {}).get("provider", "unknown")
        logger.info(f"Interpretation completed in {latency_ms} ms using provider '{provider}'.")
        logger.info(f"Parsed intent: '{interpretation.get('intent')}' with confidence {interpretation.get('confidence')}")
        
        # 2. Touch registered resource if mentioned in query
        for res in list(self.loaded_resources):
            if res in query:
                logger.info(f"Query mentions loaded resource '{res}'. Touching it to prevent unload.")
                await self.timer_manager.touch(res)
                
        # 3. Route execution
        try:
            route_result = await self.router.route(query, interpretation)
        except Exception as e:
            logger.error(f"Error executing route: {e}")
            route_result = {
                "status": "failure",
                "response": f"Internal routing failure: {e}",
                "route": "failed"
            }

        # Context Engine: Record completed turn in conversational memory
        if context_engine:
            try:
                context_engine.record_turn(
                    user_query=query,
                    intent_data=interpretation,
                    assistant_response=route_result.get("response", "") or route_result.get("message", ""),
                    action_taken=route_result.get("route", "")
                )
            except Exception as e:
                logger.debug(f"ContextEngine record_turn error: {e}")

        # Update active project tracker if current_focus changed
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT project_id FROM current_focus WHERE id = 1;")
                row = cursor.fetchone()
                if row and row["project_id"]:
                    self.active_project = row["project_id"]
        except Exception:
            pass

        res_payload = {
            "status": route_result.get("status", "success"),
            "interpretation": interpretation,
            "latency_ms": latency_ms,
            "provider": provider,
            "response": route_result.get("response", ""),
            "route": route_result.get("route", "")
        }
        for k, v in route_result.items():
            if k not in res_payload:
                res_payload[k] = v
        return res_payload

    async def handle_load_resource(self, name: str, timeout: int) -> dict:
        logger.info(f"Loading lazy resource: '{name}'")
        self.loaded_resources.add(name)
        
        # Define unload callback
        def unload_callback():
            logger.info(f"Callback executing: Unloading '{name}'")
            self.loaded_resources.discard(name)
            
        await self.timer_manager.register_or_update(name, unload_callback, timeout=timeout)
        return {"message": f"Loaded resource '{name}'"}

    async def handle_shutdown(self) -> dict:
        logger.info("Shutdown request received via IPC.")
        # Schedule graceful shutdown in 0.5s to allow response to send
        asyncio.get_event_loop().call_later(0.5, lambda: asyncio.create_task(self.stop()))
        return {"message": "Shutting down..."}

def run_daemon():
    daemon = JarvisDaemon()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(daemon.start())
    except KeyboardInterrupt:
        logger.info("Daemon interrupted by keyboard signal.")
        loop.run_until_complete(daemon.stop())
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.critical(f"Daemon crashed with error: {e}")
    finally:
        # Cancel all remaining tasks
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        logger.info("Daemon loop closed. Exiting.")

if __name__ == "__main__":
    run_daemon()
