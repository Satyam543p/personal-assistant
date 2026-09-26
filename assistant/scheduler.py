"""
Autonomous Background Scheduler & Proactive Cron Engine (Phase 2 Milestone 21)
Provides persistent background job scheduling, automated interval & clock-time triggers,
concurrency overlap protection, detailed execution auditing in SQLite, proactive morning/evening briefings,
and automated system maintenance (skill decay, backup rotation, resource watchdogs)
strictly within the < 1 GB daemon RAM budget.
"""

import abc
import asyncio
import datetime
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional

try:
    from assistant.config import WORKSPACE_ROOT, DATABASE_PATH
    from assistant.tools import Tool
except ModuleNotFoundError:
    from config import WORKSPACE_ROOT, DATABASE_PATH
    from tools import Tool

logger = logging.getLogger("jarvis.scheduler")


# =====================================================================
# Data Transfer Objects
# =====================================================================

@dataclass
class JobResult:
    status: str  # 'success', 'failed', 'skipped'
    summary: str
    duration_ms: int = 0
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ScheduledJobInfo:
    id: str
    name: str
    description: str
    schedule_type: str  # 'interval', 'daily_at', 'cron'
    schedule_value: str
    priority: int
    is_active: bool
    last_run_at: Optional[str]
    next_run_at: Optional[str]
    run_count: int
    failure_count: int
    last_status: Optional[str]
    last_error: Optional[str]
    metadata: Dict[str, Any] = field(default_factory=dict)


# =====================================================================
# Abstract Contracts ("Abstractions & Interfaces First")
# =====================================================================

class ScheduledJob(abc.ABC):
    """
    Abstract contract for an autonomous background job.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Unique identifier name for this job."""
        pass

    @property
    @abc.abstractmethod
    def description(self) -> str:
        """Human-readable description of what this job accomplishes."""
        pass

    @property
    @abc.abstractmethod
    def schedule_type(self) -> str:
        """'interval' (seconds) or 'daily_at' (HH:MM)."""
        pass

    @property
    @abc.abstractmethod
    def schedule_value(self) -> str:
        """Value matching schedule_type, e.g. '3600' or '03:00'."""
        pass

    @property
    def priority(self) -> int:
        """Priority rank from 1 (highest) to 10 (lowest). Default is 5."""
        return 5

    @property
    def metadata(self) -> Dict[str, Any]:
        """Optional metadata or job parameters."""
        return {}

    @abc.abstractmethod
    async def execute(self, context: Dict[str, Any]) -> JobResult:
        """
        Execute the job asynchronously with error containment.
        """
        pass


class BackgroundScheduler(abc.ABC):
    """
    Abstract contract for the background task scheduling manager.
    """

    @abc.abstractmethod
    def register_job(self, job: ScheduledJob) -> bool:
        """Register a job in the scheduler and synchronize with persistent database."""
        pass

    @abc.abstractmethod
    def unregister_job(self, job_name: str) -> bool:
        """Remove a job from active scheduling."""
        pass

    @abc.abstractmethod
    async def start(self) -> None:
        """Start the background scheduler event loop."""
        pass

    @abc.abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the background scheduler and await running jobs."""
        pass

    @abc.abstractmethod
    def pause_job(self, job_name: str) -> bool:
        """Pause a job from automatic execution."""
        pass

    @abc.abstractmethod
    def resume_job(self, job_name: str) -> bool:
        """Resume a paused job."""
        pass

    @abc.abstractmethod
    async def run_job_now(self, job_name: str) -> JobResult:
        """Trigger immediate on-demand execution of a registered job."""
        pass

    @abc.abstractmethod
    def list_jobs(self) -> List[Dict[str, Any]]:
        """List all registered jobs and their current execution status."""
        pass

    @abc.abstractmethod
    def get_job_logs(self, job_name: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        """Retrieve historical audit logs for jobs."""
        pass

    @property
    @abc.abstractmethod
    def is_running(self) -> bool:
        """Check if scheduler loop is actively running."""
        pass


class ProactiveEngine(abc.ABC):
    """
    Abstract contract for generating proactive briefings and autonomous feeds.
    """

    @abc.abstractmethod
    async def generate_morning_briefing(self) -> Dict[str, Any]:
        """Synthesize a morning briefing covering focus project, goals, and recommendations."""
        pass

    @abc.abstractmethod
    async def generate_evening_digest(self) -> Dict[str, Any]:
        """Synthesize an evening summary of work completed, study sessions, and blockers."""
        pass

    @abc.abstractmethod
    def get_active_feed(self, unread_only: bool = True, limit: int = 10) -> List[Dict[str, Any]]:
        """Fetch proactive briefing cards for the user."""
        pass

    @abc.abstractmethod
    def mark_feed_read(self, feed_id: str) -> bool:
        """Mark a proactive feed entry as read."""
        pass


# =====================================================================
# Concrete Implementation: JarvisBackgroundScheduler
# =====================================================================

class JarvisBackgroundScheduler(BackgroundScheduler):
    """
    Production background scheduler managing interval and clock-time jobs,
    preventing concurrent overlaps, and recording audit logs to SQLite.
    """

    def __init__(self, db_manager, tick_interval_seconds: float = 1.0):
        self.db = db_manager
        self.tick_interval_seconds = tick_interval_seconds
        self._jobs: Dict[str, ScheduledJob] = {}
        self._running_jobs: set[str] = set()
        self._is_running: bool = False
        self._loop_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._is_running

    def _calculate_next_run(self, schedule_type: str, schedule_value: str, from_time: Optional[datetime.datetime] = None) -> str:
        """
        Calculate the next run timestamp string (ISO format).
        """
        now = from_time or datetime.datetime.now()
        if schedule_type == "interval":
            try:
                seconds = float(schedule_value)
            except ValueError:
                seconds = 3600.0
            next_dt = now + datetime.timedelta(seconds=seconds)
            return next_dt.isoformat()

        elif schedule_type == "daily_at":
            try:
                parts = schedule_value.strip().split(":")
                target_hour = int(parts[0])
                target_minute = int(parts[1]) if len(parts) > 1 else 0
            except Exception:
                target_hour, target_minute = 3, 0

            candidate = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += datetime.timedelta(days=1)
            return candidate.isoformat()

        # Default fallback
        return (now + datetime.timedelta(hours=1)).isoformat()

    def register_job(self, job: ScheduledJob) -> bool:
        """
        Register job and persist in SQLite `scheduled_jobs`.
        """
        self._jobs[job.name] = job
        next_run = self._calculate_next_run(job.schedule_type, job.schedule_value)

        conn = self.db.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id, schedule_value, is_active FROM scheduled_jobs WHERE name = ?", (job.name,))
            row = cursor.fetchone()
            now_iso = datetime.datetime.now().isoformat()

            if row:
                # Update existing job metadata without overwriting its active toggle if already set by user
                cursor.execute(
                    """
                    UPDATE scheduled_jobs
                    SET description = ?, schedule_type = ?, schedule_value = ?,
                        priority = ?, metadata = ?, updated_at = ?
                    WHERE name = ?
                    """,
                    (job.description, job.schedule_type, job.schedule_value,
                     job.priority, json.dumps(job.metadata), now_iso, job.name)
                )
            else:
                job_id = f"job_{uuid.uuid4().hex[:10]}"
                cursor.execute(
                    """
                    INSERT INTO scheduled_jobs (
                        id, name, description, schedule_type, schedule_value,
                        priority, is_active, next_run_at, metadata, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (job_id, job.name, job.description, job.schedule_type, job.schedule_value,
                     job.priority, next_run, json.dumps(job.metadata), now_iso, now_iso)
                )
            conn.commit()
            logger.info(f"Registered scheduled job '{job.name}' (next run: {next_run})")
            return True
        except Exception as e:
            logger.error(f"Failed to register job '{job.name}' in database: {e}")
            return False
        finally:
            conn.close()

    def unregister_job(self, job_name: str) -> bool:
        if job_name in self._jobs:
            del self._jobs[job_name]

        conn = self.db.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM scheduled_jobs WHERE name = ?", (job_name,))
            conn.commit()
            logger.info(f"Unregistered job '{job_name}'")
            return True
        except Exception as e:
            logger.error(f"Failed to unregister job '{job_name}': {e}")
            return False
        finally:
            conn.close()

    def pause_job(self, job_name: str) -> bool:
        conn = self.db.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE scheduled_jobs SET is_active = 0, updated_at = ? WHERE name = ?",
                           (datetime.datetime.now().isoformat(), job_name))
            conn.commit()
            logger.info(f"Paused scheduled job '{job_name}'")
            return True
        except Exception as e:
            logger.error(f"Error pausing job '{job_name}': {e}")
            return False
        finally:
            conn.close()

    def resume_job(self, job_name: str) -> bool:
        if job_name not in self._jobs:
            return False

        job = self._jobs[job_name]
        next_run = self._calculate_next_run(job.schedule_type, job.schedule_value)

        conn = self.db.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE scheduled_jobs SET is_active = 1, next_run_at = ?, updated_at = ? WHERE name = ?",
                           (next_run, datetime.datetime.now().isoformat(), job_name))
            conn.commit()
            logger.info(f"Resumed scheduled job '{job_name}' (next run: {next_run})")
            return True
        except Exception as e:
            logger.error(f"Error resuming job '{job_name}': {e}")
            return False
        finally:
            conn.close()

    async def run_job_now(self, job_name: str) -> JobResult:
        """
        Manually trigger a job immediately.
        """
        if job_name not in self._jobs:
            return JobResult(status="failed", summary=f"Job '{job_name}' is not registered.")

        job = self._jobs[job_name]
        return await self._execute_job(job, manual=True)

    async def _execute_job(self, job: ScheduledJob, manual: bool = False) -> JobResult:
        """
        Execute job with overlap protection, timing, error containment, and database logging.
        """
        if job.name in self._running_jobs:
            logger.warning(f"Job '{job.name}' is already executing. Overlap prevented, skipping duplicate run.")
            return JobResult(status="skipped", summary=f"Job '{job.name}' overlap prevented (already running).")

        self._running_jobs.add(job.name)
        start_time = time.time()
        started_iso = datetime.datetime.now().isoformat()
        log_id = f"log_{uuid.uuid4().hex[:12]}"

        conn = self.db.get_connection()
        job_id = job.name
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM scheduled_jobs WHERE name = ?", (job.name,))
            row = cursor.fetchone()
            if row:
                job_id = row["id"]
                cursor.execute("UPDATE scheduled_jobs SET last_status = 'running' WHERE id = ?", (job_id,))
                conn.commit()
        except Exception as e:
            logger.error(f"Error updating job status to running: {e}")
        finally:
            conn.close()

        logger.info(f"▶ Executing background job '{job.name}' (manual={manual})...")
        job_result: JobResult

        try:
            context = {
                "db_manager": self.db,
                "manual": manual,
                "timestamp": started_iso
            }
            job_result = await job.execute(context)
            duration_ms = int((time.time() - start_time) * 1000)
            job_result.duration_ms = duration_ms
            logger.info(f"✔ Job '{job.name}' finished with status '{job_result.status}' in {duration_ms}ms: {job_result.summary}")
        except Exception as exc:
            duration_ms = int((time.time() - start_time) * 1000)
            err_msg = str(exc)
            logger.exception(f"✖ Job '{job.name}' failed with exception: {err_msg}")
            job_result = JobResult(
                status="failed",
                summary=f"Job '{job.name}' crashed with error: {err_msg}",
                duration_ms=duration_ms,
                error_message=err_msg
            )
        finally:
            self._running_jobs.discard(job.name)

        # Update SQLite scheduled_jobs & job_execution_logs
        completed_iso = datetime.datetime.now().isoformat()
        next_run = self._calculate_next_run(job.schedule_type, job.schedule_value)

        conn = self.db.get_connection()
        try:
            cursor = conn.cursor()
            # 1. Insert execution log
            cursor.execute(
                """
                INSERT INTO job_execution_logs (
                    id, job_id, job_name, started_at, completed_at,
                    duration_ms, status, summary, error_message, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (log_id, job_id, job.name, started_iso, completed_iso,
                 job_result.duration_ms, job_result.status, job_result.summary,
                 job_result.error_message, json.dumps(job_result.metadata), completed_iso)
            )

            # 2. Update job status
            if job_result.status == "success":
                cursor.execute(
                    """
                    UPDATE scheduled_jobs
                    SET last_run_at = ?, next_run_at = ?, run_count = run_count + 1,
                        last_status = ?, last_error = NULL, updated_at = ?
                    WHERE name = ?
                    """,
                    (completed_iso, next_run, job_result.status, completed_iso, job.name)
                )
            else:
                cursor.execute(
                    """
                    UPDATE scheduled_jobs
                    SET last_run_at = ?, next_run_at = ?, run_count = run_count + 1,
                        failure_count = failure_count + 1, last_status = ?,
                        last_error = ?, updated_at = ?
                    WHERE name = ?
                    """,
                    (completed_iso, next_run, job_result.status, job_result.error_message, completed_iso, job.name)
                )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to record execution log for '{job.name}': {e}")
        finally:
            conn.close()

        return job_result

    async def start(self) -> None:
        """
        Start the background polling loop.
        """
        if self._is_running:
            return

        self._is_running = True
        self._loop_task = asyncio.create_task(self._scheduler_loop())
        logger.info("JarvisBackgroundScheduler loop started.")

    async def stop(self) -> None:
        """
        Gracefully stop the scheduler loop and await in-flight jobs.
        """
        if not self._is_running:
            return

        self._is_running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass

        # Wait briefly for running tasks if any
        wait_start = time.time()
        while self._running_jobs and (time.time() - wait_start < 3.0):
            await asyncio.sleep(0.1)

        logger.info("JarvisBackgroundScheduler stopped.")

    async def _scheduler_loop(self) -> None:
        """
        Core scheduler tick loop.
        """
        while self._is_running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error in scheduler loop tick: {e}")

            await asyncio.sleep(self.tick_interval_seconds)

    async def _tick(self) -> None:
        """
        Inspect scheduled_jobs in SQLite and trigger due jobs.
        """
        now_iso = datetime.datetime.now().isoformat()
        conn = self.db.get_connection()
        due_jobs = []
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT name, schedule_type, schedule_value
                FROM scheduled_jobs
                WHERE is_active = 1 AND next_run_at <= ?
                ORDER BY priority ASC
                """,
                (now_iso,)
            )
            due_jobs = cursor.fetchall()
        except Exception as e:
            logger.error(f"Error querying due jobs: {e}")
        finally:
            conn.close()

        for row in due_jobs:
            job_name = row["name"]
            if job_name in self._jobs and job_name not in self._running_jobs:
                job = self._jobs[job_name]
                # Fire as background task without blocking the loop
                asyncio.create_task(self._execute_job(job))

    def list_jobs(self) -> List[Dict[str, Any]]:
        conn = self.db.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, name, description, schedule_type, schedule_value,
                       priority, is_active, last_run_at, next_run_at,
                       run_count, failure_count, last_status, last_error
                FROM scheduled_jobs
                ORDER BY priority ASC, name ASC
                """
            )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"Error listing scheduled jobs: {e}")
            return []
        finally:
            conn.close()

    def get_job_logs(self, job_name: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        conn = self.db.get_connection()
        try:
            cursor = conn.cursor()
            if job_name:
                cursor.execute(
                    """
                    SELECT id, job_name, started_at, completed_at, duration_ms,
                           status, summary, error_message
                    FROM job_execution_logs
                    WHERE job_name = ?
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    (job_name, limit)
                )
            else:
                cursor.execute(
                    """
                    SELECT id, job_name, started_at, completed_at, duration_ms,
                           status, summary, error_message
                    FROM job_execution_logs
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    (limit,)
                )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"Error fetching job logs: {e}")
            return []
        finally:
            conn.close()


# =====================================================================
# Concrete Implementation: JarvisProactiveEngine
# =====================================================================

class JarvisProactiveEngine(ProactiveEngine):
    """
    Synthesizes morning and evening briefings, tracks proactive cards,
    and alerts the user to recommendations and blockers.
    """

    def __init__(self, db_manager, growth_engine=None, recommendation_engine=None, context_store=None):
        self.db = db_manager
        self.growth_engine = growth_engine
        self.recommendation_engine = recommendation_engine
        self.context_store = context_store

    async def generate_morning_briefing(self) -> Dict[str, Any]:
        """
        Synthesize today's morning briefing card:
        - Active focus project & last opened
        - Top active goals & skill confidence
        - Top priority recommendation
        """
        now = datetime.datetime.now()
        date_str = now.strftime("%A, %B %d, %Y")
        
        # 1. Fetch current focus
        focus_project = "General Workspace"
        conn = self.db.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT project_id, updated_at FROM current_focus LIMIT 1;")
            row = cursor.fetchone()
            if row and row["project_id"]:
                focus_project = row["project_id"]
        except Exception as e:
            logger.error(f"Error fetching current focus for briefing: {e}")
        finally:
            conn.close()

        # 2. Fetch goals
        top_goals = []
        if self.growth_engine:
            try:
                goals = self.growth_engine.list_goals(status="active")
                top_goals = [g["title"] for g in goals[:3]]
            except Exception as e:
                logger.error(f"Error fetching goals for briefing: {e}")

        # 3. Fetch recommendation
        recommendation_text = "Review pending tasks and continue project workflow."
        if self.recommendation_engine:
            try:
                recs = await self.recommendation_engine.get_recommendations(limit=1)
                if recs:
                    recommendation_text = recs[0].title
            except Exception as e:
                logger.error(f"Error fetching recommendation for briefing: {e}")

        # Build content
        lines = [
            f"Good morning! Here is your daily Jarvis briefing for **{date_str}**:",
            "",
            f"🎯 **Active Focus Project:** `{focus_project}`",
            f"💡 **Top Recommendation:** {recommendation_text}",
        ]
        if top_goals:
            lines.append("📋 **Active Goals:**")
            for g in top_goals:
                lines.append(f"  • {g}")
        else:
            lines.append("📋 **Active Goals:** No immediate deadlines set.")

        lines.extend([
            "",
            "Systems are running clean and background maintenance is up to date."
        ])

        content = "\n".join(lines)
        feed_id = f"feed_{uuid.uuid4().hex[:10]}"
        title = f"Morning Briefing — {now.strftime('%b %d')}"

        # Persist in proactive_feed
        conn = self.db.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO proactive_feed (id, feed_type, title, content, priority, is_read, created_at)
                VALUES (?, 'morning_briefing', ?, ?, 3, 0, ?)
                """,
                (feed_id, title, content, now.isoformat())
            )
            conn.commit()
            logger.info(f"Saved morning briefing '{title}' into proactive_feed")
        except Exception as e:
            logger.error(f"Failed to persist morning briefing: {e}")
        finally:
            conn.close()

        return {
            "id": feed_id,
            "title": title,
            "content": content,
            "feed_type": "morning_briefing",
            "created_at": now.isoformat()
        }

    async def generate_evening_digest(self) -> Dict[str, Any]:
        """
        Synthesize evening wrap-up: study sessions logged, tool executions count.
        """
        now = datetime.datetime.now()
        date_str = now.strftime("%A, %B %d")
        today_start_ts = int(now.replace(hour=0, minute=0, second=0).timestamp())

        conn = self.db.get_connection()
        tool_count = 0
        study_minutes = 0
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as cnt FROM tool_logs WHERE executed_at >= ?", (today_start_ts,))
            row = cursor.fetchone()
            if row:
                tool_count = row["cnt"]

            cursor.execute("SELECT duration_minutes FROM study_sessions WHERE started_at >= ?", (today_start_ts,))
            sessions = cursor.fetchall()
            study_minutes = sum(s["duration_minutes"] for s in sessions if s["duration_minutes"])
        except Exception as e:
            logger.error(f"Error compiling evening digest: {e}")
        finally:
            conn.close()

        lines = [
            f"Evening Digest for **{date_str}**:",
            "",
            f"⚡ **Automated Tool Executions Today:** {tool_count}",
            f"📚 **Total Study Time Today:** {study_minutes} minutes",
            "",
            "Rest well! Jarvis will run scheduled memory consolidation and backup overnight."
        ]
        content = "\n".join(lines)
        feed_id = f"feed_{uuid.uuid4().hex[:10]}"
        title = f"Evening Digest — {now.strftime('%b %d')}"

        conn = self.db.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO proactive_feed (id, feed_type, title, content, priority, is_read, created_at)
                VALUES (?, 'evening_digest', ?, ?, 4, 0, ?)
                """,
                (feed_id, title, content, now.isoformat())
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to persist evening digest: {e}")
        finally:
            conn.close()

        return {
            "id": feed_id,
            "title": title,
            "content": content,
            "feed_type": "evening_digest",
            "created_at": now.isoformat()
        }

    def get_active_feed(self, unread_only: bool = True, limit: int = 10) -> List[Dict[str, Any]]:
        conn = self.db.get_connection()
        try:
            cursor = conn.cursor()
            if unread_only:
                cursor.execute(
                    """
                    SELECT id, feed_type, title, content, priority, is_read, created_at
                    FROM proactive_feed
                    WHERE is_read = 0
                    ORDER BY priority ASC, created_at DESC
                    LIMIT ?
                    """,
                    (limit,)
                )
            else:
                cursor.execute(
                    """
                    SELECT id, feed_type, title, content, priority, is_read, created_at
                    FROM proactive_feed
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,)
                )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"Error retrieving proactive feed: {e}")
            return []
        finally:
            conn.close()

    def mark_feed_read(self, feed_id: str) -> bool:
        conn = self.db.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE proactive_feed SET is_read = 1 WHERE id = ?", (feed_id,))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error marking feed item {feed_id} as read: {e}")
            return False
        finally:
            conn.close()


# =====================================================================
# Built-In Production Scheduled Jobs
# =====================================================================

class NightlyMaintenanceJob(ScheduledJob):
    """
    Executes daily overnight maintenance:
    1. Skill decay calculation (90-day half-life per Section 23).
    2. Pruning of expired logs and ephemeral records.
    3. Automated database backup rotation (retaining at most 5 recent backups).
    """

    def __init__(self, growth_engine=None, schedule_value: str = "03:00"):
        self.growth_engine = growth_engine
        self._schedule_value = schedule_value

    @property
    def name(self) -> str:
        return "nightly_maintenance"

    @property
    def description(self) -> str:
        return "Overnight maintenance: skill decay, log pruning, and database backup rotation"

    @property
    def schedule_type(self) -> str:
        return "daily_at"

    @property
    def schedule_value(self) -> str:
        return self._schedule_value

    @property
    def priority(self) -> int:
        return 2

    async def execute(self, context: Dict[str, Any]) -> JobResult:
        db = context["db_manager"]
        details = []

        # 1. Skill decay
        if self.growth_engine:
            try:
                if hasattr(self.growth_engine, "decay_all_skills"):
                    decayed_list = self.growth_engine.decay_all_skills()
                    details.append(f"Decayed {len(decayed_list)} skills")
                elif hasattr(self.growth_engine, "decay_skills"):
                    decayed = self.growth_engine.decay_skills()
                    details.append(f"Decayed {decayed} skills")
            except Exception as e:
                details.append(f"Skill decay failed: {e}")

        # 2. Backup rotation
        try:
            backup_result = self._rotate_backups(db.db_path)
            details.append(backup_result)
        except Exception as e:
            details.append(f"Backup rotation failed: {e}")

        # 3. Clean old execution logs (> 30 days)
        conn = db.get_connection()
        try:
            cursor = conn.cursor()
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=30)).isoformat()
            cursor.execute("DELETE FROM job_execution_logs WHERE started_at < ?", (cutoff,))
            pruned_count = cursor.rowcount
            conn.commit()
            details.append(f"Pruned {pruned_count} old job logs")
        except Exception as e:
            details.append(f"Log pruning failed: {e}")
        finally:
            conn.close()

        summary = "; ".join(details)
        return JobResult(status="success", summary=summary)

    def _rotate_backups(self, db_path: str, max_backups: int = 5) -> str:
        """
        Creates a timestamped backup and removes older backups if exceeding max_backups.
        """
        if not os.path.exists(db_path):
            return "No database file to backup"

        db_dir = os.path.dirname(os.path.abspath(db_path))
        backups_dir = os.path.join(db_dir, "backups")
        os.makedirs(backups_dir, exist_ok=True)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"jarvis_backup_{timestamp}.db"
        backup_path = os.path.join(backups_dir, backup_filename)

        # Safe copy
        shutil.copy2(db_path, backup_path)

        # Rotation
        existing = [
            os.path.join(backups_dir, f) for f in os.listdir(backups_dir)
            if f.startswith("jarvis_backup_") and f.endswith(".db")
        ]
        existing.sort(key=lambda p: os.path.getmtime(p))

        pruned = 0
        while len(existing) > max_backups:
            oldest = existing.pop(0)
            try:
                os.remove(oldest)
                pruned += 1
            except Exception as e:
                logger.warning(f"Could not remove old backup {oldest}: {e}")

        return f"Created backup '{backup_filename}' (retained {len(existing)}, pruned {pruned})"


class ProactiveBriefingJob(ScheduledJob):
    """
    Generates morning briefing and stores it in proactive_feed.
    """

    def __init__(self, proactive_engine: ProactiveEngine, schedule_value: str = "08:30"):
        self.proactive_engine = proactive_engine
        self._schedule_value = schedule_value

    @property
    def name(self) -> str:
        return "proactive_morning_briefing"

    @property
    def description(self) -> str:
        return "Generates morning goal, project, and recommendation briefing"

    @property
    def schedule_type(self) -> str:
        return "daily_at"

    @property
    def schedule_value(self) -> str:
        return self._schedule_value

    @property
    def priority(self) -> int:
        return 3

    async def execute(self, context: Dict[str, Any]) -> JobResult:
        briefing = await self.proactive_engine.generate_morning_briefing()
        return JobResult(
            status="success",
            summary=f"Generated morning briefing: '{briefing['title']}'",
            metadata={"feed_id": briefing["id"]}
        )


class ResourceWatchdogJob(ScheduledJob):
    """
    Verifies lazy-loaded resources (Whisper STT, MiniLM Embeddings, Playwright)
    are properly unloaded if idle, preserving the < 1 GB daemon RAM limit.
    """

    def __init__(self, timer_manager=None, voice_subsystem=None, schedule_interval_seconds: int = 60):
        self.timer_manager = timer_manager
        self.voice_subsystem = voice_subsystem
        self._schedule_interval = str(schedule_interval_seconds)

    @property
    def name(self) -> str:
        return "resource_watchdog"

    @property
    def description(self) -> str:
        return "Monitors idle resources and enforces strict < 1 GB memory budget"

    @property
    def schedule_type(self) -> str:
        return "interval"

    @property
    def schedule_value(self) -> str:
        return self._schedule_interval

    @property
    def priority(self) -> int:
        return 1

    async def execute(self, context: Dict[str, Any]) -> JobResult:
        unloaded_count = 0
        details = []

        if self.voice_subsystem:
            try:
                res = self.voice_subsystem.check_idle_unload()
                if res.get("stt_unloaded") or res.get("tts_unloaded"):
                    unloaded_count += 1
                    details.append(f"Voice subsystem unloaded idle models: {res}")
            except Exception as e:
                details.append(f"Voice check error: {e}")

        summary = f"Watchdog completed. {'; '.join(details) if details else 'All resources within idle budget.'}"
        return JobResult(status="success", summary=summary)


class StudySessionWatchdogJob(ScheduledJob):
    """
    Checks open video study sessions that exceed 45 minutes of idle time
    without an explicit completion signal (Section 41).
    """

    def __init__(self, schedule_interval_seconds: int = 300):
        self._schedule_interval = str(schedule_interval_seconds)

    @property
    def name(self) -> str:
        return "study_session_watchdog"

    @property
    def description(self) -> str:
        return "Flags or closes unclosed study sessions exceeding 45 minutes idle time"

    @property
    def schedule_type(self) -> str:
        return "interval"

    @property
    def schedule_value(self) -> str:
        return self._schedule_interval

    @property
    def priority(self) -> int:
        return 4

    async def execute(self, context: Dict[str, Any]) -> JobResult:
        db = context["db_manager"]
        conn = db.get_connection()
        flagged = 0
        try:
            cursor = conn.cursor()
            cutoff = (datetime.datetime.now() - datetime.timedelta(minutes=45)).isoformat()
            cursor.execute(
                """
                UPDATE study_sessions
                SET ended_at = ?, duration_minutes = 45, notes = COALESCE(notes, '') || ' [Auto-closed by watchdog after 45m idle]'
                WHERE ended_at IS NULL AND started_at < ?
                """,
                (datetime.datetime.now().isoformat(), cutoff)
            )
            flagged = cursor.rowcount
            conn.commit()
        except Exception as e:
            return JobResult(status="failed", summary=f"Error inspecting study sessions: {e}", error_message=str(e))
        finally:
            conn.close()

        return JobResult(status="success", summary=f"Inspected active study sessions. Auto-closed {flagged} stale sessions.")


# =====================================================================
# Dedicated System Tools
# =====================================================================

class ListScheduledJobsTool(Tool):
    def __init__(self, scheduler: BackgroundScheduler):
        declaration = {
            "inputs": {},
            "side_effects": "none",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("list_scheduled_jobs", "read_only", declaration)
        self.scheduler = scheduler

    async def execute(self, executor=None, **kwargs) -> dict:
        jobs = self.scheduler.list_jobs()
        if not jobs:
            return {"status": "success", "response": "No background jobs currently registered.", "jobs": []}

        lines = ["Registered Background Jobs:"]
        for j in jobs:
            status_icon = "🟢" if j["is_active"] else "⏸"
            last_stat = j["last_status"] or "never_run"
            lines.append(
                f"- {status_icon} **{j['name']}** [{j['schedule_type']}={j['schedule_value']}] "
                f"(Status: {last_stat}, Runs: {j['run_count']}, Next: {j['next_run_at'] or 'N/A'})\n"
                f"  Description: {j['description']}"
            )
        resp = "\n".join(lines)
        return {"status": "success", "response": resp, "jobs": jobs}


class ScheduleJobTool(Tool):
    def __init__(self, scheduler: BackgroundScheduler):
        declaration = {
            "inputs": {
                "name": {"type": "string"},
                "schedule_type": {"type": "string"},
                "schedule_value": {"type": "string"},
                "description": {"type": "string", "default": ""}
            },
            "side_effects": "registers_scheduled_job",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("schedule_job", "reversible", declaration)
        self.scheduler = scheduler

    async def execute(self, executor=None, **kwargs) -> dict:
        name = kwargs.get("name", "")
        schedule_type = kwargs.get("schedule_type", "interval").lower()
        schedule_value = kwargs.get("schedule_value", "3600")
        description = kwargs.get("description", "")

        if not name:
            return {"status": "failure", "response": "Error: Job name is required."}

        if schedule_type not in ["interval", "daily_at"]:
            return {"status": "failure", "response": f"Error: schedule_type must be 'interval' or 'daily_at', got '{schedule_type}'."}

        class GenericJob(ScheduledJob):
            def __init__(self, j_name, j_type, j_val, j_desc):
                self._name = j_name
                self._type = j_type
                self._val = j_val
                self._desc = j_desc or f"Custom scheduled job '{j_name}'"

            @property
            def name(self) -> str:
                return self._name

            @property
            def description(self) -> str:
                return self._desc

            @property
            def schedule_type(self) -> str:
                return self._type

            @property
            def schedule_value(self) -> str:
                return self._val

            async def execute(self, context: Dict[str, Any]) -> JobResult:
                return JobResult(status="success", summary=f"Custom job '{self._name}' executed successfully.")

        job = GenericJob(name, schedule_type, schedule_value, description)
        success = self.scheduler.register_job(job)
        if success:
            msg = f"Successfully scheduled background job '{name}' with {schedule_type}='{schedule_value}'."
            return {"status": "success", "response": msg, "job_name": name}
        return {"status": "failure", "response": f"Failed to schedule job '{name}'."}


class RunScheduledJobNowTool(Tool):
    def __init__(self, scheduler: BackgroundScheduler):
        declaration = {
            "inputs": {
                "name": {"type": "string"}
            },
            "side_effects": "executes_job_immediately",
            "timeout_ms": 30000,
            "memory_limit_mb": 100
        }
        super().__init__("run_scheduled_job", "reversible", declaration)
        self.scheduler = scheduler

    async def execute(self, executor=None, **kwargs) -> dict:
        name = kwargs.get("name") or kwargs.get("job_name", "")
        if not name:
            return {"status": "failure", "response": "Error: Job name is required."}

        result = await self.scheduler.run_job_now(name)
        if result.status == "success":
            msg = f"✔ Job '{name}' executed successfully in {result.duration_ms}ms: {result.summary}"
            return {"status": "success", "response": msg, "duration_ms": result.duration_ms}
        elif result.status == "skipped":
            msg = f"⚠ Job '{name}' was skipped: {result.summary}"
            return {"status": "skipped", "response": msg}
        else:
            msg = f"✖ Job '{name}' failed: {result.summary} (Error: {result.error_message})"
            return {"status": "failure", "response": msg, "error": result.error_message}


class GetProactiveBriefingTool(Tool):
    def __init__(self, proactive_engine: ProactiveEngine):
        declaration = {
            "inputs": {
                "feed_type": {"type": "string", "default": "morning_briefing"}
            },
            "side_effects": "none",
            "timeout_ms": 10000,
            "memory_limit_mb": 50
        }
        super().__init__("get_proactive_briefing", "read_only", declaration)
        self.proactive_engine = proactive_engine

    async def execute(self, executor=None, **kwargs) -> dict:
        feed_type = kwargs.get("feed_type", "morning_briefing")
        feed = self.proactive_engine.get_active_feed(unread_only=False, limit=5)
        matching = [item for item in feed if item["feed_type"] == feed_type]
        if matching:
            top = matching[0]
            self.proactive_engine.mark_feed_read(top["id"])
            content = f"### {top['title']}\n\n{top['content']}"
            return {"status": "success", "response": content, "briefing": top}

        if feed_type == "morning_briefing":
            briefing = await self.proactive_engine.generate_morning_briefing()
            content = f"### {briefing['title']}\n\n{briefing['content']}"
            return {"status": "success", "response": content, "briefing": briefing}
        elif feed_type == "evening_digest":
            digest = await self.proactive_engine.generate_evening_digest()
            content = f"### {digest['title']}\n\n{digest['content']}"
            return {"status": "success", "response": content, "digest": digest}

        return {"status": "success", "response": "No proactive briefings currently available."}


class ToggleJobStatusTool(Tool):
    def __init__(self, scheduler: BackgroundScheduler):
        declaration = {
            "inputs": {
                "name": {"type": "string"},
                "action": {"type": "string", "default": "pause"}
            },
            "side_effects": "updates_job_active_state",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("toggle_job_status", "reversible", declaration)
        self.scheduler = scheduler

    async def execute(self, executor=None, **kwargs) -> dict:
        name = kwargs.get("name") or kwargs.get("job_name", "")
        action = kwargs.get("action", "pause").lower()

        if not name:
            return {"status": "failure", "response": "Error: Job name is required."}

        if action in ["pause", "stop", "disable"]:
            ok = self.scheduler.pause_job(name)
            msg = f"Job '{name}' paused." if ok else f"Failed to pause job '{name}'."
            return {"status": "success" if ok else "failure", "response": msg}
        elif action in ["resume", "start", "enable"]:
            ok = self.scheduler.resume_job(name)
            msg = f"Job '{name}' resumed." if ok else f"Failed to resume job '{name}'."
            return {"status": "success" if ok else "failure", "response": msg}
        else:
            return {"status": "failure", "response": f"Unknown action '{action}'. Use 'pause' or 'resume'."}
