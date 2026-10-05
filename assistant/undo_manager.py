import abc
import json
import logging
import os
import subprocess
import time
import uuid

try:
    from assistant.tools import Tool, ToolResult, ErrorCode
    from assistant.file_safety import QuarantineManager, LocalQuarantineManager
except ModuleNotFoundError:
    from tools import Tool, ToolResult, ErrorCode
    from file_safety import QuarantineManager, LocalQuarantineManager

logger = logging.getLogger("jarvis.undo_manager")


class AbstractUndoManager(abc.ABC):
    """
    Abstract interface for managing undo operations.
    Ensures safe rollbacks for downloads, git commits, and file mutations without data loss.
    """

    @abc.abstractmethod
    def record_action(self, action_type: str, description: str, data: dict) -> str:
        """Records an undoable action in the journal and returns an undo_id."""
        pass

    @abc.abstractmethod
    async def undo_last(self) -> ToolResult:
        """Undoes the most recent reversible action safely."""
        pass

    @abc.abstractmethod
    async def undo_by_id(self, undo_id: str) -> ToolResult:
        """Undoes a specific action by its undo_id."""
        pass

    @abc.abstractmethod
    def list_undoable_actions(self, limit: int = 10) -> list[dict]:
        """Lists recent undoable actions."""
        pass

    @abc.abstractmethod
    def clean_expired_quarantine(self, max_days: int = 7) -> int:
        """Purges quarantined items older than max_days retention period."""
        pass


class UndoManager(AbstractUndoManager):
    """
    Concrete implementation of Safe Undo.
    Guarantees:
    1. Downloads are NEVER permanently deleted; moved to Quarantine with 7-day retention.
    2. Git commits are NEVER reset blindly; safety refs are created and --soft reset preserves work.
    3. Multi-file operations can be rolled back using quarantined snapshots.
    """

    RETENTION_SECONDS = 7 * 86400  # 7 days

    def __init__(
        self,
        quarantine_mgr: QuarantineManager | None = None,
        journal_path: str | None = None
    ):
        self.quarantine = quarantine_mgr or LocalQuarantineManager()
        self.journal_path = journal_path or os.path.abspath("assistant/backups/undo_journal.json")
        os.makedirs(os.path.dirname(self.journal_path), exist_ok=True)
        self._ensure_journal()

    def _ensure_journal(self):
        if not os.path.exists(self.journal_path):
            with open(self.journal_path, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2)

    def _read_journal(self) -> list[dict]:
        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _write_journal(self, entries: list[dict]):
        with open(self.journal_path, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)

    def record_action(self, action_type: str, description: str, data: dict) -> str:
        undo_id = f"undo_{uuid.uuid4().hex[:8]}"
        entries = self._read_journal()
        entries.append({
            "undo_id": undo_id,
            "action_type": action_type,
            "description": description,
            "data": data,
            "timestamp": int(time.time()),
            "status": "active"
        })
        self._write_journal(entries)
        logger.info(f"Recorded undoable action {undo_id}: {description}")
        return undo_id

    def list_undoable_actions(self, limit: int = 10) -> list[dict]:
        entries = self._read_journal()
        active = [e for e in entries if e.get("status") == "active"]
        return active[-limit:]

    async def undo_last(self) -> ToolResult:
        entries = self._read_journal()
        active_entries = [e for e in entries if e.get("status") == "active"]
        if not active_entries:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.NOT_FOUND,
                message="No undoable actions found in journal.",
                status="failure"
            )
        last_action = active_entries[-1]
        return await self.undo_by_id(last_action["undo_id"])

    async def undo_by_id(self, undo_id: str) -> ToolResult:
        entries = self._read_journal()
        target = next((e for e in entries if e["undo_id"] == undo_id and e.get("status") == "active"), None)
        if not target:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.NOT_FOUND,
                message=f"No active action found with ID '{undo_id}'.",
                status="failure"
            )

        action_type = target["action_type"]
        data = target["data"]
        result = None

        if action_type == "download":
            result = self._undo_download(data)
        elif action_type == "git_commit":
            result = self._undo_git_commit(data)
        elif action_type == "file_delete":
            result = self._undo_file_delete(data)
        elif action_type == "file_write":
            result = self._undo_file_write(data)
        else:
            result = ToolResult(
                ok=False,
                error_code=ErrorCode.EXECUTION_FAILED,
                message=f"Unsupported undo action type: {action_type}",
                status="failure"
            )

        if result.ok:
            target["status"] = "undone"
            target["undone_at"] = int(time.time())
            self._write_journal(entries)

        return result

    def _undo_download(self, data: dict) -> ToolResult:
        """
        Safely undoes a download by moving it to Quarantine with 7-day retention.
        NEVER permanently deletes the file.
        """
        file_path = data.get("file_path")
        if not file_path or not os.path.exists(file_path):
            return ToolResult(
                ok=False,
                error_code=ErrorCode.NOT_FOUND,
                message=f"Downloaded file no longer exists: {file_path}",
                status="failure"
            )

        backup_id = self.quarantine.quarantine_file(file_path, action="undo_download")
        if not backup_id:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.EXECUTION_FAILED,
                message="Failed to move downloaded file to quarantine safety backup.",
                status="failure"
            )

        try:
            os.remove(file_path)
            clean_name = os.path.basename(file_path)
            return ToolResult(
                ok=True,
                data={"backup_id": backup_id, "original_path": file_path, "retention_days": 7},
                message=f"Safely removed download '{clean_name}'. Backup retained in quarantine for 7 days (Backup ID: {backup_id}).",
                status="success"
            )
        except Exception as e:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.EXECUTION_FAILED,
                message=f"Error removing file after quarantine: {e}",
                status="failure"
            )

    def _undo_git_commit(self, data: dict) -> ToolResult:
        """
        Safely undoes a git commit.
        1. Checks repository presence.
        2. Creates a safety backup branch/tag at current HEAD.
        3. Uses `git reset --soft HEAD~1` to keep all changes safely in index/staging.
        """
        repo_path = data.get("repo_path") or os.getcwd()
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            return ToolResult(
                ok=False,
                error_code=ErrorCode.NOT_FOUND,
                message=f"Path '{repo_path}' is not a valid Git repository.",
                status="failure"
            )

        try:
            # 1. Get current HEAD hash
            head_proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )
            head_hash = head_proc.stdout.strip()
            short_hash = head_hash[:7]

            # 2. Create safety tag/ref so commit is never garbage collected
            safety_tag = f"undo_backup_{short_hash}_{int(time.time())}"
            subprocess.run(
                ["git", "tag", safety_tag, head_hash],
                cwd=repo_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False
            )

            # 3. Safe soft reset: preserves worktree and index modifications
            subprocess.run(
                ["git", "reset", "--soft", "HEAD~1"],
                cwd=repo_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )

            return ToolResult(
                ok=True,
                data={
                    "undone_commit": head_hash,
                    "safety_backup_tag": safety_tag,
                    "repo_path": repo_path
                },
                message=f"Commit {short_hash} safely undone. Changes remain preserved in staging. Safety tag '{safety_tag}' created.",
                status="success"
            )
        except subprocess.CalledProcessError as e:
            err = e.stderr.strip() if hasattr(e, "stderr") else str(e)
            return ToolResult(
                ok=False,
                error_code=ErrorCode.EXECUTION_FAILED,
                message=f"Git undo failed: {err}",
                status="failure"
            )
        except Exception as e:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.EXECUTION_FAILED,
                message=f"Error executing git undo: {e}",
                status="failure"
            )

    def _undo_file_delete(self, data: dict) -> ToolResult:
        backup_id = data.get("backup_id")
        dest = data.get("file_path")
        if not backup_id:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.NOT_FOUND,
                message="No backup ID found to restore deleted file.",
                status="failure"
            )

        restored = self.quarantine.restore_file(backup_id, destination_path=dest)
        if restored:
            return ToolResult(
                ok=True,
                data={"backup_id": backup_id, "file_path": dest},
                message=f"Successfully restored deleted file '{os.path.basename(dest)}'.",
                status="success"
            )
        return ToolResult(
            ok=False,
            error_code=ErrorCode.EXECUTION_FAILED,
            message=f"Failed to restore deleted file from quarantine backup '{backup_id}'.",
            status="failure"
        )

    def _undo_file_write(self, data: dict) -> ToolResult:
        backup_id = data.get("backup_id")
        file_path = data.get("file_path")
        if backup_id:
            restored = self.quarantine.restore_file(backup_id, destination_path=file_path)
            if restored:
                return ToolResult(
                    ok=True,
                    data={"backup_id": backup_id, "file_path": file_path},
                    message=f"Successfully reverted '{os.path.basename(file_path)}' to pre-write state.",
                    status="success"
                )
        return ToolResult(
            ok=False,
            error_code=ErrorCode.EXECUTION_FAILED,
            message="No prior snapshot available to revert file write.",
            status="failure"
        )

    def clean_expired_quarantine(self, max_days: int = 7) -> int:
        """
        Cleans quarantine files whose age exceeds max_days.
        Default 7 days retention.
        """
        now = int(time.time())
        cutoff = now - (max_days * 86400)
        manifest_files = self.quarantine.list_quarantined_files()
        purged = 0

        surviving = []
        for entry in manifest_files:
            ts = entry.get("timestamp", now)
            archive_path = entry.get("archive_path")
            if ts < cutoff:
                try:
                    if archive_path and os.path.exists(archive_path):
                        os.remove(archive_path)
                    purged += 1
                except Exception as e:
                    logger.warning(f"Error removing expired archive '{archive_path}': {e}")
                    surviving.append(entry)
            else:
                surviving.append(entry)

        if hasattr(self.quarantine, "_write_manifest"):
            self.quarantine._write_manifest(surviving)

        logger.info(f"Purged {purged} expired quarantine items (retention: {max_days} days).")
        return purged


class UndoActionTool(Tool):
    """
    Safely undoes the most recent action or a specific action by ID.
    Supports safe download undo (quarantine with 7-day retention),
    safe git commit undo (--soft reset with safety refs), and file restore.
    """

    def __init__(self, undo_manager: AbstractUndoManager | None = None):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "undo_id": {
                        "type": "string",
                        "description": "Optional specific undo ID. If omitted, undoes the latest action."
                    }
                }
            },
            "side_effects": "Safely reverts prior actions without data loss",
            "timeout_ms": 10000,
            "memory_limit_mb": 50
        }
        super().__init__("undo_action", "reversible", declaration)
        self.undo_manager = undo_manager or UndoManager()

    async def execute(self, executor, **kwargs) -> dict:
        undo_id = kwargs.get("undo_id")
        if undo_id:
            res = await self.undo_manager.undo_by_id(undo_id)
        else:
            res = await self.undo_manager.undo_last()
        return res.to_dict()
