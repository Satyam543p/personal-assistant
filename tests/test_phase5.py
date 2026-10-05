import asyncio
import datetime
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from assistant.database.manager import DatabaseManager
from assistant.scheduler import (
    JarvisBackgroundScheduler,
    CreateReminderTool,
    ListRemindersTool,
    CancelReminderTool
)
from assistant.tools import ErrorCode


class TestPhase5UnifiedSchedulerReminders(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(self.temp_dir, "test_scheduler.db")
        self.db = DatabaseManager(db_path=db_path)
        self.mock_notifications = MagicMock()
        self.mock_callback = MagicMock()
        self.scheduler = JarvisBackgroundScheduler(
            db_manager=self.db,
            tick_interval_seconds=0.1,
            notifications=self.mock_notifications,
            on_reminder_triggered=self.mock_callback
        )

    def tearDown(self):
        if self.scheduler.is_running:
            asyncio.run(self.scheduler.stop())
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ─────────────────────────────────────────────────────────────
    # 1. Single Engine Principle & Persistence in SQLite
    # ─────────────────────────────────────────────────────────────

    def test_create_and_list_reminders_persisted(self):
        future_time = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)).isoformat()
        res = self.scheduler.create_reminder(
            reminder_text="Drink water",
            trigger_time_utc=future_time,
            spoken_time="in 2 hours"
        )
        self.assertTrue(res["is_active"])
        self.assertEqual(res["reminder_text"], "Drink water")

        # Verify persisted in SQLite
        active = self.scheduler.list_reminders(active_only=True)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], res["id"])
        self.assertEqual(active[0]["reminder_text"], "Drink water")

    def test_cancel_reminder(self):
        future_time = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat()
        res = self.scheduler.create_reminder(
            reminder_text="Call mom",
            trigger_time_utc=future_time,
            spoken_time="in 1 hour"
        )
        self.assertEqual(len(self.scheduler.list_reminders()), 1)

        # Cancel by ID
        cancelled = self.scheduler.cancel_reminder(reminder_id=res["id"])
        self.assertTrue(cancelled)
        self.assertEqual(len(self.scheduler.list_reminders(active_only=True)), 0)

        # Create another and cancel by query text
        self.scheduler.create_reminder(
            reminder_text="Buy groceries tomorrow",
            trigger_time_utc=future_time,
            spoken_time="tomorrow"
        )
        cancelled2 = self.scheduler.cancel_reminder(query="groceries")
        self.assertTrue(cancelled2)
        self.assertEqual(len(self.scheduler.list_reminders(active_only=True)), 0)

    # ─────────────────────────────────────────────────────────────
    # 2. Execution on Due Time in Scheduler Tick
    # ─────────────────────────────────────────────────────────────

    def test_reminder_triggers_in_scheduler_tick(self):
        # Create a reminder whose due time is in the past
        past_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=5)).isoformat()
        res = self.scheduler.create_reminder(
            reminder_text="Stand up and stretch",
            trigger_time_utc=past_time,
            spoken_time="just now"
        )

        # Run one tick of the single scheduler loop
        asyncio.run(self.scheduler._tick())

        # Give async task a brief moment to run callback
        asyncio.run(asyncio.sleep(0.05))

        # Check reminder is now marked triggered / inactive
        all_reminders = self.scheduler.list_reminders(active_only=False)
        target = next((r for r in all_reminders if r["id"] == res["id"]), None)
        self.assertIsNotNone(target)
        self.assertEqual(target["is_active"], 0)
        self.assertIsNotNone(target["triggered_at"])

        # Check notification and callback were dispatched
        self.mock_notifications.send_notification.assert_called_with(
            title="Jarvis Reminder",
            message="Stand up and stretch"
        )
        self.mock_callback.assert_called()

    # ─────────────────────────────────────────────────────────────
    # 3. Reminder Tools Verification
    # ─────────────────────────────────────────────────────────────

    def test_create_reminder_tool(self):
        tool = CreateReminderTool(self.scheduler)
        future_time = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=30)).isoformat()
        res = asyncio.run(tool.execute(
            reminder_text="Team sync meeting",
            time_utc=future_time,
            spoken_time="in 30 minutes"
        ))
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "success")
        self.assertIn("Team sync meeting", res["message"])

    def test_list_reminders_tool(self):
        tool = ListRemindersTool(self.scheduler)
        # Empty
        res_empty = asyncio.run(tool.execute())
        self.assertTrue(res_empty["ok"])
        self.assertIn("no active reminders", res_empty["message"])

        # Add one and list
        future_time = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5)).isoformat()
        self.scheduler.create_reminder("Check email", future_time, spoken_time="in 5 hours")
        res_list = asyncio.run(tool.execute())
        self.assertTrue(res_list["ok"])
        self.assertEqual(res_list["data"]["count"], 1)
        self.assertIn("Check email", res_list["message"])

    def test_cancel_reminder_tool(self):
        tool = CancelReminderTool(self.scheduler)
        # Add reminder
        future_time = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat()
        self.scheduler.create_reminder("Take medicine", future_time)

        # Cancel it
        res = asyncio.run(tool.execute(query="medicine"))
        self.assertTrue(res["ok"])
        self.assertIn("cancelled successfully", res["message"])

        # Cancel non-existent
        res_fail = asyncio.run(tool.execute(query="nonexistent"))
        self.assertFalse(res_fail["ok"])
        self.assertEqual(res_fail["error_code"], ErrorCode.NOT_FOUND.value)


if __name__ == "__main__":
    unittest.main()
