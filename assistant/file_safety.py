"""
File Quarantine & Reversible Operations Subsystem for Jarvis.
Safeguards filesystem operations by archiving pre-overwrite snapshots
and routing file deletions through an isolated quarantine repository
to guarantee zero unrecoverable data loss.
"""

import abc
import json
import logging
import os
import shutil
import time
import uuid

logger = logging.getLogger("jarvis.file_safety")

try:
    import send2trash
except ImportError:
    send2trash = None

try:
    from assistant.tools import Tool, ToolResult, ErrorCode
except ModuleNotFoundError:
    from tools import Tool, ToolResult, ErrorCode


class QuarantineManager(abc.ABC):
    @abc.abstractmethod
    def quarantine_file(self, file_path: str, action: str = "overwrite") -> str | None:
        """Backs up a copy of the target file to quarantine before modification."""
        pass

    @abc.abstractmethod
    def restore_file(self, backup_id: str, destination_path: str | None = None) -> bool:
        """Restores a quarantined file to its original location."""
        pass

    @abc.abstractmethod
    def list_quarantined_files(self) -> list[dict]:
        """Lists all files currently in the quarantine repository."""
        pass


class LocalQuarantineManager(QuarantineManager):
    def __init__(self, quarantine_dir: str = None):
        self.quarantine_dir = quarantine_dir or os.path.abspath("assistant/backups/quarantine")
        self.manifest_file = os.path.join(self.quarantine_dir, "manifest.json")
        os.makedirs(self.quarantine_dir, exist_ok=True)
        self._ensure_manifest()

    def _ensure_manifest(self):
        if not os.path.exists(self.manifest_file):
            with open(self.manifest_file, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2)

    def _read_manifest(self) -> list[dict]:
        try:
            with open(self.manifest_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _write_manifest(self, entries: list[dict]):
        with open(self.manifest_file, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)

    def quarantine_file(self, file_path: str, action: str = "overwrite") -> str | None:
        if not os.path.exists(file_path) or os.path.isdir(file_path):
            return None

        backup_id = str(uuid.uuid4())[:8]
        filename = os.path.basename(file_path)
        timestamp = int(time.time())
        archive_name = f"{timestamp}_{backup_id}_{filename}"
        archive_path = os.path.join(self.quarantine_dir, archive_name)

        try:
            shutil.copy2(file_path, archive_path)
            entries = self._read_manifest()
            entries.append({
                "backup_id": backup_id,
                "original_path": os.path.abspath(file_path),
                "archive_path": archive_path,
                "action": action,
                "timestamp": timestamp,
                "size_bytes": os.path.getsize(file_path)
            })
            self._write_manifest(entries)
            logger.info(f"Quarantined '{file_path}' as backup '{backup_id}' ({action}).")
            return backup_id
        except Exception as e:
            logger.error(f"Failed to quarantine file '{file_path}': {e}")
            return None

    def restore_file(self, backup_id: str, destination_path: str | None = None) -> bool:
        entries = self._read_manifest()
        target_entry = next((e for e in entries if e["backup_id"] == backup_id), None)
        if not target_entry:
            logger.warning(f"No quarantined file found with backup_id: {backup_id}")
            return False

        archive_path = target_entry["archive_path"]
        dest = destination_path or target_entry["original_path"]

        if not os.path.exists(archive_path):
            logger.error(f"Archive file missing on disk: {archive_path}")
            return False

        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(archive_path, dest)
            logger.info(f"Successfully restored backup '{backup_id}' to '{dest}'.")
            return True
        except Exception as e:
            logger.error(f"Failed to restore quarantined file '{backup_id}': {e}")
            return False

    def list_quarantined_files(self) -> list[dict]:
        return self._read_manifest()


class DeleteFileTool(Tool):
    def __init__(self, quarantine_mgr: QuarantineManager = None):
        declaration = {
            "inputs": {
                "file_path": {"type": "string"}
            },
            "side_effects": "Safely moves target file to quarantine backup before removal",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("delete_file", "destructive", declaration)
        self.quarantine = quarantine_mgr or LocalQuarantineManager()

    async def execute(self, executor, **kwargs) -> dict:
        file_path = kwargs.get("file_path") or kwargs.get("path")
        if not file_path:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.EXECUTION_FAILED,
                message="Parameter 'file_path' is required for delete_file.",
                status="failed"
            ).to_dict()

        if not executor.is_path_allowed(file_path):
            return ToolResult(
                ok=False,
                error_code=ErrorCode.PERMISSION_DENIED,
                message=f"Access denied to file path: {file_path}",
                status="permission_denied"
            ).to_dict()

        if not os.path.exists(file_path):
            return ToolResult(
                ok=False,
                error_code=ErrorCode.NOT_FOUND,
                message=f"File not found: {file_path}",
                status="not_found"
            ).to_dict()

        # Quarantine first
        backup_id = self.quarantine.quarantine_file(file_path, action="delete")
        used_trash = False
        try:
            if send2trash:
                send2trash.send2trash(file_path)
                used_trash = True
            else:
                os.remove(file_path)
        except Exception as e:
            try:
                os.remove(file_path)
            except Exception as rm_err:
                logger.error(f"Failed to remove file after quarantine: {rm_err} (original error: {e})")
                return ToolResult(
                    ok=False,
                    error_code=ErrorCode.EXECUTION_FAILED,
                    message=f"Failed to delete '{file_path}': {rm_err}",
                    status="failed"
                ).to_dict()

        msg = f"Safely deleted '{file_path}'. Quarantined as backup ID '{backup_id}'."
        if used_trash:
            msg += " Sent to Recycle Bin."

        return ToolResult(
            ok=True,
            data={
                "backup_id": backup_id,
                "file_path": file_path,
                "recycle_bin": used_trash
            },
            message=msg,
            undo={
                "action": "restore_quarantined_file",
                "params": {"backup_id": backup_id, "destination_path": file_path}
            },
            status="success"
        ).to_dict()


class RestoreQuarantinedFileTool(Tool):
    def __init__(self, quarantine_mgr: QuarantineManager = None):
        declaration = {
            "inputs": {
                "backup_id": {"type": "string"},
                "destination_path": {"type": "string", "default": None}
            },
            "side_effects": "Restores a quarantined file back to disk",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("restore_quarantined_file", "reversible", declaration)
        self.quarantine = quarantine_mgr or LocalQuarantineManager()

    async def execute(self, executor, **kwargs) -> dict:
        backup_id = kwargs.get("backup_id")
        dest = kwargs.get("destination_path")

        if not backup_id:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.EXECUTION_FAILED,
                message="Parameter 'backup_id' is required for restore_quarantined_file.",
                status="failed"
            ).to_dict()

        success = self.quarantine.restore_file(backup_id, destination_path=dest)
        if not success:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.EXECUTION_FAILED,
                message=f"Failed to restore quarantined file with ID '{backup_id}'.",
                status="failed"
            ).to_dict()

        return ToolResult(
            ok=True,
            data={
                "backup_id": backup_id,
                "destination_path": dest
            },
            message=f"Successfully restored quarantined file '{backup_id}'.",
            status="success"
        ).to_dict()
