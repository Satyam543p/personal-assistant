# Jarvis — Engineering Phase Document

Companion document to `prompt.md`. This file contains the engineering
rationale, architecture, budgets, build plan, risks, and explicit assumptions
behind the Jarvis local-first personal assistant.

---

## 1. Project Vision

Jarvis is a CPU-only, local-first personal assistant that interprets natural
language with a small always-on local model, executes real actions through
narrowly scoped tools, remembers the user's identity/projects/activity in
SQLite, recommends next actions when the user is unsure, and escalates to a
cloud model only for genuinely hard reasoning or coding work. The system is
built for reliability, safety, and speed first; features come second.

---

## 2. Hardware Constraints

- CPU: AMD Ryzen 5 7430U (6 cores / 12 threads, mobile, no boost-heavy design)
- RAM: 8 GB total system memory
- No dedicated GPU — all inference is CPU-bound
- Single laptop, single user, expected to run on battery part of the time

These constraints rule out any locally-hosted model larger than a few hundred
million parameters if it must run *always-on* without starving the OS and
other applications.

---

## 3. RAM Budget

Target: **always-on Jarvis footprint < 1 GB**, leaving ~7 GB for the OS, the
browser, IDEs, and other applications on an 8 GB machine.

| Component                                   | State        | Approx. RAM |
|----------------------------------------------|--------------|-------------|
| Core daemon (router, IPC, scheduler)         | always-on    | ~60–100 MB  |
| Local interpreter model (Qwen 0.5B-class, Q4 GGUF, llama.cpp) | always-on (resident) | ~400–500 MB |
| SQLite engine + memory manager               | always-on    | ~30–60 MB   |
| **Always-on subtotal**                       |              | **~550–650 MB** |
| Embedding model (e.g. MiniLM/bge-small)      | lazy-loaded, idle-unloaded | ~100–250 MB while active |
| Sandbox/tool execution processes             | spawned per-call, exit after | varies, short-lived |
| Cloud client (HTTP)                          | always-on, negligible | <10 MB |
| Speech / vision / browser-automation modules | lazy-loaded only | 0 when idle |

Headroom of ~350–450 MB is kept inside the 1 GB budget for transient spikes
(e.g., embedding model briefly active during a memory query). Nothing in the
"lazy-loaded" row counts toward steady-state budget because it is unloaded
after an idle timeout (target: 60–120 seconds of inactivity).

---

## 4. Performance Budget

- Simple intents (open known project/app, basic conversation, memory lookup)
  should resolve in well under 1 second end-to-end on CPU.
- Local model inference for a single short turn (intent classification) is
  the dominant latency cost and is the reason the model must stay small and
  quantized (Q4_K_M or similar) and resident in memory rather than reloaded
  per request.
- Deterministic shortcuts (direct registry/memory match with high confidence)
  should bypass the planner and, where safe, bypass even a full model call,
  to keep obvious actions near-instant.
- Cloud escalation latency is accepted as slower (network + larger model) and
  is reserved for tasks where correctness matters more than speed.

---

## 5. Model Strategy

- **Always-on interpreter**: a small local model (Qwen2.5-0.5B-Instruct class
  or smaller), quantized (e.g., Q4_K_M GGUF) and served via a lightweight
  local inference server (e.g., llama.cpp's server mode) so the model stays
  resident and warm instead of being reloaded per request.
- **Structured output**: grammar-constrained decoding (e.g., GBNF/JSON
  schema constraints) is used so the interpreter reliably emits the intent
  JSON object defined in `prompt.md` section 4, rather than free text that must be
  parsed heuristically.
- **Responsibilities kept local**: intent detection, entity extraction,
  ambiguity resolution, confidence scoring, short conversation, short coding
  help, summarization, routing decisions.
- **Cloud model**: used only for deep reasoning, advanced coding, long
  context, hard planning, new tool drafting, and complex debugging. The
  cloud provider is abstracted behind a single client interface so the
  backing model (Grok, or an alternative) can be swapped without touching the
  router or safety layer.

This split exists because: the local model's job is narrow and structured
(classification-shaped), which small models do reasonably well, while open
ended deep reasoning is exactly what small local models do poorly — that
work is deliberately pushed to the cloud, on purpose, rather than forced onto
hardware that cannot support it.

---

## 6. Memory Strategy

- **Primary store**: SQLite (single file, WAL mode for concurrent
  read/write safety, zero always-on daemon).
- **Semantic layer ("embeddings v2")**: a lightweight embedding model
  (e.g., MiniLM-L6 or bge-small, ~100–250 MB) is loaded on demand to embed
  new durable memories and user queries, store vectors as BLOBs inside
  SQLite, and perform similarity search in-process (brute-force cosine over
  a small per-user corpus is fast enough at this scale; a SQLite vector
  extension can be adopted later if the corpus grows). The embedding model is
  unloaded after an idle timeout — it is never a permanent daemon.
- **"v2" naming** reflects that this is a deliberate second iteration: v1
  would be raw structured-only memory (no semantic search); v2 adds the
  semantic layer plus importance scoring and conflict resolution on top,
  without introducing a separate always-on vector database service.
- **Durable memory categories** (kept, prioritized): personal identity facts,
  long-term preferences, active projects, study/learning goals, repeated
  habits, past decisions, context useful for future actions.
- **Pruning**: raw conversation turns are summarized and the summaries are
  retained; raw transcripts are not kept indefinitely.
- **Active context**: a small, fast-access table holding current pronoun
  referents and recent entities, scoped to the current session/recent window.
- **Current focus**: a single pointer (one row) identifying the active
  project/workspace, updated whenever the user switches focus.
- **Conflict handling**: each durable fact carries a timestamp and a
  specificity/source weight; newer + more specific facts override older,
  vaguer ones, and superseded facts are archived rather than deleted outright
  (for auditability).
- **Importance scoring**: each memory write gets a score from signal type
  (explicit user statement > inferred from behavior), recency, and reuse
  frequency; low-scoring memories decay and are eligible for pruning.
- **Full schema**: see section 30 for the complete `memories` table definition
  and all related tables.
- **Hybrid retrieval**: when a memory query is run, two result sets are
  produced: (a) structured SQLite query results (exact category/scope match)
  and (b) semantic vector search results (embedding cosine similarity). Merge
  rule: deduplicate by `memory.id`; rank by
  `(importance_score * 0.4) + (semantic_similarity * 0.4) + (recency_decay * 0.2)`.
  Return top-K (default: 8) to the router.
- **Session boundary**: a session starts when the user sends the first message
  and ends after 30 minutes of inactivity (configurable). On session end:
  `active_context` is snapshotted to `conversation_summaries` and then
  cleared. `current_focus` is persisted.

---

## 7. Project Registry Strategy

SQLite table `projects`:

```
id, name, aliases, folder_path, preferred_editor, run_command, tags,
last_opened_at, description, related_notes
```

- **Alias support**: the `aliases` column (JSON array) allows the user to
  register informal names: `["internship", "work project", "the React app"]`.
  Alias matching is attempted before fuzzy matching on the canonical name.
- Name resolution is fuzzy-but-bounded: exact match → alias match → fuzzy
  match above a confidence threshold → otherwise present candidates.
- **Name resolution tie-breaking**: if fuzzy match returns two projects with
  the same score, prefer the one with the more recent `last_opened_at`. If
  still tied, present both to the user.
- "Open Jarvis", "open Antigravity in VS Code", "run internship project",
  "continue my web dev work", and "resume the last project" all resolve
  through this table plus `current_focus`/`active_context`, not through
  string-guessing alone.
- Each project optionally stores recent files and last known editor state so
  "continue" requests can restore more than just the folder. See section 30
  (`workspace_snapshots` table) and section 33 (Workspace Snapshot System)
  for the full schema and restore protocol.

---

## 8. Workspace Strategy

- Workspace **profiles** (coding mode, study mode, project mode, research
  mode) are stored as lightweight presets that bias: which tools are
  surfaced first, what the recommendation engine prioritizes, and which
  project/context is assumed "current" by default.
- Switching profiles updates `current_focus` and active context, and may
  trigger lazy-loading of profile-relevant tools (e.g., research mode may
  pre-warm the web-search tool, not the embedding model).
- The assistant always tracks *one* active project at a time per profile,
  even if several are "known"; ambiguity between profiles triggers
  clarification rather than guessing.

---

## 9. Recommendation Engine Strategy

Inputs: user goals, study/learning progress, project registry + recency,
recent activity log, known skill gaps, unfinished tasks, time of day, and
recorded habits (all read from SQLite, no separate service).

- Recommendations are generated by combining: (a) the most recently active
  unfinished project/task, (b) any goal with an approaching or implied
  deadline, (c) habitual time-of-day patterns (e.g., "user usually studies at
  9pm"), and (d) skill gaps tied to active goals.
- Output is concrete and actionable: "you were last working on X", "your
  next best step is Y", optionally followed by "I can open it now" — and if
  accepted, Jarvis actually invokes the relevant tool rather than repeating
  the suggestion as text.
- Entertainment-only suggestions are explicitly excluded unless the user's
  own stated goals/habits include them.
- The scoring algorithm, weight definitions, feedback loop, and cold-start
  behavior are specified in section 32.

---

## 10. Tool Strategy

- Tools are implemented as small, independently testable capability modules
  (e.g., `open_app`, `open_project`, `search_files`, `read_file`,
  `write_file`, `search_web`, `open_video`, `run_sandboxed_code`,
  `scaffold_project`, `manage_task`, `log_tool_event`).
- Each tool declares: required inputs, side effects, risk level
  (read-only / reversible / destructive), and timeout/resource limits.
- Tool registry table `tools`: `id, name, capability, version, success_rate,
  last_used_at, risk_level, declaration, created_at, status`.
- The `declaration` field is a JSON object specifying the full allow-list:
  inputs schema, side effects, timeout_ms, memory_limit_mb, allowed_paths,
  allowed_hosts, and allowed_imports. See section 35 for the full schema.
- No tool wraps a raw shell with arbitrary argument passthrough; each tool's
  surface area is fixed and validated before any system call is made.
- Tool calls and their outcomes are written to `tool_logs` for auditability
  and for computing `success_rate`.

---

## 11. Dynamic Tool Creation Strategy

1. Router/planner determines no existing tool covers a needed capability.
2. A request is sent to the cloud model to **draft** a new tool
   implementation (treated as an unreviewed PR, not as code to run).
3. Local static analysis checks syntax, imports/dependencies against the
   allow-list (see section 35), and obvious unsafe patterns (raw `exec`,
   unrestricted filesystem/network calls outside the declared capability).
4. The draft is executed only inside a sandbox (resource- and time-limited)
   against synthetic/safe test inputs.
5. **"Passes sandbox test" definition**: a tool passes when it returns a
   value matching its declared output schema, raises no unhandled exception,
   and terminates within `timeout_ms`, across all 3+ synthetic test inputs.
   Any test case failure is a hard block on registration.
6. Only a draft that passes static checks **and** sandbox tests is
   registered into the `tools` table, versioned, and made available to the
   router.
7. If the task was flagged risky during drafting, the user must explicitly
   confirm before the new tool's first real-world use.
8. **Tool versioning contract**: when a tool is updated (new version drafted
   and validated), the old version's `tools.status` is set to `deprecated`
   (not deleted). It remains callable for 7 days (configurable grace period)
   so in-flight plans referencing the old version are not broken mid-execution.
9. Tools with degrading success rates are flagged for review or retirement.

This mirrors a real engineering review process deliberately: cloud = senior
engineer writing a PR, local system = CI pipeline + reviewer, user = final
sign-off on anything risky.

---

## 12. Safety Strategy

- **Sandboxing**: all generated/dynamic code runs in an isolated process with
  CPU time limits, memory limits, and wall-clock timeouts; no network or
  filesystem access beyond an explicit allow-list per tool. See section 35
  for the full sandbox specification.
- **Confirmation gating**: destructive/irreversible actions (delete, overwrite,
  system-level changes) require explicit user confirmation regardless of
  confidence score.
- **Confirmation UX contract**: confirmation prompts use a fixed format:
  "⚠ This will [action]. Confirm? (yes/no)" — never "do you want to proceed?"
  or other phrasing that the user might accept without reading. For voice
  input, confirmation requires a verbatim "yes, confirm" utterance, not just
  "yeah" or "ok".
- **No blind trust in model output**: both local and cloud outputs are
  treated as proposals, validated against schemas/allow-lists before any
  effect on real state.
- **Prompt-injection defense**: content read from files, web pages, or tool
  results is passed to the local model inside a clearly-delimited `<data>`
  block, structurally separate from the `<instruction>` block. The
  grammar-constrained decoder is configured to never treat content inside
  `<data>` as a routing instruction. This is enforced at the prompt-template
  level, not left to the model's judgment.
- **Anti-hallucination checks**: existence of files/apps/projects is verified
  against the filesystem/registry before claiming an action occurred.
- **Memory hygiene**: importance scoring and conflict resolution (see section 6)
  prevent low-value or contradictory data from corrupting "current state"
  reasoning.
- **Disambiguation over guessing**: multiple registry matches or low
  confidence trigger a clarifying question instead of a silent pick.
- **Loop/runaway protection**: planner steps and tool retries are bounded;
  the system fails closed (stops and reports) rather than looping.
- **Auditability**: every tool execution, escalation, and confirmation
  decision is logged with timestamp and outcome.

---

## 13. Current Architecture Decisions

- **Decoupled IPC Transport Layer**: The core runtime is fully decoupled from the communication transport protocol. The daemon implements a generic `IPCHandler` interface, and client commands use an `IPCClient` interface. The HTTP server (`HTTPIPCServer`) is one swappable implementation of `IPCServer` that binds to `localhost:7474`, allowing future transport mechanisms (e.g. Named Pipes, WebSockets, Unix Domain Sockets) to be introduced without modifying the daemon's internal modules or business logic.
- Single local SQLite database file as the system of record for all
  structured and semantic memory (no separate services).
- Always-on local model is small, quantized, and served warm via a local
  inference server rather than spawned per request.
- Router is a deterministic layer sitting *between* the local model's
  structured intent output and the execution subsystems — it does not itself
  call the model a second time except in genuinely ambiguous cases. Full
  routing table, confidence thresholds, and tie-breaking rules are specified
  in section 28.
- Embeddings and any other heavy subsystem (speech, vision, browser
  automation) are lazy-loaded, on-demand, idle-unloaded — never daemons.
- Cloud access is a single abstracted client; the specific provider is
  swappable.
- Tool lifecycle is mandatory for any new capability; there is no "fast
  path" that skips validation/sandboxing for cloud-drafted code.

---

## 14. Decisions Still Open

- Final inference runtime: llama.cpp server vs. an alternative
  (e.g., Ollama) — both are compatible with the RAM budget; choice affects
  packaging and grammar-constrained decoding ergonomics.
- Exact embedding model and whether a SQLite vector extension (e.g.
  `sqlite-vec`) is adopted now or only once the memory corpus grows large
  enough that brute-force cosine search becomes slow.
- Sandbox technology for generated code: lightweight OS-level process limits
  (e.g., Python `resource` module / Windows Job Objects) vs. container-based
  isolation (Docker) where available — affects setup complexity vs. isolation
  strength.
- Scope and timing of voice input/output — text-first is the committed v1
  scope; voice is a hook for later, not built now.
- Final cloud provider selection and exact pricing/quota handling.
- Whether project "recent files" tracking needs OS-level file-watch hooks or
  can be satisfied by tool-logged access events only.
- **Schema migration tooling**: whether to use raw SQL migration files
  (simplest) or a lightweight Python migration library (e.g.,
  `yoyo-migrations`) for more structured handling of rollbacks. The migration
  policy is defined in section 39; the tooling choice is still open.

---

## 15. Why SQLite + Embeddings v2 Is the Memory Choice

- SQLite requires no always-on daemon, has near-zero idle memory cost, and
  is durable across restarts — directly satisfying the < 1 GB always-on
  budget and the "no heavy always-on services" constraint.
- A dedicated vector database (e.g., ChromaDB) run as a daemon would add a
  persistent memory and process cost for a personal-scale memory corpus that
  does not need it — explicitly excluded as the default by the constraints.
- "Embeddings v2" — semantic search layered on top of SQLite, computed by an
  on-demand, idle-unloaded embedding model — gets most of the benefit of
  semantic recall (fuzzy "what was I working on" queries) without paying for
  a permanent service. If the corpus grows large enough that brute-force
  cosine search is too slow, a SQLite vector extension is the planned
  incremental upgrade — not a new daemon.

---

## 16. Why the Local Model Must Always Interpret Intent

- Even "obvious" commands carry hidden ambiguity ("open Jarvis" could mean a
  folder, an editor session, or a chat — resolved only through memory and the
  project registry, not string matching).
- A single mandatory interpretation step gives one consistent place to apply
  confidence scoring, entity extraction, and clarification logic, instead of
  scattering ad-hoc parsing across every tool.
- Keeping this step local (not cloud) is what makes "fast response is more
  important than model size" achievable: the highest-frequency operation in
  the whole system (parsing what the user just said) must not depend on
  network latency or a large model.

---

## 17. Why Cloud Is Only an Escalation Layer

- The hardware constraint (CPU-only, 8 GB RAM, no GPU) makes any large local
  model impractical to keep always-on without violating the RAM and speed
  budgets.
- Most real usage (opening projects, short conversation, simple lookups) does
  not need deep reasoning — sending all of it to the cloud would be slower,
  costlier, and unnecessary.
- Reserving cloud for genuinely hard reasoning, advanced coding, and tool
  drafting keeps latency and cost proportional to task difficulty, and keeps
  the system usable offline for the large majority of everyday requests.
- Cloud output is never given direct execution rights, which keeps the
  security boundary in one place (local validation/sandboxing) regardless of
  which cloud provider is used.

---

## 18. Module List

```
1. Core Daemon            – always-on process, IPC, scheduling, idle-unload timers
2. Local Interpreter       – small quantized LLM, grammar-constrained JSON output
3. Router                 – intent -> execution path decision, confidence gating
4. Memory Manager         – SQLite access, active context, current focus,
                             conflict resolution, importance scoring
5. Semantic Memory (v2)   – lazy embedding model + vector search over SQLite
6. Project & Workspace Mgr– project registry, workspace profiles, resolution logic
7. Tool Registry/Executor – capability tools, sandboxed execution, logging
8. Tool Lifecycle Manager – discover/create/validate/sandbox/register/version/retire
9. Planner                – multi-step task decomposition + stepwise execution
10. Recommendation Engine – goal/habit/time-aware next-action suggestions
11. Safety Layer          – validation, sandbox enforcement, confirmation gating,
                             prompt-injection defense, audit logging
12. Cloud Escalation Client – abstracted API client for deep reasoning/tool drafting
13. Personal Growth Engine – goal tracker, skill tracker, learning graph,
                             study session logger, growth dashboard
```

---

## 19. Data Flow

```
                         ┌───────────────────────┐
 User input  ───────────▶   Local Interpreter     │  (always-on, small model)
 (text/voice*)           │  intent + entities +    │
                         │  confidence (JSON)       │
                         └───────────┬─────────────┘
                                     │
                                     ▼
                         ┌───────────────────────┐
                         │        Router          │
                         │  reads active_context,  │
                         │  current_focus, registry │
                         └───────────┬─────────────┘
                       low confidence │     high confidence
                         or ambiguous │
                                     ▼                         ▼
                         ┌─────────────────┐        ┌────────────────────┐
                         │  Clarification    │        │  Execution Path     │
                         │  question to user │        │  selection:          │
                         └─────────────────┘        │  tool / memory /     │
                                                      │  planner / cloud      │
                                                      └─────────┬───────────┘
                                                                │
                  ┌────────────────────┬────────────────────────┼─────────────────────┐
                  ▼                    ▼                        ▼                     ▼
           ┌─────────────┐     ┌───────────────┐       ┌─────────────────┐   ┌──────────────────┐
           │ Direct Tool  │     │ Memory Lookup  │       │ Planner -> Tools  │   │ Cloud Escalation   │
           │ (Safety Layer│     │ (SQLite + v2   │       │ (sandboxed steps) │   │ (draft only, then  │
           │  wraps exec) │     │  embeddings)   │       │                   │   │  local validation) │
           └──────┬───────┘     └───────┬───────┘       └─────────┬─────────┘   └─────────┬─────────┘
                  │                     │                          │                       │
                  └─────────────────────┴──────────────────────────┴───────────────────────┘
                                                  │
                                                  ▼
                                       ┌────────────────────┐
                                       │  Result + audit log │
                                       │  written to SQLite   │
                                       └─────────┬──────────┘
                                                  ▼
                                       ┌────────────────────┐
                                       │  Response to user    │
                                       └────────────────────┘

(* voice input is a future, lazy-loaded hook — not in v1 scope)
```

---

## 20. Execution Flow

```
1. Receive user message
2. Local Interpreter -> structured intent JSON (always)
3. Router checks confidence + active_context/current_focus
     - low confidence / multi-match -> ask clarifying question, STOP here
     - after clarification answer is received: re-enter at step 3
     - else continue
4. Router selects path (full routing table in section 28):
     a. Deterministic/simple  -> call tool directly via Safety Layer
     b. Memory/context query  -> Memory Manager (+ Semantic Memory if needed)
     c. Multi-step task       -> Planner decomposes -> steps run as (a)/(b)/(d)
     d. Beyond local capability -> Cloud Escalation Client
5. Safety Layer validates any tool/code path:
     - schema/allow-list check
     - risk classification -> confirmation required? if yes, ask and wait
     - sandbox execution with timeout + memory limit
     - on failure: log, compose honest error, offer alternative or STOP
6. Tool/planner/cloud result returned
7. Memory Manager updates active_context / current_focus / durable memory
   as appropriate (with conflict + importance handling)
8. Audit log entry written (tool_logs)
9. Response composed and returned to user (concise, honest about success/failure)
```

---

## 21. What to Build First (Phase 1)

- [ ] Core daemon skeleton with IPC and idle-unload timer infrastructure
- [ ] Local interpreter integration (quantized small model, warm-served,
      grammar-constrained JSON output matching the schema in `prompt.md`)
- [ ] SQLite schema: all tables defined in section 30
- [ ] Router with confidence-based path selection (no cloud yet)
- [ ] Project registry + 3–4 core tools: `open_app`, `open_project`,
      `search_files`, `read_file`/`write_file` (scoped, validated)
- [ ] Safety Layer v1: confirmation gating + basic sandbox for any code
      execution tool
- [ ] Minimal recommendation engine using only structured SQLite data
      (no embeddings yet)

**Phase 1 definition of done:**

- [ ] Local interpreter produces valid intent JSON for 95%+ of a
      hand-written 50-command test set.
- [ ] `open_project` correctly resolves all 5 test project names (including
      aliases and fuzzy matches) with no false positives.
- [ ] Safety Layer blocks a synthetic destructive command without confirmation
      in 100% of test cases.
- [ ] Recommendation engine returns a non-empty, non-generic recommendation
      from real stored data within 200 ms.
- [ ] Always-on RAM footprint measured at < 700 MB on the target hardware
      under idle load.

## 22. What to Build Later (Phase 2+)

- [ ] Semantic memory ("embeddings v2"): lazy-loaded embedding model + vector
      search over SQLite-stored vectors
- [ ] Planner for multi-step tasks (project creation, research/download
      workflows)
- [ ] Tool lifecycle manager (cloud-drafted tool creation, static checks,
      sandbox testing, versioning, retirement)
- [ ] Cloud Escalation Client (abstracted provider integration)
- [ ] Workspace profiles (coding/study/project/research modes)
- [ ] Memory importance scoring + conflict resolution refinements
- [ ] Personal Growth Engine (goal tracker, skill tracker, learning graph,
      study session logger)
- [ ] Workspace Snapshot System + Continue Working protocol
- [ ] Recommendation Engine algorithm with feedback loop (section 32)
- [ ] YouTube Learning Pipeline (section 41)
- [ ] Autonomous Research Workflow (section 42)
- [ ] Lawful media/download tools (subtitle/audio extraction for
      user-approved content) via the tool lifecycle
- [ ] Optional: lazy-loaded voice input/output hooks
- [ ] Optional: SQLite vector extension if semantic corpus growth requires it

**Phase 2 definition of done:**

- [ ] Semantic memory query returns a relevant result (human-judged) for
      80%+ of a 20-query test set.
- [ ] Planner successfully executes a 5-step "scaffold new project" workflow
      end-to-end, including rollback on a synthetic step-3 failure.
- [ ] First cloud-drafted tool passes full lifecycle (static check → sandbox
      → registration) within a single session.
- [ ] Workspace snapshot captured and fully restored (all files exist and
      open correctly) in a resume-after-restart test.
- [ ] Cloud escalation fails gracefully (local-only degraded mode, honest
      error message) when the cloud endpoint is unreachable.

---

## 23. Edge Cases & Failure Modes

- Ambiguous project/app names with multiple registry matches.
- User references "it"/"that" with no resolvable active context.
- Local model misclassifies intent with high stated confidence (calibration
  drift) — mitigated by periodic confidence-threshold review, not by trusting
  the score blindly.
- Tool reports success but the underlying OS action silently failed (e.g.,
  app crashed immediately after launch) — mitigated by post-action
  verification where feasible.
- Cloud API outage or rate limiting during an escalation — must degrade to
  "local-only capability, here's what I can't do right now" rather than
  hanging or fabricating output.
- Generated tool code that passes sandbox tests but behaves differently
  against real inputs — mitigated by conservative resource limits and
  required user confirmation on first real use.
- Memory corpus growing large enough that brute-force semantic search becomes
  slow — mitigated by the planned vector-extension upgrade path.
- Conflicting "current focus" across rapid context switches — mitigated by
  recency + explicit confirmation when switches happen unusually fast.
- Disk/SQLite file corruption — mitigated by WAL mode and periodic backups
  of the database file.
- **Study session interrupted by crash**: on restart, detect incomplete
  `study_sessions` rows (started_at set, ended_at null, older than 2 hours).
  Prompt: "Looks like you were watching [resource] before the last restart.
  Did you finish it?" Allow user to mark complete, incomplete, or ignore.
- **Recommendation accepted but tool to execute it is missing**: do not
  present a recommendation whose execution requires a missing tool unless the
  user has previously consented to dynamic tool creation. If missing, flag
  the recommendation as "requires new capability" rather than surfacing it
  silently and failing on acceptance.
- **Skill confidence decay below floor**: if a skill's confidence decays to
  0.1 (minimum floor), stop decaying and surface a reminder: "You haven't
  practiced [skill] in [N] days. Want to schedule some time?"
- **Goal deadline passed with incomplete progress**: mark goal
  `status = paused` (not abandoned) and surface a prompt: "Your goal '[X]'
  passed its target date at [progress]% completion. Extend the deadline or
  mark it done?"

---

## 24. Open Risks

- Small local model quality ceiling: intent classification accuracy on
  unusual phrasing may need prompt/grammar tuning over time, or a slightly
  larger (but still small) model swap if accuracy is insufficient.
- Sandbox isolation strength depends on the final sandbox technology choice
  (section 14); OS-level resource limits are weaker than container isolation.
- Cloud provider dependency and cost exposure for heavy escalation usage.
- User trust calibration: if confirmation prompts are too frequent, users may
  habitually approve without reading — mitigated by reserving confirmation
  for genuinely risky actions only, per `prompt.md` section 12.
- Multi-application "recent files" tracking may require OS-specific
  integration work not yet scoped.
- **Recommendation weight drift**: if the feedback loop (section 32) runs too
  aggressively, weights can diverge far from their defaults, causing the
  engine to obsess over one signal type and ignore others. Mitigate by
  clamping weights to `[default * 0.5, default * 2.0]` and logging when a
  weight reaches its bound.
- **Study session quality inflation**: if the user consistently rates sessions
  highly, the skill tracker will overestimate skill levels and produce poor
  recommendations. Mitigate by anchoring quality partly to session duration,
  not entirely to self-report.
- **Workspace snapshot staleness for large projects**: if a project has
  hundreds of open files, the snapshot file list can become unwieldy.
  Mitigate by capping snapshotted files at 20 (the most recently accessed in
  this session).
- **SQLite WAL file growth**: under heavy write loads, WAL files can grow
  large before checkpointing. Mitigate by running
  `PRAGMA wal_checkpoint(TRUNCATE)` at session end.

---

## 25. Next Milestones

1. Local interpreter serving structured JSON intents reliably (accuracy spot
   check against a hand-written test set of representative commands).
2. SQLite schema + project registry + first 4 tools working end-to-end for
   "open X" style commands with correct disambiguation behavior.
3. Safety Layer enforcing confirmation + sandbox limits on at least one
   code-execution-capable tool.
4. Minimal recommendation engine producing a "you were last working on X,
   next step is Y" response from real stored data.
5. Semantic memory layer added and benchmarked for latency/idle-unload
   behavior within the RAM budget.
6. Planner + first multi-step workflow (e.g., scaffold a new project).
7. Cloud escalation client + first end-to-end cloud-drafted tool passing the
   full lifecycle into the registry.

---

## 26. Engineering Plan Summary

Build bottom-up from the always-on core outward: get the small local model
reliably producing structured intent, wire it to a minimal SQLite-backed
registry and a handful of safe tools, and enforce the safety layer from day
one rather than bolting it on later. Only after that loop is solid should
semantic memory, the planner, the recommendation engine, and cloud escalation
be layered in — each as an additive capability behind the same router and
safety boundary, never as a bypass of it. Heavy/optional subsystems (speech,
vision, browser automation, embeddings) are designed as lazy-loaded plug-ins
from the start so the always-on budget never has to be renegotiated later.

---

## 27. Practical Assumptions

The brief did not pin down every implementation detail. The following
assumptions were made to produce a concrete, buildable spec; all are
reasonably easy to change later without altering the architecture:

- **Operating system**: no OS was specified. The design assumes a Windows or
  Linux laptop and isolates all app/project "launch" behavior behind a tool
  interface specifically so the underlying OS-specific launch mechanism can
  be swapped without touching the router, memory, or safety layers.
- **Concrete local model pick**: the brief said "Qwen 0.5B or smaller" as an
  example; this spec treats Qwen2.5-0.5B-Instruct (quantized GGUF) as the
  concrete v1 choice, swappable for a smaller model if accuracy/latency
  testing recommends it.
- **Concrete embedding model pick**: a small CPU-friendly sentence-embedding
  model (MiniLM-L6 or bge-small class, ~100–250 MB) is assumed for
  "embeddings v2"; this is an implementation detail, not an architectural
  commitment.
- **Cloud provider**: the brief names Grok as an example cloud model. The
  architecture treats the cloud provider as fully abstracted/swappable
  behind one client interface, so this is a configuration choice, not a hard
  dependency.
- **Sandbox technology**: OS-level process resource limits are assumed as the
  v1 sandbox mechanism (lowest setup complexity), with container-based
  isolation (Docker) noted as a stronger optional upgrade rather than a v1
  requirement.
- **Voice input/output**: the brief mentions guarding against "bad
  voice-to-text matches," implying voice may be added later, but does not
  request it as a v1 feature. Voice is therefore treated as an explicit
  future hook (lazy-loaded, not built in Phase 1), not part of the initial
  scope.
- **Single-user system**: no multi-user/multi-tenant requirement was stated,
  so the entire design (memory, registry, recommendations) assumes one
  local user profile.
- **Vector search method**: brute-force cosine similarity over SQLite-stored
  vectors is assumed sufficient at personal-memory scale for v1, with a
  SQLite vector extension as the planned upgrade only if/when corpus size
  demands it — explicitly avoiding a permanent vector-DB daemon as required.

---

## 28. Router Specification

The Router is the deterministic dispatch layer between the local interpreter's
intent JSON and all downstream execution subsystems. It does not call the
model a second time except to generate a clarifying question.

**Input:** the intent JSON object from the local interpreter (schema defined
in `prompt.md` section 4).

**Routing Table (priority order):**

```
1. confidence < THRESHOLD_LOW (default: 0.55)
   → emit clarifying question, store pending_intent in active_context, STOP

2. intent == "conversation" AND confidence >= THRESHOLD_HIGH (default: 0.85)
   → local model generates short reply directly, skip all other subsystems

3. intent in DIRECT_TOOL_INTENTS
   AND resolved_reference is not null
   AND confidence >= THRESHOLD_HIGH
   → Safety Layer → direct tool call (bypass planner)

4. intent in DIRECT_TOOL_INTENTS
   AND (resolved_reference is null OR confidence in [THRESHOLD_LOW, THRESHOLD_HIGH))
   → Memory Manager lookup first → re-evaluate → if still unresolved: clarify

5. intent == "multi_step_task"
   OR (intent in DIRECT_TOOL_INTENTS AND requires_plan flag set by router heuristic)
   → Planner → step-by-step execution via Safety Layer

6. intent in CLOUD_ESCALATION_INTENTS
   OR (cloud_score > CLOUD_THRESHOLD computed from complexity signals)
   → Cloud Escalation Client

7. intent == "memory_query" OR "recommend_next_action"
   → Memory Manager (+ Semantic Memory if semantic flag set)
```

**Threshold values (tunable, stored in config, not hard-coded):**

| Parameter | Default | Notes |
|---|---|---|
| `THRESHOLD_LOW` | 0.55 | Below this: always clarify |
| `THRESHOLD_HIGH` | 0.82 | Above this: skip clarification |
| `CLOUD_THRESHOLD` | 0.65 (complexity score) | Above this: escalate |
| `MAX_CLARIFY_ROUNDS` | 2 | After 2 unresolved clarifications, surface raw options |

**Tie-breaking:** if two routes qualify at the same priority, prefer the
lower-latency path (tool > memory > planner > cloud).

**Failure cascade:** if the selected path fails (tool error, sandbox
rejection, cloud timeout), the router re-routes to the next available path in
priority order, with the original path's failure logged. If all paths fail,
compose an honest failure response and stop.

**State written after routing:** `active_context.last_intent`,
`active_context.last_route`, `active_context.pending_intent` (if
clarification issued).

---

## 29. Planner Specification

The Planner is invoked for any multi-step task. It decomposes a goal into a
sequenced, dependency-aware step list before any execution begins.

**Step representation:**

```python
@dataclass
class PlanStep:
    step_id: str              # uuid
    plan_id: str              # parent plan uuid
    index: int                # execution order (0-based)
    intent: str               # tool name or subsystem
    inputs: dict              # resolved inputs for this step
    depends_on: list[str]     # step_ids that must succeed first
    risk_level: str           # "read_only" | "reversible" | "destructive"
    status: str               # "pending" | "running" | "done" | "failed" | "skipped"
    result: dict | None       # populated after execution
    error: str | None         # populated on failure
    rollback_action: str | None  # tool name for undo, if applicable
```

**Plan execution contract:**

- Steps execute in `index` order, respecting `depends_on`.
- If a step fails:
  - Log failure with partial state achieved.
  - Attempt rollback actions for completed steps (in reverse order) where
    `rollback_action` is defined.
  - Surface the failure to the user with a summary of what succeeded and
    what did not.
  - Do **not** silently continue to later steps.
- Maximum steps per plan: 20 (configurable). Plans exceeding this are
  rejected and the user is prompted to break the task down.
- Maximum execution time per plan: 5 minutes wall-clock. Long-running plans
  must checkpoint state to SQLite so they survive a crash.

**Plan storage:** plans and steps are persisted to `plans` and `plan_steps`
SQLite tables so they survive a restart. On startup, in-progress plans are
resumed from the last completed step.

**Planner SQLite schema:**

```sql
CREATE TABLE plans (
  id TEXT PRIMARY KEY,
  goal TEXT NOT NULL,
  status TEXT NOT NULL,  -- pending | running | done | failed
  created_at INTEGER NOT NULL,
  completed_at INTEGER
);

CREATE TABLE plan_steps (
  id TEXT PRIMARY KEY,
  plan_id TEXT REFERENCES plans(id),
  index_order INTEGER NOT NULL,
  intent TEXT NOT NULL,
  inputs TEXT NOT NULL,  -- JSON
  depends_on TEXT,       -- JSON array of step ids
  risk_level TEXT NOT NULL,
  status TEXT NOT NULL,
  result TEXT,           -- JSON
  error TEXT,
  rollback_action TEXT
);
```

---

## 30. Full SQLite Schema

All tables. Schema version tracked in `schema_meta` to support migrations.

```sql
-- Schema version tracking
CREATE TABLE schema_meta (
  version INTEGER NOT NULL,
  applied_at INTEGER NOT NULL
);

-- User profile (single row)
CREATE TABLE profile (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  name TEXT,
  timezone TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Long-term preferences (key-value)
CREATE TABLE preferences (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Project registry
CREATE TABLE projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  aliases TEXT,               -- JSON array
  folder_path TEXT NOT NULL,
  preferred_editor TEXT,
  run_command TEXT,
  tags TEXT,                  -- JSON array
  description TEXT,
  related_notes TEXT,
  last_opened_at INTEGER,
  created_at INTEGER NOT NULL
);

-- Per-project workspace snapshots
CREATE TABLE workspace_snapshots (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id),
  snapshot_type TEXT NOT NULL,  -- "session_end" | "manual" | "auto_interval"
  open_files TEXT,              -- JSON array of file paths
  editor_state TEXT,            -- JSON: scroll position, cursor, split layout
  active_branch TEXT,           -- git branch if applicable
  captured_at INTEGER NOT NULL
);

-- Durable memories
CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  category TEXT NOT NULL,  -- "identity" | "preference" | "goal" | "habit" | "decision" | "project_context"
  scope TEXT NOT NULL,     -- "global" | project_id for project-specific memories
  content TEXT NOT NULL,
  embedding BLOB,          -- stored only when semantic layer is active
  importance_score REAL NOT NULL DEFAULT 0.5,
  source TEXT NOT NULL,    -- "explicit_statement" | "inferred" | "tool_result"
  created_at INTEGER NOT NULL,
  last_accessed_at INTEGER,
  access_count INTEGER NOT NULL DEFAULT 0,
  superseded_by TEXT,      -- memory id that replaces this one (archive, not delete)
  specificity_weight REAL NOT NULL DEFAULT 1.0
);

-- Active context (single-session, fast-access)
CREATE TABLE active_context (
  key TEXT PRIMARY KEY,    -- "current_pronoun_ref" | "last_intent" | "last_route" | "pending_intent" etc.
  value TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Current focus pointer (single row)
CREATE TABLE current_focus (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  project_id TEXT REFERENCES projects(id),
  workspace_mode TEXT,     -- "coding" | "study" | "research" | "project"
  updated_at INTEGER NOT NULL
);

-- Goals
CREATE TABLE goals (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT,
  category TEXT,           -- "skill" | "project" | "habit" | "career" | "study"
  status TEXT NOT NULL,    -- "active" | "paused" | "completed" | "abandoned"
  target_date INTEGER,
  progress_pct REAL NOT NULL DEFAULT 0.0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Goal progress events
CREATE TABLE goal_events (
  id TEXT PRIMARY KEY,
  goal_id TEXT REFERENCES goals(id),
  event_type TEXT NOT NULL,  -- "progress_update" | "milestone" | "note" | "completed"
  value REAL,
  note TEXT,
  occurred_at INTEGER NOT NULL
);

-- Skill tracking
CREATE TABLE skills (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT,            -- "programming" | "tool" | "domain" | "language" etc.
  level TEXT NOT NULL,      -- "beginner" | "intermediate" | "advanced" | "expert"
  confidence REAL NOT NULL DEFAULT 0.5,
  last_practiced_at INTEGER,
  related_goal_ids TEXT,    -- JSON array
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

-- Study / learning sessions
CREATE TABLE study_sessions (
  id TEXT PRIMARY KEY,
  topic TEXT NOT NULL,
  skill_id TEXT REFERENCES skills(id),
  resource_url TEXT,
  resource_type TEXT,       -- "video" | "article" | "book" | "course" | "hands_on"
  duration_minutes INTEGER,
  notes TEXT,
  quality REAL,             -- 0.0-1.0 self-rated or inferred
  started_at INTEGER NOT NULL,
  ended_at INTEGER
);

-- Tools registry
CREATE TABLE tools (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  capability TEXT NOT NULL,
  version TEXT NOT NULL,
  risk_level TEXT NOT NULL,  -- "read_only" | "reversible" | "destructive"
  declaration TEXT NOT NULL, -- JSON: inputs schema, side_effects, timeout_ms, memory_limit_mb, allow_list
  success_rate REAL NOT NULL DEFAULT 1.0,
  last_used_at INTEGER,
  created_at INTEGER NOT NULL,
  status TEXT NOT NULL       -- "active" | "deprecated" | "retired"
);

-- Tool execution log
CREATE TABLE tool_logs (
  id TEXT PRIMARY KEY,
  tool_id TEXT REFERENCES tools(id),
  plan_step_id TEXT,         -- null if called directly
  input TEXT NOT NULL,       -- JSON
  output TEXT,               -- JSON
  success INTEGER NOT NULL,  -- 0 | 1
  error TEXT,
  duration_ms INTEGER,
  executed_at INTEGER NOT NULL
);

-- Recommendation history
CREATE TABLE recommendations (
  id TEXT PRIMARY KEY,
  recommendation_text TEXT NOT NULL,
  basis TEXT NOT NULL,       -- JSON: which goals/projects/habits contributed
  accepted INTEGER,          -- null=not yet, 0=rejected, 1=accepted
  outcome TEXT,              -- null | "completed" | "abandoned" — set later
  generated_at INTEGER NOT NULL,
  responded_at INTEGER
);

-- Conversation summaries
CREATE TABLE conversation_summaries (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  summary TEXT NOT NULL,
  key_entities TEXT,         -- JSON array of extracted entities
  started_at INTEGER NOT NULL,
  ended_at INTEGER NOT NULL
);

-- System error log
CREATE TABLE system_errors (
  id TEXT PRIMARY KEY,
  component TEXT NOT NULL,  -- "router" | "planner" | "memory" | "sandbox" | "cloud" | "interpreter"
  severity TEXT NOT NULL,   -- "debug" | "info" | "warning" | "error" | "critical"
  message TEXT NOT NULL,
  context TEXT,             -- JSON: relevant state at time of error
  occurred_at INTEGER NOT NULL
);

-- Plans and plan steps (see section 29)
CREATE TABLE plans (
  id TEXT PRIMARY KEY,
  goal TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  completed_at INTEGER
);

CREATE TABLE plan_steps (
  id TEXT PRIMARY KEY,
  plan_id TEXT REFERENCES plans(id),
  index_order INTEGER NOT NULL,
  intent TEXT NOT NULL,
  inputs TEXT NOT NULL,
  depends_on TEXT,
  risk_level TEXT NOT NULL,
  status TEXT NOT NULL,
  result TEXT,
  error TEXT,
  rollback_action TEXT
);
```

**Schema versioning policy:** any schema change increments
`schema_meta.version` and ships with a corresponding migration script
(`migrations/v{N}_to_v{N+1}.sql`). On startup, the daemon checks the version
and runs pending migrations before any other operation.

---

## 31. Personal Growth Engine

This subsystem tracks who the user is becoming, not just what they are doing.
It is the data backbone of the recommendation engine.

**Goal Tracker:** reads/writes the `goals` and `goal_events` tables. Infers
progress from: study session completions (tool log events tagged to a goal),
explicit user statements ("I finished that chapter"), and periodic check-ins
prompted by the recommendation engine. Progress events trigger re-scoring of
the recommendation engine's priority weights.

**Skill Tracker:** reads/writes `skills`. Skill level is not directly
user-entered. Instead it is inferred from: study session count + quality on a
topic, successful tool executions in a skill domain, and explicit corrections
("I already know that"). Skill levels decay slightly over time if the skill
has not been practiced (configurable decay half-life, default 90 days).

**Learning Graph:** a logical layer over `skills` and `goals` — not a
separate table. The learning graph answers: "what skills does goal G require?
which of those are weak? what's the next skill to develop given current
levels?" This is a static adjacency definition (JSON config file:
`learning_graph.json`) mapping goals to required skill clusters. The system
does not auto-generate this graph; the user and/or agent defines it and
updates it over time.

**Study Session Logger:** every time Jarvis opens a video, article, or course
resource, a `study_sessions` entry is created with start time, resource URL,
and inferred topic. End time and quality are written on session close. If
duration is under 2 minutes, the session is discarded (probably an accidental
open). Sessions link to a `skill_id` via topic classification (local model
inference, stored result, not re-inferred).

**Growth Dashboard:** a tool that reads `goals`, `skills`, `study_sessions`
and produces a human-readable summary: "This week you studied Python for 4
hours. Your data structures skill is at intermediate. Your goal 'complete DSA
prep' is 38% done, target date in 23 days." This is Recommendation Engine
input and also user-facing on request.

---

## 32. Recommendation Engine Algorithm

The recommendation engine is a scoring function, not a language model call.
It runs deterministically from SQLite data.

**Scoring formula per candidate recommendation:**

```
score(candidate) =
    w_goal       * goal_urgency(candidate)
  + w_habit      * habit_match(candidate, current_time)
  + w_recency    * recency_bonus(candidate)
  + w_skill_gap  * skill_gap_relevance(candidate)
  + w_unfinished * unfinished_task_bonus(candidate)
  - w_rejection  * recent_rejection_penalty(candidate)
```

**Default weights** (stored in `preferences`, tunable):

| Weight | Default | Meaning |
|---|---|---|
| `w_goal` | 0.35 | Distance to goal deadline × goal importance |
| `w_habit` | 0.20 | Match between time-of-day and recorded usage pattern |
| `w_recency` | 0.15 | Time since last interaction with this project/topic |
| `w_skill_gap` | 0.20 | Severity of skill gap tied to active goal |
| `w_unfinished` | 0.25 | Incomplete task or interrupted session |
| `w_rejection` | 0.30 | Penalty if user rejected this same recommendation recently |

**Output:** top-3 scored candidates, each with a one-line rationale ("You
haven't touched your DSA prep in 4 days and your target date is 3 weeks
away").

**Cold-start behavior:** when history is empty (new install), fall back to
prompting the user to set one goal and one current project. Until that is
done, recommendations output only "Tell me one thing you're working on and
I'll help you stay on track."

**Feedback loop:** when a recommendation is accepted (`recommendations.accepted = 1`)
and the outcome is later marked `completed`, the weights for that signal type
are nudged upward by a small factor (default 0.02). Rejected recommendations
that were accepted recently with bad outcomes nudge that signal type downward.
Weight update runs at session end. Weights are clamped to
`[default * 0.5, default * 2.0]` to prevent drift.

**Feedback capture:** after executing a recommendation, Jarvis checks at the
next session whether the referenced goal/task made progress. If yes, records
positive outcome. If the user abandoned mid-session (inferred from
`study_sessions.duration_minutes` < 10), records neutral. This runs as a
low-priority background task on session end, not as a blocking call.

---

## 33. Workspace Snapshot System & Continue Working Protocol

**Snapshot capture triggers:**

- Session end (user says goodbye, closes the UI, or daemon detects N minutes
  idle).
- Manual: "save my workspace."
- Auto-interval: every 30 minutes if a project is active (configurable).

**What is captured** (written to `workspace_snapshots`):

- List of open files at time of capture (from tool log: last
  `read_file`/`write_file` calls on this project in this session), capped at
  20 most recently accessed files.
- Editor state blob: if the active editor exposes a state API (VS Code
  workspace file, Vim session file), that file is read and stored as JSON.
  If not available, only the file list is stored.
- Active git branch (if `.git` exists in project folder):
  `git rev-parse --abbrev-ref HEAD`.
- Timestamp.

**Restore protocol ("continue" / "resume" requests):**

1. Load most recent `workspace_snapshots` row for `project_id`.
2. Validate each file in `open_files` still exists on disk. Files that no
   longer exist are flagged in the restore summary.
3. Open project folder in preferred editor (via `open_project` tool).
4. If editor state blob exists and editor supports it, restore editor state.
   Otherwise open the validated files in the editor.
5. Restore `current_focus` to this project.
6. Report: "Resumed [project]. Last session: [date]. [N] files restored.
   [M] files missing."

**Staleness policy:** if the snapshot is older than 7 days, add a note to
the resume summary ("This snapshot is 12 days old — things may have
changed"). Do not block restoration; just inform.

---

## 34. Context Recovery After Restart

On any startup (clean boot, crash recovery, or daemon restart), the system
reconstructs enough context to resume normal operation without asking the
user "who are you."

**Startup sequence:**

```
1. Open SQLite (WAL mode; if WAL recovery needed, let SQLite handle it)
2. Read schema_meta.version; run pending migrations
3. Load profile (single row) — if missing: run first-time setup wizard
4. Load current_focus (single row) — may be null on first run
5. Load active_context rows into in-process fast-access dict
6. Load top-5 most recent memories (by last_accessed_at) into working set
7. Load active goals (status = 'active') — feed into recommendation engine
8. Check for in-progress plans (plans.status = 'running') — surface to user:
   "You had an unfinished task: [goal]. Resume?"
9. Local interpreter: warm-start (model already resident; verify it responds
   to a test inference before accepting user input)
10. Router: ready
```

**What is NOT rebuilt on startup:** full conversation history (summaries are
sufficient). Raw session transcripts. Embedding model (lazy-loaded on first
semantic query).

**Crash recovery:** if `active_context.last_route` is `planner` and a plan
is `running`, treat that plan as interrupted. Present the partial state to
the user. Do not auto-resume a destructive plan without explicit
confirmation.

---

## 35. Sandbox Specification

**Per-tool allow-list fields** (stored in `tools.declaration` JSON):

```json
{
  "inputs_schema": { "...": "JSON Schema" },
  "side_effects": ["filesystem_read", "filesystem_write", "subprocess", "network"],
  "timeout_ms": 5000,
  "memory_limit_mb": 128,
  "allowed_paths": ["/home/user/projects/", "/tmp/jarvis_sandbox/"],
  "allowed_hosts": [],
  "allowed_imports": ["os.path", "pathlib", "subprocess", "json", "re"]
}
```

**Static analysis checks for dynamically created tools:**

- No `exec()`, `eval()`, `__import__()`, `compile()`, `open()` outside
  `allowed_paths`.
- No `socket`, `urllib`, `requests`, `httpx`, `subprocess` unless listed in
  `side_effects`.
- No `ctypes`, `cffi`, `multiprocessing`, direct syscall wrappers.
- Imports must be a subset of `allowed_imports` plus the Python standard
  library whitelist.
- No relative imports that could escape the tool directory.

**Sandbox enforcement at runtime (v1 — OS-level):**

- Python: `resource.setrlimit` for CPU time and memory.
- Subprocess: spawned with `cwd` set to `/tmp/jarvis_sandbox/{tool_id}/`,
  `env` stripped to minimal safe set.
- No network: `LD_PRELOAD` intercept or process-level network namespace
  (Linux), or firewall rule per process (Windows).
- Wall-clock timeout: `subprocess.Popen` with `communicate(timeout=N)`.

**Sandbox enforcement (v2 — Docker, optional upgrade):** spawn tool as a
Docker container with `--network none --memory 128m --cpus 0.5 --read-only`
and a mounted `/tmp/jarvis_sandbox` volume. Stronger isolation; requires
Docker installed.

**"Passes sandbox test" definition:** the tool returns a value matching its
declared output schema, raises no exception, and terminates within
`timeout_ms`. A test suite of at minimum 3 synthetic inputs (provided by the
cloud model that drafted the tool) must all pass. Any test case failure is a
hard block on registration.

---

## 36. Cloud Escalation — Retry, Budget, and Caching Policy

**Retry policy:**

```
Max retries: 3
Backoff: exponential (1s, 2s, 4s)
Retry on: HTTP 429 (rate limit), HTTP 5xx (server error), network timeout
Do NOT retry on: HTTP 400 (bad request), HTTP 401 (auth), token limit exceeded
On exhausted retries: fail with explicit "cloud unavailable" message; do not fabricate
```

**Token budget:**

- Soft limit per call: 8 000 tokens (input + output).
- Hard limit per call: 16 000 tokens (configurable in `preferences`).
- If context would exceed the soft limit, the router summarizes/truncates
  before escalating.
- Monthly usage counter stored in `preferences.cloud_tokens_used_this_month`.
  If it exceeds the user-configured monthly cap
  (`preferences.cloud_monthly_token_cap`, default: 500 000), escalation is
  blocked and the user is warned.

**Response caching:**

- Cloud responses for identical (intent, context hash) pairs are cached in
  SQLite for 24 hours.
- Cache key: `SHA256(intent_json + relevant_context_snapshot)`.
- Cache hits skip the cloud call entirely.
- Cache is invalidated if any tool in the response's execution plan changes.

**Offline detection:** before any cloud call, a lightweight connectivity
check (TCP connect to the cloud endpoint, 1-second timeout). If it fails,
route to local-only path immediately without attempting the full call.

---

## 37. Failure State Machine

```
State: INTERPRETING
  Failure: local model returns malformed JSON
  Action: retry with strict grammar constraint enforcement (1 retry)
         → if still malformed: ask user to rephrase
  Next state: IDLE

State: ROUTING
  Failure: no route matches above threshold
  Action: emit clarification question
         → after 2 failed clarifications: surface raw intent options to user
  Next state: AWAITING_CLARIFICATION

State: TOOL_EXECUTING
  Failure: tool returns error
  Action: log failure, compose honest failure message
         → if alternative tool exists: offer it
         → if no alternative: report and STOP
  Next state: IDLE

State: PLANNING
  Failure: planner step fails mid-sequence
  Action: attempt rollback of completed steps (reverse order)
         → report partial state: "Steps 1-3 completed. Step 4 failed: [reason]"
         → offer to retry step 4 only, or abort
  Next state: AWAITING_USER_DECISION

State: CLOUD_ESCALATING
  Failure: cloud returns error / timeout
  Action: exhaust retry policy
         → report cloud unavailable, offer local-only degraded alternatives
  Next state: IDLE (degraded)

State: MEMORY_WRITING
  Failure: SQLite write error
  Action: log error, continue (memory loss is recoverable; action was completed)
         → alert system_errors log
  Next state: continue normally

State: SANDBOX_EXECUTING
  Failure: timeout or memory limit exceeded
  Action: kill process, log failure, report tool failure
         → do NOT use partial output from the killed process
  Next state: TOOL_EXECUTING failure path above
```

---

## 38. Observability & Diagnostics

**Structured error log:** `system_errors` table (schema defined in section 30).

**Performance metrics** (tracked in `preferences` or a dedicated `metrics`
table):

- `avg_interpretation_latency_ms` (rolling 100-call average)
- `avg_tool_execution_latency_ms` per tool name
- `confidence_histogram` (distribution of confidence scores over last 1 000
  requests)
- `cloud_escalation_rate` (fraction of requests escalated over last 7 days)

**Confidence calibration tracking:** every interpretation's stated confidence
is compared against the eventual outcome (did the action succeed on first try,
or require clarification/re-routing?). If the model is systematically
overconfident, `THRESHOLD_HIGH` is automatically nudged upward. This runs as
a weekly background job.

**Developer diagnostic mode:** a CLI flag (`--diagnostic`) or
`preferences.diagnostic_mode = true` dumps to stdout every intent JSON,
every routing decision, every tool call input/output, and every memory write.
Disabled by default. Never exposed in the user-facing interface.

**Health check endpoint:** the core daemon exposes a local HTTP endpoint
(`localhost:7474/health`) returning:

```json
{
  "status": "ok",
  "interpreter": "ready",
  "sqlite": "ok",
  "embedding_model": "idle",
  "cloud_reachable": true,
  "active_project": "Jarvis",
  "memory_mb": 612
}
```

---

## 39. Schema Migration Policy

**Version tracking:** `schema_meta` stores the current version integer. On
startup, the daemon reads this version and runs any migration scripts with a
higher version number, in order.

**Migration file convention:** `migrations/v{N}_to_v{N+1}.sql`. Append-only
SQL files — no DROP TABLE or destructive ALTER without an explicit archiving
step first.

**Safe migration rules:**

- Adding columns: `ALTER TABLE ... ADD COLUMN` with a DEFAULT.
  Always backward-compatible.
- Renaming columns: create new column, copy data, deprecate old (drop in a
  later migration after a release cycle).
- Adding tables: always safe.
- Removing tables: archive data first
  (`INSERT INTO archive_X SELECT * FROM X`); then DROP.
- Data migrations: run inside a transaction. On failure, roll back and halt
  startup with a clear error.

**Backup before migration:** before running any migration, the daemon copies
the SQLite file to `jarvis_backup_v{N}.db`. This is a hard requirement.

---

## 40. Dynamic Capability Discovery

**Proactive gap detection:** when the recommendation engine builds a
recommendation, it checks whether the tool required to execute that
recommendation exists in `tools`. If missing, the recommendation is surfaced
but flagged: "I'd need to create a new capability to do this. Want me to
draft it?"

**Reactive gap detection:** router/planner hits a missing tool during
execution and triggers the tool lifecycle (section 11).

**Capability registry:** a separate lightweight JSON file
(`capability_registry.json`) listing all known capability classes with their
canonical names and implementing tools. This is the lookup table for gap
detection.

```json
{
  "open_browser_url": { "tools": ["open_url_tool"], "status": "active" },
  "extract_youtube_transcript": { "tools": [], "status": "missing" },
  "send_email": { "tools": [], "status": "not_planned" }
}
```

On startup, the daemon compares this registry against the `tools` table and
flags discrepancies (registry stale or tool created without registry update).

---

## 41. YouTube Learning Pipeline

When the user requests "watch a tutorial on X" or "find a video about Y":

**Step 1 — Search:** `search_youtube` tool calls the YouTube Data API (or
scrapes search results if no API key) with the topic + "tutorial" keyword
filter. Returns top-5 results with title, duration, channel.

**Step 2 — Select:** if the request names a specific channel (resolved via
memory), open that result directly. Otherwise present top results and let the
user pick.

**Step 3 — Open:** `open_url` tool opens the selected video in the default
browser.

**Step 4 — Log:** a `study_sessions` row is created on open with
`resource_type = "video"`, `resource_url`, inferred topic (local model
classifies from title), and `started_at`.

**Step 5 — Close detection:** either the user explicitly says "done" /
"finished", or after a configurable idle window (default 45 minutes) the
system writes `ended_at` and asks "Did you finish that video?"

**Step 6 — Skill update:** if the video maps to a tracked skill via topic
classification, `skills.last_practiced_at` is updated and `confidence` is
nudged up (default +0.05, capped at 1.0).

**No downloading by default.** Downloading requires explicit user request and
lawful-use confirmation per `prompt.md` sections 15/16. A
`download_video_audio` tool may be added via the tool lifecycle for offline
use cases with appropriate legal checks.

---

## 42. Autonomous Research Workflow

When the user requests "research X" or "find me information about Y":

**Planner decomposition:**

```
Step 1: search_web(query=X, num_results=5)
Step 2: For each result URL, fetch_page_content(url) → extract key points
        (local model summary)
Step 3: Synthesize summaries (local model if short; cloud if long/complex)
Step 4: write_file(path="research/{topic}_{date}.md", content=synthesis)
Step 5: Log study_session(topic=X, resource_type="article", ...)
```

**Source quality filter:** a static allowlist of high-quality domains
(configurable) scores each source before summarization. Low-trust pages are
flagged in the output.

**Depth control:** "quick overview" = 1-2 sources, local summary. "Deep
research" = 5+ sources, cloud synthesis. Default is "quick" to respect the
cloud cost budget.

**Output format:** always a Markdown file in the research folder, with source
URLs cited and key takeaways bulleted. A "confidence" note is added if sources
conflict. The user receives a file link and a 2-sentence summary in the chat
interface, not a wall of text.

---

## 43. Project-Specific Memory

Global memory (section 6) is for facts about the user. Project-specific
memory is for facts about a project that would not make sense in a different
context.

**Storage:** `memories` table with `scope = project_id` (not `global`).

**Examples:**

- "This project uses Python 3.11, not system Python."
- "The main branch is called `main` but deployments use `prod`."
- "Last known blocker: the auth middleware is broken, waiting on API keys."
- "Preferred test command: `pytest tests/ -v --tb=short`."

**Capture:** when Jarvis executes a project-scoped action and can infer a
project-specific fact from the output, it asks before writing: "Want me to
remember that this project uses X?" This is opt-in by default.

**Retrieval:** when `current_focus` switches to a project, the memory manager
loads the top-5 most important project-specific memories into working context
so the user does not have to re-explain the project's quirks.

**Conflict handling:** project-specific memories follow the same recency +
specificity rules as global memories, resolved within the project scope. A
project fact does not override a global fact with the same key.

---

## 44. Multi-Project Context Switching

**Switching trigger:** user says "switch to [project X]", "open [project Y]",
or the router resolves an intent to a project that is not `current_focus`.

**Switch sequence:**

```
1. Snapshot current workspace (section 33)
2. Update current_focus.project_id to new project
3. Load new project's most recent workspace snapshot
4. Load new project's project-specific memories into working context
5. Update active_context.current_pronoun_ref to null (references do not carry over)
6. Open project in editor (via open_project tool)
7. Report: "Switched to [project X]. Last session: [date]. Restoring [N] files."
```

**Multi-project awareness:** "active" means `last_opened_at` within the last
30 days. Older projects are archived in the registry but not deleted.

**Context bleed prevention:** after a switch, entity resolution prioritizes
the new project's namespace. "Open the main file" resolves to the new
project's `main.py`, not a file from the previous project.

---

## 45. Long-Term Project Context (After Days/Weeks Gap)

When `current_focus.updated_at` is more than 48 hours ago for the project
being resumed:

**Long gap resume sequence:**

```
1. Load project-specific memories (section 43)
2. Load most recent conversation_summary for this project
3. Load most recent workspace_snapshot
4. Local model synthesizes a catch-up brief: 3-5 bullet points summarizing
   last known state, open blockers, last action taken, next suggested step
5. Present catch-up brief to user before restoring workspace
6. Ask: "Want me to open where you left off?"
```

**Catch-up brief format:**

```
Last worked on: 14 days ago
Last action: Wrote the authentication middleware (auth.py)
Open blocker: Waiting on API keys from the provider
Next step: Integrate the middleware into the main route handler
Files from last session: auth.py, routes/api.py, config.py
```

---

## 46. Future Multi-Agent Expansion Hooks

The current architecture is single-agent. The following abstraction
boundaries already support multi-agent expansion and must not be violated
during Phase 1 and 2 development.

**Boundaries that support multi-agent without changes:**

- The Router's routing table uses a strategy pattern. A second agent can be
  registered as a new route target without touching the router's decision
  logic.
- The Tool Registry is agent-agnostic. Tools do not know which agent called
  them.
- The Safety Layer wraps all execution. Any agent calling a tool goes through
  the same validation and sandboxing.
- SQLite is the single system of record. Multiple agents can read from it;
  WAL mode handles safe concurrent writes.
- The Cloud Escalation Client is stateless per-call.

**What would need to change for multi-agent:**

- `active_context` would need an `agent_id` column.
- `tool_logs` would need an `agent_id` column for attribution.
- The planner would need to support assigning steps to different agents.
- A message-passing bus would need to handle inter-agent communication.

Do not build any of this in Phase 1 or 2, but do not hard-code agent-specific
logic into the router, safety layer, or tool registry.