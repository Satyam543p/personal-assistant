import unittest
from assistant.tts.base import TTSProvider
from assistant.tts.manager import clean_text_for_speech, TTSManager
from assistant.tts.kokoro_provider import KokoroTTSProvider
from assistant.tts.edge_provider import EdgeTTSProvider


class TestTTSSubsystem(unittest.TestCase):
    def test_clean_text_for_speech_bullet_reduction(self):
        long_bullets = (
            "• naruto\n"
            "• My name is Satyam Pandey.\n"
            "• I am the creator of Jarvis.\n"
            "• More long bullet items."
        )
        concise = clean_text_for_speech(long_bullets, max_words=20)
        self.assertIn("naruto", concise)
        self.assertIn("Satyam Pandey", concise)
        # Should not contain the 4th bullet
        self.assertNotIn("More long bullet", concise)

    def test_clean_text_for_speech_markdown_stripping(self):
        md_text = "Here is **bold** text and `inline code` with [a link](https://google.com)."
        cleaned = clean_text_for_speech(md_text)
        self.assertNotIn("**", cleaned)
        self.assertNotIn("`", cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertIn("bold text", cleaned)

    def test_tts_manager_interface_compliance(self):
        manager = TTSManager(auto_prewarm=False)
        self.assertIsInstance(manager.kokoro, TTSProvider)
        self.assertIsInstance(manager.edge, TTSProvider)
        self.assertEqual(manager.kokoro.name, "kokoro")
        self.assertEqual(manager.edge.name, "edge")


if __name__ == "__main__":
    unittest.main()
