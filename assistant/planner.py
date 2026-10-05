import abc
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

try:
    from assistant.router import PlanExecutor
    from assistant.projects import ProjectMetadata
except ModuleNotFoundError:
    from router import PlanExecutor
    from projects import ProjectMetadata

logger = logging.getLogger("jarvis.planner")

# Maximum permitted steps per plan as per Phase specification
MAX_PLAN_STEPS = 20


# =====================================================================
# Data Structures
# =====================================================================

@dataclass
class PlanStep:
    step_id: str
    plan_id: str
    index: int
    intent: str
    inputs: dict
    depends_on: list[str] = field(default_factory=list)
    risk_level: str = "reversible"  # "read_only" | "reversible" | "destructive"
    status: str = "pending"          # "pending" | "running" | "done" | "failed" | "skipped"
    result: dict | None = None
    error: str | None = None
    rollback_action: str | None = None
    rollback_inputs: dict | None = None


@dataclass
class Plan:
    id: str
    goal: str
    status: str = "pending"          # "pending" | "running" | "done" | "failed"
    steps: list[PlanStep] = field(default_factory=list)
    created_at: int = 0
    updated_at: int = 0
    completed_at: int | None = None


# =====================================================================
# Plan Decomposer Abstraction & Rule-Based Implementation
# =====================================================================

class PlanDecomposer(abc.ABC):
    """Abstract interface for decomposing goals into structured PlanSteps."""

    @abc.abstractmethod
    def decompose(self, plan_id: str, goal: str, context: dict = None) -> list[PlanStep]:
        pass


class RuleBasedPlanDecomposer(PlanDecomposer):
    """
    Deterministic rule-based decomposer for standard workflows like
    project scaffolding, research pipelines, or explicit step sequences.
    """

    def decompose(self, plan_id: str, goal: str, context: dict = None) -> list[PlanStep]:
        context = context or {}

        # 1. If explicit steps are provided in context (e.g. from tests or caller)
        if "steps" in context and isinstance(context["steps"], list):
            steps = []
            for idx, s_data in enumerate(context["steps"]):
                s_id = s_data.get("step_id") or f"{plan_id}_step_{idx}"
                steps.append(PlanStep(
                    step_id=s_id,
                    plan_id=plan_id,
                    index=idx,
                    intent=s_data.get("intent", ""),
                    inputs=s_data.get("inputs", {}),
                    depends_on=s_data.get("depends_on", []),
                    risk_level=s_data.get("risk_level", "reversible"),
                    status="pending",
                    rollback_action=s_data.get("rollback_action"),
                    rollback_inputs=s_data.get("rollback_inputs")
                ))
            return steps

        # 2. Match Project Scaffolding Workflow: "scaffold project <name>" or "create project <name>"
        scaffold_match = re.search(r"\b(?:scaffold|create|setup|new)\s+project\s+([a-zA-Z0-9_\-\s]+)", goal, re.IGNORECASE)
        if scaffold_match or context.get("workflow") == "scaffold_project":
            if context.get("project_name"):
                p_name = context.get("project_name").strip()
            elif scaffold_match:
                p_name = scaffold_match.group(1).strip()
            else:
                p_name = "new_project"
            p_slug = p_name.lower().replace(" ", "_")
            
            # Target directory
            custom_path = context.get("folder_path")
            if custom_path:
                folder_path = os.path.abspath(custom_path)
            else:
                workspace_root = os.path.abspath("c:/Users/Satyam Pandey/Desktop/personal assistent")
                folder_path = os.path.join(workspace_root, "assistant", "scratch", p_slug)

            s0_id = f"{plan_id}_step_0"
            s1_id = f"{plan_id}_step_1"
            s2_id = f"{plan_id}_step_2"
            s3_id = f"{plan_id}_step_3"
            s4_id = f"{plan_id}_step_4"

            readme_path = os.path.join(folder_path, "README.md")
            entry_path = os.path.join(folder_path, "main.py")

            steps = [
                # Step 0: Check target directory
                PlanStep(
                    step_id=s0_id,
                    plan_id=plan_id,
                    index=0,
                    intent="search_files",
                    inputs={"folder_path": os.path.dirname(folder_path), "directory": os.path.dirname(folder_path), "pattern": "*"},
                    depends_on=[],
                    risk_level="read_only",
                    status="pending",
                    rollback_action=None
                ),
                # Step 1: Write README.md (Rollback: delete file)
                PlanStep(
                    step_id=s1_id,
                    plan_id=plan_id,
                    index=1,
                    intent="write_file",
                    inputs={
                        "file_path": readme_path,
                        "content": f"# {p_name}\n\nAutomated project scaffolded by Jarvis Assistant."
                    },
                    depends_on=[s0_id],
                    risk_level="reversible",
                    status="pending",
                    rollback_action="delete_file",
                    rollback_inputs={"file_path": readme_path}
                ),
                # Step 2: Write main.py (Rollback: delete file)
                PlanStep(
                    step_id=s2_id,
                    plan_id=plan_id,
                    index=2,
                    intent="write_file",
                    inputs={
                        "file_path": entry_path,
                        "content": f"\"\"\"Entry point for {p_name}\"\"\"\n\ndef main():\n    print('Running {p_name}')\n\nif __name__ == '__main__':\n    main()\n"
                    },
                    depends_on=[s1_id],
                    risk_level="reversible",
                    status="pending",
                    rollback_action="delete_file",
                    rollback_inputs={"file_path": entry_path}
                ),
                # Step 3: Register in ProjectRegistry (Rollback: delete_project)
                PlanStep(
                    step_id=s3_id,
                    plan_id=plan_id,
                    index=3,
                    intent="add_project",
                    inputs={
                        "id": p_slug,
                        "name": p_name,
                        "aliases": [p_slug, p_name.lower()],
                        "folder_path": folder_path,
                        "preferred_editor": context.get("preferred_editor", "vscode")
                    },
                    depends_on=[s2_id],
                    risk_level="reversible",
                    status="pending",
                    rollback_action="delete_project",
                    rollback_inputs={"project_id": p_slug}
                ),
                # Step 4: Open project in editor
                PlanStep(
                    step_id=s4_id,
                    plan_id=plan_id,
                    index=4,
                    intent="open_project",
                    inputs={
                        "project_name": p_name,
                        "folder_path": folder_path,
                        "preferred_editor": context.get("preferred_editor", "vscode")
                    },
                    depends_on=[s3_id],
                    risk_level="reversible",
                    status="pending",
                    rollback_action=None
                )
            ]
            return steps

        # Default fallback: 1-step direct goal
        return [
            PlanStep(
                step_id=f"{plan_id}_step_0",
                plan_id=plan_id,
                index=0,
                intent=context.get("intent", "conversation"),
                inputs=context.get("inputs", {}),
                depends_on=[],
                risk_level="read_only",
                status="pending"
            )
        ]


# =====================================================================
# Jarvis Planner Subsystem (Planner & PlanExecutor)
# =====================================================================

class Planner(abc.ABC):
    """Abstract interface for the Planner subsystem."""

    @abc.abstractmethod
    async def create_plan(self, goal: str, context: dict = None) -> Plan:
        pass

    @abc.abstractmethod
    async def execute(self, plan: Plan) -> dict:
        pass

    @abc.abstractmethod
    async def rollback_plan(self, plan: Plan, failed_step_index: int) -> dict:
        pass

    @abc.abstractmethod
    def get_plan(self, plan_id: str) -> Plan | None:
        pass


class JarvisPlanner(PlanExecutor, Planner):
    """
    Subsystem responsible for multi-step task decomposition, sequential
    execution with prerequisite checks, SQLite persistence, and reverse rollbacks.
    """

    def __init__(self, db_manager, tool_executor=None, project_registry=None, decomposer=None):
        self.db = db_manager
        self.tool_executor = tool_executor
        self.project_registry = project_registry
        self.decomposer = decomposer or RuleBasedPlanDecomposer()

    # -----------------------------------------------------------------
    # Persistence Helpers
    # -----------------------------------------------------------------

    def _persist_plan(self, plan: Plan):
        """Saves a new Plan and its PlanSteps to SQLite."""
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO plans (id, goal, status, created_at, updated_at, completed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?);",
                    (plan.id, plan.goal, plan.status, plan.created_at, plan.updated_at, plan.completed_at)
                )
                for step in plan.steps:
                    conn.execute(
                        "INSERT INTO plan_steps (id, plan_id, index_order, intent, inputs, depends_on, "
                        "risk_level, status, result, error, rollback_action, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                        (
                            step.step_id,
                            step.plan_id,
                            step.index,
                            step.intent,
                            json.dumps(step.inputs or {}),
                            json.dumps(step.depends_on or []),
                            step.risk_level,
                            step.status,
                            json.dumps(step.result) if step.result else None,
                            step.error,
                            step.rollback_action,
                            plan.created_at,
                            plan.updated_at
                        )
                    )
            logger.info(f"Persisted plan '{plan.id}' with {len(plan.steps)} steps to SQLite.")
        except Exception as e:
            logger.error(f"Error persisting plan '{plan.id}': {e}")
            raise

    def _update_step_status(self, step: PlanStep):
        """Updates an individual PlanStep's execution status in SQLite."""
        now = int(time.time())
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE plan_steps SET status = ?, result = ?, error = ?, updated_at = ? WHERE id = ?;",
                    (
                        step.status,
                        json.dumps(step.result) if step.result else None,
                        step.error,
                        now,
                        step.step_id
                    )
                )
        except Exception as e:
            logger.error(f"Error updating plan_step '{step.step_id}': {e}")

    def _update_plan_status(self, plan: Plan):
        """Updates plan status and completion timestamp in SQLite."""
        now = int(time.time())
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE plans SET status = ?, updated_at = ?, completed_at = ? WHERE id = ?;",
                    (plan.status, now, plan.completed_at, plan.id)
                )
        except Exception as e:
            logger.error(f"Error updating plan '{plan.id}': {e}")

    def get_plan(self, plan_id: str) -> Plan | None:
        """Retrieves a full Plan and its steps from SQLite."""
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM plans WHERE id = ?;", (plan_id,))
                p_row = cursor.fetchone()
                if not p_row:
                    return None

                cursor.execute("SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY index_order ASC;", (plan_id,))
                s_rows = cursor.fetchall()

                steps = []
                for r in s_rows:
                    steps.append(PlanStep(
                        step_id=r["id"],
                        plan_id=r["plan_id"],
                        index=r["index_order"],
                        intent=r["intent"],
                        inputs=json.loads(r["inputs"]) if r["inputs"] else {},
                        depends_on=json.loads(r["depends_on"]) if r["depends_on"] else [],
                        risk_level=r["risk_level"],
                        status=r["status"],
                        result=json.loads(r["result"]) if r["result"] else None,
                        error=r["error"],
                        rollback_action=r["rollback_action"]
                    ))

                return Plan(
                    id=p_row["id"],
                    goal=p_row["goal"],
                    status=p_row["status"],
                    steps=steps,
                    created_at=p_row["created_at"],
                    updated_at=p_row["updated_at"],
                    completed_at=p_row["completed_at"]
                )
        except Exception as e:
            logger.error(f"Error loading plan '{plan_id}': {e}")
            return None

    # -----------------------------------------------------------------
    # Plan Lifecycle: Create, Execute, Rollback
    # -----------------------------------------------------------------

    async def create_plan(self, goal: str, context: dict = None) -> Plan:
        now = int(time.time())
        plan_id = f"plan_{now}_{int(time.time() * 1000) % 1000}"

        steps = self.decomposer.decompose(plan_id, goal, context)
        if len(steps) > MAX_PLAN_STEPS:
            raise ValueError(f"Plan exceeds maximum limit of {MAX_PLAN_STEPS} steps ({len(steps)} steps generated).")

        plan = Plan(
            id=plan_id,
            goal=goal,
            status="pending",
            steps=steps,
            created_at=now,
            updated_at=now
        )
        self._persist_plan(plan)
        return plan

    async def _execute_single_step(self, step: PlanStep) -> dict:
        """Executes a single step action through tool executor or project registry."""
        intent = step.intent
        inputs = step.inputs or {}

        # 1. Synthetic test failure
        if intent == "synthetic_failure":
            err_msg = inputs.get("error_message", "Synthetic failure triggered for rollback testing.")
            raise RuntimeError(err_msg)

        # 2. Project Registry actions
        if intent == "add_project":
            if not self.project_registry:
                raise RuntimeError("ProjectRegistry is not configured in Planner.")
            existing = self.project_registry.get_project(inputs["id"])
            if existing:
                self.project_registry.update_project(inputs["id"], {
                    "name": inputs["name"],
                    "folder_path": inputs["folder_path"],
                    "preferred_editor": inputs.get("preferred_editor", "vscode")
                })
                return {"status": "success", "message": f"Project '{inputs['name']}' updated in registry."}

            meta = ProjectMetadata(
                id=inputs["id"],
                name=inputs["name"],
                aliases=inputs.get("aliases", [inputs["id"], inputs["name"].lower()]),
                folder_path=inputs["folder_path"],
                preferred_editor=inputs.get("preferred_editor", "vscode")
            )
            ok = self.project_registry.add_project(meta)
            if not ok:
                raise RuntimeError(f"Failed to register project '{meta.name}' in ProjectRegistry.")
            return {"status": "success", "message": f"Project '{meta.name}' registered."}

        # 3. ToolExecutor actions (write_file, read_file, search_files, open_app, open_project)
        if self.tool_executor:
            ref = inputs.get("file_path") or inputs.get("folder_path") or inputs.get("directory") or inputs.get("app_name") or ""
            return await self.tool_executor.execute_tool(intent, inputs, ref)

        return {"status": "success", "message": f"Step '{intent}' executed successfully."}

    def _topological_levels(self, steps: list[PlanStep]) -> list[list[PlanStep]]:
        """
        Organizes plan steps into parallel execution waves using topological sorting.
        Each wave contains steps whose dependencies were satisfied in earlier waves.
        Detects circular dependencies and raises ValueError if cycles exist.
        """
        step_map = {s.step_id: s for s in steps}
        in_degree = {s.step_id: 0 for s in steps}
        graph: dict[str, list[str]] = {s.step_id: [] for s in steps}

        for s in steps:
            for dep in s.depends_on:
                if dep in step_map:
                    graph[dep].append(s.step_id)
                    in_degree[s.step_id] += 1

        queue = [s.step_id for s in steps if in_degree[s.step_id] == 0]
        levels = []
        visited_count = 0

        while queue:
            current_level_ids = queue[:]
            queue = []
            level_steps = [step_map[sid] for sid in current_level_ids]
            levels.append(level_steps)
            visited_count += len(current_level_ids)

            for sid in current_level_ids:
                for neighbor in graph[sid]:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)

        if visited_count < len(steps):
            raise ValueError("Cyclic dependency detected in plan DAG.")

        return levels

    def _resolve_piped_inputs(self, val: Any, completed_steps: dict[str, PlanStep]) -> Any:
        """
        Resolves inter-step piping syntax like '$step_0.file_path' or '$step_0.result.data.path'.
        Substitutes referenced output values before step execution.
        """
        if isinstance(val, dict):
            return {k: self._resolve_piped_inputs(v, completed_steps) for k, v in val.items()}
        elif isinstance(val, list):
            return [self._resolve_piped_inputs(x, completed_steps) for x in val]
        elif isinstance(val, str) and "$" in val:
            m = re.match(r"^\$([a-zA-Z0-9_\-]+)(?:\.([a-zA-Z0-9_\.\-]+))?$", val.strip())
            if m:
                step_id = m.group(1)
                prop_path = m.group(2)
                target_step = completed_steps.get(step_id)
                if not target_step:
                    return val

                if not prop_path:
                    return target_step.result

                curr: Any = target_step.result or {}
                for part in prop_path.split("."):
                    if isinstance(curr, dict) and part in curr:
                        curr = curr[part]
                    elif hasattr(curr, part):
                        curr = getattr(curr, part)
                    elif isinstance(target_step.inputs, dict) and part in target_step.inputs:
                        curr = target_step.inputs[part]
                    else:
                        return val
                return curr

            def _sub(match):
                sid = match.group(1)
                path = match.group(2)
                st = completed_steps.get(sid)
                if not st:
                    return match.group(0)
                curr: Any = st.result or {}
                if path:
                    for part in path.split("."):
                        if isinstance(curr, dict) and part in curr:
                            curr = curr[part]
                        elif isinstance(st.inputs, dict) and part in st.inputs:
                            curr = st.inputs[part]
                        else:
                            return match.group(0)
                return str(curr)

            return re.sub(r"\$([a-zA-Z0-9_\-]+)(?:\.([a-zA-Z0-9_\.\-]+))?", _sub, val)

        return val

    async def execute(self, plan: Plan) -> dict:
        """
        Executes plan steps using DAG topological levels. Independent steps in the
        same level execute in parallel. Inter-step piping is resolved before each step runs.
        """
        plan.status = "running"
        self._update_plan_status(plan)

        logger.info(f"Starting DAG execution of plan '{plan.id}' with {len(plan.steps)} steps.")

        try:
            levels = self._topological_levels(plan.steps)
        except ValueError as ve:
            plan.status = "failed"
            self._update_plan_status(plan)
            return {
                "status": "failure",
                "plan_id": plan.id,
                "error": str(ve),
                "response": f"Plan DAG validation failed: {ve}",
                "route": "planner"
            }

        completed_steps: dict[str, PlanStep] = {}
        failed_steps: list[PlanStep] = []
        step_id_map = {s.step_id: s for s in plan.steps}

        # Build descendant tree to mark skipped steps if a dependency fails
        descendants: dict[str, set[str]] = {s.step_id: set() for s in plan.steps}
        for s in plan.steps:
            for dep in s.depends_on:
                if dep in descendants:
                    descendants[dep].add(s.step_id)

        def _get_all_descendants(root_id: str) -> set[str]:
            all_desc = set()
            to_visit = list(descendants.get(root_id, []))
            while to_visit:
                curr = to_visit.pop(0)
                if curr not in all_desc:
                    all_desc.add(curr)
                    to_visit.extend(descendants.get(curr, []))
            return all_desc

        for level_idx, level_steps in enumerate(levels):
            ready_to_run = []
            for step in level_steps:
                # Check prerequisites
                dep_failed = False
                for dep_id in step.depends_on:
                    dep_s = step_id_map.get(dep_id)
                    if not dep_s or dep_s.status != "done":
                        dep_failed = True
                        break

                if dep_failed:
                    step.status = "skipped"
                    step.error = "Skipped due to upstream dependency failure."
                    self._update_step_status(step)
                else:
                    ready_to_run.append(step)

            if not ready_to_run:
                continue

            async def _run_single(st: PlanStep):
                # Resolve piping
                resolved_inputs = self._resolve_piped_inputs(st.inputs, completed_steps)
                st.inputs = resolved_inputs
                st.status = "running"
                self._update_step_status(st)

                try:
                    logger.info(f"Executing DAG step '{st.step_id}' ('{st.intent}')")
                    res = await self._execute_single_step(st)
                    st.status = "done"
                    st.result = res
                    self._update_step_status(st)
                    return st, None
                except Exception as ex:
                    err_text = str(ex)
                    logger.error(f"Step '{st.step_id}' failed: {err_text}")
                    st.status = "failed"
                    st.error = err_text
                    self._update_step_status(st)
                    return st, err_text

            # Execute ready steps in parallel
            tasks = [_run_single(s) for s in ready_to_run]
            step_outcomes = await asyncio.gather(*tasks, return_exceptions=True)

            for outcome in step_outcomes:
                if isinstance(outcome, tuple):
                    finished_step, err = outcome
                    if err:
                        failed_steps.append(finished_step)
                        # Mark descendants as skipped
                        for d_id in _get_all_descendants(finished_step.step_id):
                            d_step = step_id_map.get(d_id)
                            if d_step and d_step.status == "pending":
                                d_step.status = "skipped"
                                d_step.error = f"Skipped due to failure of upstream step '{finished_step.step_id}'."
                                self._update_step_status(d_step)
                    else:
                        completed_steps[finished_step.step_id] = finished_step

        # Post execution summary
        total_steps = len(plan.steps)
        done_count = len(completed_steps)

        if failed_steps:
            plan.status = "failed" if done_count == 0 else "partial_failure"
            self._update_plan_status(plan)

            # Trigger reverse rollback on failed step(s)
            first_failed = failed_steps[0]
            first_failed_idx = first_failed.index
            rollback_info = await self.rollback_plan(plan, first_failed_idx)

            status_name = "partial_failure" if done_count > 0 else "failure"
            err_msg = first_failed.error or "Step execution failed."
            return {
                "status": status_name,
                "plan_id": plan.id,
                "failed_step_index": first_failed_idx,
                "error": err_msg,
                "response": (
                    f"Completed {done_count} of {total_steps} steps before failing "
                    f"at step '{first_failed.intent}': {err_msg}"
                ),
                "spoken_summary": (
                    f"Completed {done_count} steps before encountering a problem at {first_failed.intent}."
                    if done_count > 0 else f"Workflow failed at {first_failed.intent}."
                ),
                "step_badges": [{"step": s.step_id, "intent": s.intent, "status": s.status} for s in plan.steps],
                "rollback": rollback_info,
                "route": "planner"
            }

        # All steps succeeded
        plan.status = "done"
        plan.completed_at = int(time.time())
        self._update_plan_status(plan)
        logger.info(f"Plan '{plan.id}' completed successfully with all {done_count} steps.")

        return {
            "status": "success",
            "plan_id": plan.id,
            "response": f"Successfully completed workflow: '{plan.goal}' ({done_count}/{total_steps} steps).",
            "spoken_summary": f"Completed all {done_count} steps for {plan.goal}.",
            "steps_completed": done_count,
            "step_badges": [{"step": s.step_id, "intent": s.intent, "status": s.status} for s in plan.steps],
            "route": "planner"
        }

    async def rollback_plan(self, plan: Plan, failed_step_index: int) -> dict:
        """
        Executes reverse rollbacks for steps 0 to failed_step_index - 1
        where rollback_action is defined.
        """
        logger.warning(f"Initiating reverse rollback for plan '{plan.id}' from step {failed_step_index}...")
        rollbacks_executed = []

        # Iterate in reverse order from step immediately before the failed step
        for idx in range(failed_step_index - 1, -1, -1):
            step = plan.steps[idx]
            action = step.rollback_action
            if not action:
                continue

            r_inputs = step.rollback_inputs or step.inputs or {}
            logger.info(f"Rolling back step {idx} ('{step.intent}') via action '{action}'...")

            try:
                if action == "delete_file":
                    file_path = r_inputs.get("file_path")
                    if file_path and os.path.exists(file_path):
                        try:
                            import send2trash
                            send2trash.send2trash(file_path)
                            logger.info(f"Rollback moved file to Recycle Bin: '{file_path}'")
                        except Exception:
                            os.remove(file_path)
                            logger.info(f"Rollback deleted file: '{file_path}'")
                        rollbacks_executed.append({"step_index": idx, "action": action, "target": file_path, "status": "success"})
                elif action == "delete_project":
                    p_id = r_inputs.get("project_id")
                    if p_id and self.project_registry:
                        self.project_registry.delete_project(p_id)
                        logger.info(f"Rollback deleted project registry entry: '{p_id}'")
                        rollbacks_executed.append({"step_index": idx, "action": action, "target": p_id, "status": "success"})
                else:
                    logger.warning(f"Unrecognized rollback action '{action}' for step {idx}")
            except Exception as e:
                logger.error(f"Error rolling back step {idx}: {e}")
                rollbacks_executed.append({"step_index": idx, "action": action, "status": f"failed: {e}"})

        return {
            "status": "rolled_back",
            "actions": rollbacks_executed
        }

    # -----------------------------------------------------------------
    # PlanExecutor Interface Implementation for Router
    # -----------------------------------------------------------------

    async def execute_plan(self, intent: str, entities: dict) -> dict:
        """
        Entry point called by JarvisRouter when intent is 'multi_step_task'
        or a multi-step workflow.
        """
        goal = entities.get("goal") or entities.get("task") or "multi_step_task"
        plan = await self.create_plan(goal, context=entities)
        return await self.execute(plan)
