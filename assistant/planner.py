import abc
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

try:
    from assistant.router import PlanExecutor
    from assistant.projects import ProjectRegistry, ProjectMetadata
except ModuleNotFoundError:
    from router import PlanExecutor
    from projects import ProjectRegistry, ProjectMetadata

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

    async def execute(self, plan: Plan) -> dict:
        """Executes plan steps sequentially, halting and rolling back on failure."""
        now = int(time.time())
        plan.status = "running"
        self._update_plan_status(plan)

        logger.info(f"Starting execution of plan '{plan.id}' with {len(plan.steps)} steps.")

        completed_steps = []
        step_id_map = {s.step_id: s for s in plan.steps}

        for idx, step in enumerate(plan.steps):
            # Verify dependencies
            for dep_id in step.depends_on:
                dep_step = step_id_map.get(dep_id)
                if not dep_step or dep_step.status != "done":
                    err_msg = f"Prerequisite step '{dep_id}' was not completed (status: {getattr(dep_step, 'status', 'missing')})."
                    logger.error(err_msg)
                    step.status = "failed"
                    step.error = err_msg
                    self._update_step_status(step)

                    # Mark subsequent steps as skipped
                    for skip_idx in range(idx + 1, len(plan.steps)):
                        skip_step = plan.steps[skip_idx]
                        skip_step.status = "skipped"
                        self._update_step_status(skip_step)

                    plan.status = "failed"
                    self._update_plan_status(plan)
                    rollback_info = await self.rollback_plan(plan, idx)
                    return {
                        "status": "failure",
                        "plan_id": plan.id,
                        "failed_step_index": idx,
                        "error": err_msg,
                        "rollback": rollback_info,
                        "route": "planner"
                    }

            # Execute step
            step.status = "running"
            self._update_step_status(step)

            try:
                logger.info(f"Executing plan step {idx}: '{step.intent}' (inputs: {step.inputs})")
                res = await self._execute_single_step(step)
                step.status = "done"
                step.result = res
                self._update_step_status(step)
                completed_steps.append(step)
            except Exception as e:
                err_str = str(e)
                logger.error(f"Step {idx} ('{step.intent}') failed: {err_str}")
                step.status = "failed"
                step.error = err_str
                self._update_step_status(step)

                # Mark subsequent steps as skipped
                for skip_idx in range(idx + 1, len(plan.steps)):
                    skip_step = plan.steps[skip_idx]
                    skip_step.status = "skipped"
                    self._update_step_status(skip_step)

                plan.status = "failed"
                self._update_plan_status(plan)

                # Trigger reverse rollback on failed step
                rollback_info = await self.rollback_plan(plan, idx)
                return {
                    "status": "failure",
                    "plan_id": plan.id,
                    "failed_step_index": idx,
                    "error": err_str,
                    "rollback": rollback_info,
                    "route": "planner"
                }

        # All steps completed successfully
        plan.status = "done"
        plan.completed_at = int(time.time())
        self._update_plan_status(plan)
        logger.info(f"Plan '{plan.id}' completed successfully.")

        return {
            "status": "success",
            "plan_id": plan.id,
            "response": f"Successfully completed workflow: '{plan.goal}' ({len(completed_steps)}/{len(plan.steps)} steps).",
            "steps_completed": len(completed_steps),
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
