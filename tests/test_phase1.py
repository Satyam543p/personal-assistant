import asyncio
import json
import time
import unittest
from unittest.mock import MagicMock

from assistant.interpreter import (
    RuleBasedInterpreter,
    parse_guarded_download_request,
    resolve_media_platform,
)
from assistant.safety import is_confirmation, is_negation
from assistant.router import JarvisRouter


class FakeContextStore:
    def __init__(self):
        self._store = {}

    def get(self, key: str):
        return self._store.get(key)

    def set(self, key: str, value: str):
        self._store[key] = value

    def delete(self, key: str):
        self._store.pop(key, None)


class TestPhase1(unittest.TestCase):
    def setUp(self):
        self.interpreter = RuleBasedInterpreter()

    # ─────────────────────────────────────────────────────────────
    # 1. Guarded Media Download NLU & Platform Resolution
    # ─────────────────────────────────────────────────────────────

    def test_platform_resolution(self):
        self.assertEqual(resolve_media_platform("https://www.youtube.com/watch?v=dQw4w9WgXcQ"), "youtube")
        self.assertEqual(resolve_media_platform("https://youtube.com/shorts/abcd1234"), "youtube_shorts")
        self.assertEqual(resolve_media_platform("https://music.youtube.com/watch?v=xyz"), "youtube_music")
        self.assertEqual(resolve_media_platform("https://www.instagram.com/reel/C3zYx7/"), "instagram")
        self.assertEqual(resolve_media_platform("https://www.tiktok.com/@user/video/1234567"), "tiktok")
        self.assertEqual(resolve_media_platform("https://www.facebook.com/watch/?v=123"), "facebook")
        self.assertEqual(resolve_media_platform("https://x.com/user/status/123"), "twitter")
        self.assertEqual(resolve_media_platform("https://twitter.com/user/status/123"), "twitter")
        self.assertEqual(resolve_media_platform("https://vimeo.com/123456"), "vimeo")
        self.assertEqual(resolve_media_platform("https://reddit.com/r/videos/comments/xyz"), "reddit")
        self.assertEqual(resolve_media_platform("https://example.com/file.zip"), "generic")

    def test_guarded_download_with_urls(self):
        # YouTube URL
        res = parse_guarded_download_request("download https://www.youtube.com/watch?v=xyz")
        self.assertIsNotNone(res)
        self.assertEqual(res["intent"], "download_video")
        self.assertEqual(res["platform"], "youtube")

        # YouTube Shorts URL
        res = parse_guarded_download_request("https://youtube.com/shorts/abcd1234 download karo")
        self.assertIsNotNone(res)
        self.assertEqual(res["intent"], "download_video")
        self.assertEqual(res["platform"], "youtube_shorts")

        # Instagram URL
        res = parse_guarded_download_request("https://www.instagram.com/reel/C3zYx7/ save kar do")
        self.assertIsNotNone(res)
        self.assertEqual(res["platform"], "instagram")

        # Audio request with URL
        res = parse_guarded_download_request("extract audio mp3 from https://www.youtube.com/watch?v=xyz")
        self.assertIsNotNone(res)
        self.assertEqual(res["intent"], "extract_audio")

    def test_guarded_download_with_keywords(self):
        # Song download request
        res = parse_guarded_download_request("download song kesariya")
        self.assertIsNotNone(res)
        self.assertEqual(res["intent"], "download_song")

        # Video download request
        res = parse_guarded_download_request("download video python tutorial")
        self.assertIsNotNone(res)
        self.assertEqual(res["intent"], "download_video")

        # Reel download request without context
        res = parse_guarded_download_request("ye reel download karo")
        self.assertIsNone(res)

    def test_guarded_download_anaphora_with_context(self):
        context = {"last_url": "https://www.youtube.com/watch?v=active123"}
        res = parse_guarded_download_request("isko download kar", context=context)
        self.assertIsNotNone(res)
        self.assertEqual(res["entities"]["source"], "https://www.youtube.com/watch?v=active123")
        self.assertEqual(res["platform"], "youtube")

        res2 = parse_guarded_download_request("ye download karo", context=context)
        self.assertIsNotNone(res2)
        self.assertEqual(res2["entities"]["source"], "https://www.youtube.com/watch?v=active123")

    def test_guarded_download_rejection_non_media(self):
        self.assertIsNone(parse_guarded_download_request("download windows updates"))
        self.assertIsNone(parse_guarded_download_request("download python installer"))
        self.assertIsNone(parse_guarded_download_request("download nvidia drivers"))
        self.assertIsNone(parse_guarded_download_request("download chrome browser"))

    # ─────────────────────────────────────────────────────────────
    # 2. Hindi & Hinglish Affirmation / Negation Safety Tokens
    # ─────────────────────────────────────────────────────────────

    def test_hindi_hinglish_affirmations(self):
        self.assertTrue(is_confirmation("yes"))
        self.assertTrue(is_confirmation("haan"))
        self.assertTrue(is_confirmation("haan kar do"))
        self.assertTrue(is_confirmation("kar do"))
        self.assertTrue(is_confirmation("bilkul"))
        self.assertTrue(is_confirmation("karo"))
        self.assertTrue(is_confirmation("sure"))
        self.assertTrue(is_confirmation("proceed"))
        self.assertFalse(is_confirmation("random query"))

    def test_hindi_hinglish_negations(self):
        self.assertTrue(is_negation("no"))
        self.assertTrue(is_negation("nahi"))
        self.assertTrue(is_negation("nahin"))
        self.assertTrue(is_negation("mat karo"))
        self.assertTrue(is_negation("ruko"))
        self.assertTrue(is_negation("rehne do"))
        self.assertTrue(is_negation("rehnde"))
        self.assertTrue(is_negation("cancel"))
        self.assertTrue(is_negation("stop"))
        self.assertFalse(is_negation("haan"))

    # ─────────────────────────────────────────────────────────────
    # 3. Confirmation Loop 30-Second TTL & Safety Order
    # ─────────────────────────────────────────────────────────────

    def _create_mock_router(self, context_store):
        router = JarvisRouter(
            db_manager=MagicMock(),
            context_store=context_store,
            project_registry=MagicMock(),
            memory_manager=MagicMock(),
            tool_executor=MagicMock(),
            plan_executor=MagicMock(),
            cloud_client=MagicMock(),
            conversation_handler=MagicMock()
        )
        return router

    def test_confirmation_timeout_ttl(self):
        context_store = FakeContextStore()
        action = {
            "action_id": "act_123",
            "intent": "delete_file",
            "entities": {"file_path": "test.txt"},
            "resolved_reference": "test.txt",
            "timestamp": time.time() - 35.0,
            "action_description": "safely delete file 'test.txt'"
        }
        context_store.set("pending_action", json.dumps(action))

        router = self._create_mock_router(context_store)
        res = asyncio.run(router.route("yes", {"intent": "conversation", "confidence": 0.5}))

        self.assertEqual(res["status"], "failure")
        self.assertEqual(res["route"], "confirmation_expired")
        self.assertIn("timed out after 30 seconds", res["response"])
        self.assertIsNone(context_store.get("pending_action"))

    def test_confirmation_negation_before_affirmation(self):
        context_store = FakeContextStore()
        action = {
            "action_id": "act_456",
            "intent": "delete_file",
            "entities": {"file_path": "test.txt"},
            "resolved_reference": "test.txt",
            "timestamp": time.time() - 5.0,
            "action_description": "safely delete file 'test.txt'"
        }
        context_store.set("pending_action", json.dumps(action))

        router = self._create_mock_router(context_store)
        res = asyncio.run(router.route("mat karo", {"intent": "conversation", "confidence": 0.5}))

        self.assertEqual(res["status"], "failure")
        self.assertEqual(res["route"], "confirmation_cancelled")
        self.assertEqual(res["response"], "Action cancelled.")
        self.assertIsNone(context_store.get("pending_action"))

    def test_confirmation_affirmation_executes(self):
        context_store = FakeContextStore()
        action = {
            "action_id": "act_789",
            "intent": "write_file",
            "entities": {"file_path": "hello.py"},
            "resolved_reference": "hello.py",
            "timestamp": time.time() - 5.0,
            "action_description": "write to file 'hello.py'"
        }
        context_store.set("pending_action", json.dumps(action))

        router = self._create_mock_router(context_store)

        async def mock_exec(*args, **kwargs):
            return {"status": "success", "response": "File written"}
        router._execute_tool_safely = mock_exec

        res = asyncio.run(router.route("haan kar do", {"intent": "conversation", "confidence": 0.5}))
        self.assertEqual(res["status"], "success")
        self.assertIsNone(context_store.get("pending_action"))

    # ─────────────────────────────────────────────────────────────
    # 4. Natural Time Integration in RuleBasedInterpreter
    # ─────────────────────────────────────────────────────────────

    def test_interpreter_reminder_routing(self):
        res = asyncio.run(self.interpreter.interpret("15 minute baad paani peene ka reminder lagao"))
        self.assertEqual(res["intent"], "create_reminder")
        self.assertIn("15", res["entities"]["spoken_time"])

        res2 = asyncio.run(self.interpreter.interpret("kal subah 8 baje meeting yaad dilao"))
        self.assertEqual(res2["intent"], "create_reminder")
        self.assertIn("8:00 AM", res2["entities"]["spoken_time"])
        self.assertTrue(res2["entities"]["is_tomorrow"])


if __name__ == "__main__":
    unittest.main()
