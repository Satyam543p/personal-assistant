# development.md

# Jarvis Development Control

## Purpose

This file is the implementation control center for the project.

Unlike `phase.md`, this document does **not** describe the architecture.

Instead, it tells coding agents:

* what should be implemented now
* what should remain future work
* current project progress
* completed work
* remaining work

This file changes during development.

`phase.md` should remain mostly stable.

---

# Current Development

**Current Module:** Pre-UI System Hardening & Comprehensive Feature Expansion (Milestone 22)  
**Status:** Completed & Verified (100%)  
**Completed Goal:** Implemented all 6 audited functional enhancements identified prior to UI development per `pre_ui_todo_roadmap.md`:
1. **Hardware & System Telemetry Subsystem (`assistant/telemetry.py`):** `SystemTelemetry` abstraction, `WindowsSystemTelemetry` with live CPU/RAM/Battery metrics, adaptive throttling guard `is_safe_for_heavy_task()` protecting the 8 GB RAM and battery budget, and `GetSystemTelemetryTool`.
2. **Native Windows Desktop Notifications & Audio (`assistant/notifications.py`):** `NotificationService` abstraction, non-blocking PowerShell Windows toast notifications, audio cues for reminders, and `SendNotificationTool`.
3. **Git & Repository Intelligence Subsystem (`assistant/git_tools.py`):** `GitService` abstraction, `LocalGitService` wrapping branch/dirty-status/diff/commit, `GitStatusTool`, `GitDiffTool`, `GitCommitTool`, and automatic pre-switch uncommitted changes dirty-tree warning in `ProjectSwitcher`.
4. **Local Codebase & Document Ingestion Engine (`assistant/indexer.py` & `011_codebase_index.sql`):** `DocumentChunker` and `CodebaseIndexer` abstractions, sliding-window chunking, zero-daemon lazy-loaded vector embeddings stored in SQLite `document_chunks`, hybrid lexical + cosine similarity search, `IndexCodebaseTool`, and `SearchCodebaseTool`.
5. **Prompt-Injection `<data>` Delimiter Sanitization (`assistant/safety.py`):** `DataSanitizer` utility, detection of injection overrides, instruction defanging, and strict `<data label="...">` XML boundary encapsulation in `research.py` and file reading tools.
6. **File Quarantine & Reversible Operations (`assistant/file_safety.py`):** `QuarantineManager` abstraction, `LocalQuarantineManager` with manifest tracking, automatic pre-overwrite snapshots in `WriteFileTool`, safe `DeleteFileTool`, and `RestoreQuarantinedFileTool`.
All 7 verification stages and all 3 regression suites (Scheduler, Crawler/Browser, Voice Hooks) passed 100%. System is now 100% feature-complete and ready for UI design.  
**Next Recommended Milestone:** Phase 3 UI Implementation (Desktop Dashboard / Interactive Client).  
**Last Updated:** 2026-09-26T11:41:00+05:30  

---

# Current Features (Phase 1 & Phase 2 Active)

These features are approved for immediate implementation:

### Phase 1 (Completed)
- [x] **Core Daemon Skeleton** (IPC framework, event loop, idle-unload timer infrastructure)
- [x] **Local Interpreter Integration** (Quantized Qwen 0.5B served via llama.cpp or equivalent, warm-served, GBNF/JSON grammar-constrained schema parser)
- [x] **SQLite Memory & System DB** (Creation of all schemas from Section 30, version/migration tracking in `schema_meta`, backup logic)
- [x] **Router Engine** (Confidence-based path selection, deterministic routing table, disambiguation/clarification triggers)
- [x] **Project Registry Subsystem** (Fuzzy name resolution, alias handling, editor mapping, metadata query)
- [x] **Core Tools Suite** (Scoped/validated tools: `open_app`, `open_project`, `search_files`, `read_file`, `write_file`)
- [x] **Safety Layer v1** (Confirmation UX gating for destructive/irreversible requests, basic OS-level sandbox limits for code/tool executions)
- [x] **Minimal Recommendation Engine** (Deterministic SQLite-based priority recommendations, goal/habit matching, no embeddings)

### Phase 2 (Active & Completed)
- [x] **Semantic Memory ("embeddings v2")** (Lazy-loaded embedding model + SQLite vector storage + Cosine similarity search, idle-unload timer)
- [x] **Planner Subsystem** (Multi-step task decomposition, plans/steps state tables, rollback handler on step failure)
- [x] **Cloud Escalation Client** (Stateless client wrapper for Grok or other cloud models, API keys, cache layer, token budget counting)
- [x] **Tool Lifecycle Manager** (Cloud-drafted tool creation, static/dependency checks, sandbox test run validation, versioning/deprecations)
- [x] **Personal Growth Engine** (Goal tracker, skill tracker with decay, learning graph config, study session logger, growth dashboard tool)
- [x] **Workspace Snapshot System & Continue Working Protocol** (Workspace snapshots, restore protocol, Git active branch check)
- [x] **Workspace Profiles** (Coding, study, project, research mode presets to bias tool pre-warming and recommendations)
- [x] **Recommendation Engine Feedback Loop** (Weight self-tuning based on accepted/rejected outcomes, feedback capture)
- [x] **Memory Importance & Conflict Resolution Refinements** (Recency decay, specificity weights, archiving superseded memories)
- [x] **YouTube Learning Pipeline** (Search, select, play, watch-logging, study session integration, skill confidence nudging)
- [x] **Autonomous Web Research Workflow** (Web search, multi-source fetch, quality filter, local/cloud synthesis, citations, research folder writer)
- [x] **Project-Specific Memory & Focus Context** (Project-scoped facts in SQLite `memories`, isolated conflict resolution, top-5 pre-warming on focus switch, opt-in capture)
- [x] **Multi-Project Context Switching** (Section 44 & 45: 7-step context switch sequence, 30-day awareness, namespace isolation, >48h catch-up briefs)
- [x] **Lawful Media/Download Tools** (Section 47: MediaExtractor ABC, subtitle & audio extraction with duration/size caps, Safety Layer confirmation guards)
- [x] **Voice Input/Output Hooks** (Section 48: SpeechToTextEngine & TextToSpeechEngine ABCs, lazy-loaded speech processing, idle unloading, verbatim confirmation guards)
- [x] **Deep Multi-Level Site Crawling (Spider)** (Section 49: BFS queue traversal, URL canonicalization, domain bounding, SSRF defense, polite delays, cycle prevention, markdown knowledge base archiving)
- [x] **Headless Dynamic Browser (Playwright)** (Section 50: DynamicBrowserFetcher ABC, lazy-loaded Playwright Chromium engine, single-page execution with 0 MB idle RAM, SPA text & link extraction)
- [x] **Autonomous Background Scheduler & Proactive Cron Engine** (Section 51: ScheduledJob, BackgroundScheduler & ProactiveEngine ABCs, non-blocking tick loop, concurrency overlap protection, SQLite execution auditing in `010_scheduler.sql`, nightly maintenance, backup rotation, and morning/evening briefings)
- [x] **Hardware & System Telemetry Subsystem** (Section 2 & 3: SystemTelemetry ABC, WindowsSystemTelemetry, adaptive resource throttling for 8 GB RAM and battery protection, GetSystemTelemetryTool)
- [x] **Native Windows Desktop Notifications** (NotificationService ABC, Windows toast notifications, audio cues for reminders and proactive alerts, SendNotificationTool)
- [x] **Git & Repository Intelligence Subsystem** (GitService ABC, LocalGitService, Git status/diff/commit tools, dirty tree pre-switch protection in ProjectSwitcher)
- [x] **Local Codebase & Document Ingestion Engine (Local RAG)** (DocumentChunker & CodebaseIndexer ABCs, schema 011_codebase_index.sql, lazy embedding indexing, hybrid lexical + vector search, IndexCodebaseTool, SearchCodebaseTool)
- [x] **Prompt-Injection `<data>` Delimiter Sanitization** (Section 12: DataSanitizer utility, injection override neutralizing, structural `<data label="...">` encapsulation)
- [x] **File Quarantine & Reversible Operations** (Section 9 & 11: QuarantineManager ABC, LocalQuarantineManager, automatic pre-overwrite snapshot in WriteFileTool, DeleteFileTool, RestoreQuarantinedFileTool)

---

# Future Features (Phase 2+ - Post-Stable Release)

These features must **not** be implemented until explicitly requested and approved by the user:
- [ ] **SQLite Vector Extension** (Integration of `sqlite-vec` or similar when database scale demands it)
- [ ] **Multi-Agent Expansion Hooks** (IPC protocol, agent attribution logs, multi-agent step assignment framework)

---

# Session Progress

### Completed Today
- [x] Initialized development workspace environment.
- [x] Analyzed `prompt.md` and `phase.md` specifications.
- [x] Separated features into **Current** (Phase 1) and **Future** (Phase 2+) scopes.
- [x] Populated `development.md` file.
- [x] Designed and implemented Core Daemon Skeleton with async HTTP IPC server at port 7474.
- [x] Implemented `IdleTimerManager` to automatically unload lazy resources.
- [x] Implemented CLI control wrapper in `assistant/main.py`.
- [x] Refactored IPC communication layer to decouple the core runtime from the HTTP transport layer via abstract `IPCServer`, `IPCClient`, and `IPCHandler` interfaces.
- [x] Designed and implemented `LocalInterpreter` abstraction layer, supporting Ollama, LlamaCpp, and a pattern-matching RuleBased fallback provider.
- [x] Added `auto` interpreter mode resolving dynamically to the best available runtime.
- [x] Configured strict JSON schema enforcement with debug metadata and latency metrics logging.
- [x] Designed and implemented modular SQLite schema structure, splitting the 19 database tables into 6 logical script files.
- [x] Designed loose database relationships referencing stable ID strings rather than hard SQLite foreign keys.
- [x] Built the `DatabaseManager` connection manager at `assistant/database/manager.py` with Write-Ahead Logging (WAL) and automatic migration sequencing.
- [x] Integrated database loading and runtime connectivity checks into the daemon start and health APIs.
- [x] Verified database creation, migrations, and health integration.
- [x] Designed, implemented, and verified the Router Engine dispatch layer under `assistant/router.py` with confidence-based path selection, tie-breaking heuristics, and active context clarification loops.
- [x] Implemented Central Project Registry subsystem under `assistant/projects.py` with schema extension `007_projects_metadata.sql`, fuzzy SequenceMatcher resolution, recency tie-breaking, and CRUD interfaces.

- [x] Designed, implemented, and verified the Planner Subsystem under `assistant/planner.py` with multi-step task decomposition, sequential dependency execution, SQLite state persistence in `plans` and `plan_steps`, and automated reverse rollbacks on failure.
- [x] Designed, implemented, and verified the Cloud Escalation Client under `assistant/cloud.py` with abstract provider interface, exponential backoff retries (1s, 2s, 4s), monthly token quota tracking, 24-hour response caching in SQLite `cloud_cache` (`008_cloud_cache.sql`), and fast offline detection.
- [x] Designed, implemented, and verified the Tool Lifecycle Manager under `assistant/tool_lifecycle.py` with static AST security inspector (blocking eval/exec/disallowed imports/dunder exploits), isolated subprocess sandbox test runner requiring >=3 synthetic test cases and timeout enforcement, dynamic registration into `assistant/custom_tools/` and SQLite `tools` table, versioning/deprecation/retirement, capability gap discovery via `assistant/capability_registry.json`, and daemon IPC router integration.
- [x] Designed, implemented, and verified the Personal Growth Engine under `assistant/growth.py` with goal tracking and milestone event logging, skill tracking with 90-day half-life decay and 0.1 minimum confidence floor (Section 23), study session logging (<2 min discard rule & crash recovery per Section 31), learning graph configuration (`assistant/learning_graph.json`), and user-facing `growth_dashboard` tool.
- [x] Designed, implemented, and verified the Workspace Snapshot System & Continue Working Protocol under `assistant/snapshots.py` with `SnapshotManager` abstraction, `SQLiteSnapshotManager`, automatic open files harvesting from `tool_logs` (capped at 20), Git branch detection, Continue Working restore protocol with file existence verification, 7-day staleness checking, `SaveWorkspaceTool`/`ResumeWorkspaceTool`, router reference resolution, and daemon auto-snapshot on shutdown.
- [x] Designed, implemented, and verified Workspace Profiles Subsystem under `assistant/profiles.py` with `ProfileManager` abstraction, `SQLiteProfileManager`, schema migration `009_profiles.sql`, canonical profiles (`coding`, `study`, `research`, `project`), recommendation engine profile bias scoring (`+0.25`), lazy resource pre-warming on profile switch, `SwitchProfileTool`/`GetProfileTool`, and IPC integration.
- [x] Designed, implemented, and verified Recommendation Engine Feedback Loop under `assistant/recommendations.py` with `RecommendationEngine` abstraction, `JarvisRecommendationEngine`, explicit feedback capture (`accept_recommendation`, `reject_recommendation`), ordinal/index resolution, auto-execution of accepted recommendation (e.g. `open_project`), outcome evaluation (`completed`, `abandoned`, `neutral`), weight self-tuning algorithm with strict clamping bounds (`[default * 0.5, default * 2.0]`), persistence in SQLite `preferences` table, dedicated tools (`AcceptRecommendationTool`, `RejectRecommendationTool`, `TuneRecommendationWeightsTool`), interpreter patterns, router integration, and daemon session-end execution.
- [x] Designed, implemented, and verified Lawful Media/Download Tools under `assistant/media.py` (Section 47: MediaExtractor ABC, subtitle & audio extraction with duration/size caps, Safety Layer confirmation guards).
- [x] Designed, implemented, and verified Voice Input/Output Hooks under `assistant/voice.py` (Section 48: SpeechToTextEngine & TextToSpeechEngine ABCs, lazy-loaded speech processing, idle unloading, verbatim confirmation guards).
- [x] Designed, implemented, and verified Deep Multi-Level Site Crawling & Headless Dynamic Browser Subsystem under `assistant/crawler.py` (Sections 49 & 50: DynamicBrowserFetcher & WebCrawler ABCs, Playwright Chromium lazy execution with 0 MB idle RAM, URL canonicalization, SSRF defense, BFS spider traversal, and structured markdown knowledge base archiving).
- [x] Designed, implemented, and verified Autonomous Background Scheduler & Proactive Cron Engine under `assistant/scheduler.py` (Section 51: ScheduledJob, BackgroundScheduler & ProactiveEngine ABCs, schema migration `010_scheduler.sql`, non-blocking tick loop, concurrency overlap protection, error containment, nightly maintenance, automated backup rotation, and morning/evening briefings).

### Current Module
- None (Phase 2 Milestones 1–21 Complete)

### Completed Features
- Core Daemon Skeleton
- Local Interpreter Integration
- SQLite Memory & System DB
- Router Engine
- Project Registry Subsystem
- Core Tools Suite
- Safety Layer v1
- Minimal Recommendation Engine
- Semantic Memory ("embeddings v2")
- Planner Subsystem
- Cloud Escalation Client
- Tool Lifecycle Manager
- Personal Growth Engine
- Workspace Snapshot System & Continue Working Protocol
- Workspace Profiles Subsystem
- Recommendation Engine Feedback Loop
- Memory Importance & Conflict Resolution Refinements
- YouTube Learning Pipeline
- Autonomous Web Research Workflow
- Project-Specific Memory & Focus Context
- Multi-Project Context Switching
- Lawful Media/Download Tools
- Voice Input/Output Hooks
- Deep Multi-Level Site Crawling (Spider)
- Headless Dynamic Browser (Playwright)
- Autonomous Background Scheduler & Proactive Cron Engine

### Modified Files
- [development.md](file:///c:/Users/Satyam Pandey/Desktop/personal assistent/development.md)
- [assistant/config.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/config.py)
- [assistant/timers.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/timers.py)
- [assistant/ipc.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/ipc.py)
- [assistant/interpreter.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/interpreter.py)
- [assistant/database/manager.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/database/manager.py)
- [assistant/database/schemas/001_core.sql](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/database/schemas/001_core.sql)
- [assistant/database/schemas/002_memory.sql](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/database/schemas/002_memory.sql)
- [assistant/database/schemas/003_projects.sql](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/database/schemas/003_projects.sql)
- [assistant/database/schemas/004_tools.sql](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/database/schemas/004_tools.sql)
- [assistant/database/schemas/005_planner.sql](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/database/schemas/005_planner.sql)
- [assistant/database/schemas/006_growth.sql](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/database/schemas/006_growth.sql)
- [assistant/database/schemas/007_projects_metadata.sql](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/database/schemas/007_projects_metadata.sql)
- [assistant/database/schemas/008_cloud_cache.sql](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/database/schemas/008_cloud_cache.sql)
- [assistant/database/schemas/009_profiles.sql](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/database/schemas/009_profiles.sql)
- [assistant/database/schemas/010_scheduler.sql](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/database/schemas/010_scheduler.sql)
- [assistant/capability_registry.json](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/capability_registry.json)
- [assistant/custom_tools/__init__.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/custom_tools/__init__.py)
- [assistant/tool_lifecycle.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/tool_lifecycle.py)
- [assistant/learning_graph.json](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/learning_graph.json)
- [assistant/growth.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/growth.py)
- [assistant/snapshots.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/snapshots.py)
- [assistant/profiles.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/profiles.py)
- [assistant/daemon.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/daemon.py)
- [assistant/main.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/main.py)
- [assistant/router.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/router.py)
- [assistant/projects.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/projects.py)
- [assistant/recommendations.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/recommendations.py)
- [assistant/memory.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/memory.py)
- [assistant/planner.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/planner.py)
- [assistant/tools.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/tools.py)
- [assistant/cloud.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/cloud.py)
- [assistant/media.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/media.py)
- [assistant/voice.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/voice.py)
- [assistant/crawler.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/crawler.py)
- [assistant/safety.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/safety.py)
- [assistant/scheduler.py](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/assistant/scheduler.py)
- [phase.md](file:///c:/Users/Satyam%20Pandey/Desktop/personal%20assistent/phase.md)

### Open Issues
- None

### Next Recommended Task
- Await user instructions for the next functional subsystem (e.g., Local Codebase & Document Ingestion Engine / Local RAG, Git Intelligence Subsystem, or Hardware Telemetry Awareness).

### Current Progress
- **Current Development Phase:** Phase 2 Complete
- **Phase 1 Progress:** 100% completed (8/8 features)
- **Phase 2 Progress:** 100% completed (18/18 features)
- **Overall Project Progress:** 100% (26/26 features)

---

# Completion Rules

Whenever a feature is completed:

* mark it complete in this document
* update progress
* update `phase.md` if architecture changed

When every Current feature is completed:

The coding agent must stop.

It must **not** begin implementing Future features.

Wait for explicit user approval.

---

# Future Activation

Future features may only become Current when the user explicitly requests them.

When activated:

* move them from Future to Current
* preserve all previous progress
* continue development

Never automatically activate future features.

---

# Development Rules

The coding agent must always:

* build only Current features
* preserve Future features
* never delete Future plans
* always update this file before ending the session
* always keep project progress synchronized

This document is the single source of truth for implementation progress.
