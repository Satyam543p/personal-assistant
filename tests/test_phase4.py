import asyncio
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from assistant.file_safety import LocalQuarantineManager
from assistant.undo_manager import UndoManager, UndoActionTool
from assistant.interpreter import RuleBasedInterpreter
from assistant.tools import ErrorCode


class TestPhase4SafeUndo(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.quarantine_dir = os.path.join(self.temp_dir, "quarantine")
        self.journal_path = os.path.join(self.temp_dir, "undo_journal.json")
        self.quarantine = LocalQuarantineManager(quarantine_dir=self.quarantine_dir)
        self.undo_mgr = UndoManager(quarantine_mgr=self.quarantine, journal_path=self.journal_path)
        self.interpreter = RuleBasedInterpreter()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────
    # 1. Download Undo: Never Deletes Permanently
    # ─────────────────────────────────────────────────────────────

    def test_undo_download_quarantines_file(self):
        # Create a downloaded file
        downloads_dir = os.path.join(self.temp_dir, "downloads")
        os.makedirs(downloads_dir, exist_ok=True)
        downloaded_file = os.path.join(downloads_dir, "song.mp3")
        with open(downloaded_file, "wb") as f:
            f.write(b"AUDIO_DATA_FOR_TEST")

        # Record download action
        undo_id = self.undo_mgr.record_action(
            action_type="download",
            description="Downloaded media 'song'",
            data={"file_path": downloaded_file, "title": "song"}
        )

        # Trigger undo
        res = asyncio.run(self.undo_mgr.undo_by_id(undo_id))
        self.assertTrue(res.ok)
        self.assertIn("Backup retained in quarantine for 7 days", res.message)

        # Original file must no longer be in downloads dir
        self.assertFalse(os.path.exists(downloaded_file))

        # BUT file must exist safely in quarantine repository!
        backup_id = res.data["backup_id"]
        quarantined = self.quarantine.list_quarantined_files()
        entry = next((e for e in quarantined if e["backup_id"] == backup_id), None)
        self.assertIsNotNone(entry)
        self.assertTrue(os.path.exists(entry["archive_path"]))
        with open(entry["archive_path"], "rb") as f:
            self.assertEqual(f.read(), b"AUDIO_DATA_FOR_TEST")

        # Restore file from quarantine
        restored = self.quarantine.restore_file(backup_id, downloaded_file)
        self.assertTrue(restored)
        self.assertTrue(os.path.exists(downloaded_file))

    # ─────────────────────────────────────────────────────────────
    # 2. Git Commit Undo: Safe Soft Reset & Safety Backup Ref
    # ─────────────────────────────────────────────────────────────

    def test_undo_git_commit_safety(self):
        # Setup real temporary git repo
        repo_dir = os.path.join(self.temp_dir, "git_repo")
        os.makedirs(repo_dir, exist_ok=True)
        subprocess.run(["git", "init"], cwd=repo_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo_dir, check=True)
        subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo_dir, check=True)

        # 1st commit
        f1 = os.path.join(repo_dir, "f1.txt")
        with open(f1, "w") as f:
            f.write("hello")
        subprocess.run(["git", "add", "f1.txt"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "first commit"], cwd=repo_dir, check=True)

        # 2nd commit to be undone
        f2 = os.path.join(repo_dir, "f2.txt")
        with open(f2, "w") as f:
            f.write("important work")
        subprocess.run(["git", "add", "f2.txt"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "second commit"], cwd=repo_dir, check=True)

        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, stdout=subprocess.PIPE, text=True, check=True).stdout.strip()

        # Record and trigger undo
        undo_id = self.undo_mgr.record_action(
            action_type="git_commit",
            description="Git commit 'second commit'",
            data={"repo_path": repo_dir, "message": "second commit"}
        )

        res = asyncio.run(self.undo_mgr.undo_by_id(undo_id))
        self.assertTrue(res.ok)
        self.assertIn("safely undone", res.message)

        # 1. Safety backup tag must exist
        safety_tag = res.data["safety_backup_tag"]
        tag_commit = subprocess.run(["git", "rev-parse", safety_tag], cwd=repo_dir, stdout=subprocess.PIPE, text=True, check=True).stdout.strip()
        self.assertEqual(tag_commit, head_before)

        # 2. HEAD must have moved back 1 commit
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, stdout=subprocess.PIPE, text=True, check=True).stdout.strip()
        self.assertNotEqual(head_after, head_before)

        # 3. Work is NOT lost: f2.txt still exists with changes staged
        self.assertTrue(os.path.exists(f2))
        with open(f2, "r") as f:
            self.assertEqual(f.read(), "important work")

    # ─────────────────────────────────────────────────────────────
    # 3. Quarantine 7-Day Retention Expiry
    # ─────────────────────────────────────────────────────────────

    def test_quarantine_retention_expiry(self):
        # Create one recent file and one 10-day-old file
        f_recent = os.path.join(self.temp_dir, "recent.txt")
        f_old = os.path.join(self.temp_dir, "old.txt")
        with open(f_recent, "w") as f:
            f.write("recent")
        with open(f_old, "w") as f:
            f.write("old")

        id_recent = self.quarantine.quarantine_file(f_recent, action="delete")
        id_old = self.quarantine.quarantine_file(f_old, action="delete")

        # Manually alter old entry's timestamp in manifest to 10 days ago
        entries = self.quarantine._read_manifest()
        for e in entries:
            if e["backup_id"] == id_old:
                e["timestamp"] = int(time.time()) - (10 * 86400)
        self.quarantine._write_manifest(entries)

        # Purge older than 7 days
        purged = self.undo_mgr.clean_expired_quarantine(max_days=7)
        self.assertEqual(purged, 1)

        # Recent file still exists in manifest
        surviving = self.quarantine.list_quarantined_files()
        self.assertEqual(len(surviving), 1)
        self.assertEqual(surviving[0]["backup_id"], id_recent)

    # ─────────────────────────────────────────────────────────────
    # 4. Undo Tool Execution
    # ─────────────────────────────────────────────────────────────

    def test_undo_action_tool_empty_journal(self):
        tool = UndoActionTool(undo_manager=self.undo_mgr)
        res = asyncio.run(tool.execute(executor=MagicMock()))
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], ErrorCode.NOT_FOUND.value)

    # ─────────────────────────────────────────────────────────────
    # 5. Interpreter Recognition
    # ─────────────────────────────────────────────────────────────

    def test_interpreter_undo_queries(self):
        for q in ("undo", "undo karo", "undo last action", "pichla action wapas lo"):
            res = asyncio.run(self.interpreter.interpret(q))
            self.assertEqual(res["intent"], "undo_action")
            self.assertEqual(res["suggested_route"], "tool")

    def test_interpreter_cancel_download_queries(self):
        for q in ("cancel download", "download cancel karo", "download roko", "stop download"):
            res = asyncio.run(self.interpreter.interpret(q))
            self.assertEqual(res["intent"], "cancel_download")
            self.assertEqual(res["suggested_route"], "tool")


if __name__ == "__main__":
    unittest.main()
