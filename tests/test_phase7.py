import asyncio
import os
import tempfile
import unittest

from assistant.fallback_engine import FallbackEngine
from assistant.self_healing import AutonomousThinkingEngine
from assistant.tools import ToolResult


class TestPhase7HardeningAndFallbacks(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.fe = FallbackEngine()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_fallback_cascade_succeeds_on_second_alternative(self):
        """Verify cascade stops on first success and tracks alternatives tried."""
        async def method_1(ctx):
            return ToolResult(ok=False, message="Method 1 failed")

        async def method_2(ctx):
            return ToolResult(ok=True, data={"value": 42}, message="Method 2 worked!")

        async def method_3(ctx):
            return ToolResult(ok=True, message="Method 3 should not be called")

        candidates = [("m1", method_1), ("m2", method_2), ("m3", method_3)]

        res = asyncio.run(self.fe.execute_cascade("test_domain", candidates))
        self.assertTrue(res.ok)
        self.assertEqual(res.data["value"], 42)
        self.assertEqual(res.alternatives_tried, ["m1", "m2"])

    def test_fallback_cascade_exhausts_all_three(self):
        """Verify cascade tries up to 3 and reports failure if all fail."""
        def m1(ctx):
            return {"ok": False, "message": "m1 err"}

        def m2(ctx):
            raise RuntimeError("m2 crash")

        def m3(ctx):
            return ToolResult(ok=False, message="m3 err")

        candidates = [("m1", m1), ("m2", m2), ("m3", m3)]
        res = asyncio.run(self.fe.execute_cascade("test_domain", candidates))
        self.assertFalse(res.ok)
        self.assertEqual(len(res.alternatives_tried), 3)
        self.assertIn("All 3 attempts", res.message)

    def test_file_lookup_fuzzy_rapidfuzz_fallback(self):
        """Verify file lookup recovers from typo via RapidFuzz."""
        target_file = os.path.join(self.temp_dir.name, "user_requirements_v2.json")
        with open(target_file, "w") as f:
            f.write("{}")

        # Query with common typo: "user_requriements_v2.json"
        res = self.fe.fallback_find_file("user_requriements_v2.json", self.temp_dir.name)
        self.assertTrue(res.ok)
        self.assertEqual(res.data["method"], "rapidfuzz_match")
        self.assertIn("rapidfuzz_match", res.alternatives_tried)
        self.assertEqual(os.path.basename(res.data["path"]), "user_requirements_v2.json")

    def test_self_healing_permission_gate(self):
        """Verify AutonomousThinkingEngine gates unknown actions behind permission."""
        te = AutonomousThinkingEngine()
        res = te.think_and_remediate(
            query="generate 3d avatar for user",
            failed_intent="avatar_3d",
            error_msg="no specific route found",
            interpretation={"intent": "avatar_3d"}
        )
        self.assertEqual(res["status"], "permission_required")
        self.assertIn("draft_id", res)

        # Confirm permission
        install_res = te.confirm_and_install_skill(res["draft_id"], approved=True)
        self.assertEqual(install_res["status"], "success")
        self.assertIn("Permission confirmed", install_res["response"])


if __name__ == "__main__":
    unittest.main()
