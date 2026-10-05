# tests/golden_utterances.py
"""
Golden Utterances Regression Test Suite for Kate Personal Assistant.
Contains 60+ English, Hindi, and Hinglish test cases covering ~35 system intents.
Must be run after every phase to ensure zero intent regression.
Runs 100% hermetically offline without network or LLM dependencies.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from assistant.interpreter import RuleBasedInterpreter

GOLDEN_UTTERANCES = [
    # ── 1. Conversation and Greetings (5) ──
    ('hi', 'conversation', {}),
    ('hello kate', 'conversation', {}),
    ('who are you', 'conversation', {}),
    ('what can you do', 'conversation', {}),
    ('introduce yourself', 'conversation', {}),

    # ── 2. Desktop Controls (12) ──
    ('take a screenshot', 'take_screenshot', {}),
    ('screenshot lo', 'take_screenshot', {}),
    ('screenshot le lo', 'take_screenshot', {}),
    ('show desktop', 'show_desktop', {}),
    ('desktop dikhao', 'show_desktop', {}),
    ('switch window to chrome', 'switch_window', {'window_title': 'chrome'}),
    ('focus window vscode', 'switch_window', {'window_title': 'vscode'}),
    ('lock my pc', 'lock_workstation', {}),
    ('pc lock karo', 'lock_workstation', {}),
    ('lock workstation', 'lock_workstation', {}),
    ('organize downloads', 'organize_folder', {}),
    ('downloads saaf karo', 'organize_folder', {}),

    # ── 3. Media Controls (8) ──
    ('stop the music', 'control_media', {'action': 'stop'}),
    ('video roko', 'control_media', {'action': 'stop'}),
    ('pause playback', 'control_media', {'action': 'play_pause'}),
    ('resume video', 'control_media', {'action': 'play_pause'}),
    ('next track', 'control_media', {'action': 'next'}),
    ('mute audio', 'control_volume', {'action': 'toggle_mute'}),
    ('volume up', 'control_volume', {'action': 'up'}),
    ('volume kam karo', 'control_volume', {'action': 'down'}),

    # ── 4. Media Playback and Song Downloader (8) ──
    ('play arijit singh songs', 'play_media', {'platform': 'youtube'}),
    ('gana bajao tum hi ho', 'play_media', {'platform': 'youtube'}),
    ('open youtube and play lofi beats', 'play_media', {'platform': 'youtube'}),
    ('spotify pe shape of you chalao', 'play_media', {'platform': 'spotify'}),
    ('download song tum hi ho', 'download_song', {'title': 'tum hi ho'}),
    ('gana download karo chaleya', 'download_song', {'title': 'chaleya'}),
    ('make a pdf of my report', 'generate_pdf', {}),
    ('pdf banao research document', 'generate_pdf', {}),

    # ── 5. Project and Profile Management (8) ──
    ('run project backend', 'run_project', {'project_name': 'backend'}),
    ('start server frontend', 'run_project', {'project_name': 'frontend'}),
    ('current profile', 'current_profile', {}),
    ('what mode am i in', 'current_profile', {}),
    ('switch profile to coding', 'switch_profile', {'profile_name': 'coding'}),
    ('set mode to study', 'switch_profile', {'profile_name': 'study'}),
    ('save workspace snapshot', 'save_workspace', {}),
    ('continue working', 'continue_working', {}),

    # ── 6. Recommendations and Goals (4) ──
    ('what should i do next', 'recommend_next_action', {}),
    ('suggest next step', 'recommend_next_action', {}),
    ('prioritize learning goals', 'tune_recommendation_weights', {}),
    ('focus on projects', 'tune_recommendation_weights', {}),

    # ── 7. Personal Memory (6) ──
    ('remember that my birthday is tomorrow', 'store_memory', {}),
    ('note down that i prefer brave browser', 'store_memory', {}),
    ('what was my last goal', 'memory_query', {}),
    ('retrieve memories about python', 'memory_query', {}),
    ('query memory for goals', 'query_memory', {}),
    ('memory history for record 12', 'memory_history', {}),
    ('prune memories older than 30 days', 'prune_memories', {}),

    # ── 8. Project Scoped Memory (5) ──
    ('save project memory for current project', 'store_project_memory', {}),
    ('list project memories', 'list_project_memories', {}),
    ('switch focus to project Jarvis', 'switch_project_focus', {}),
    ('project catchup brief for Jarvis', 'project_catchup_brief', {}),
    ('list active projects', 'list_active_projects', {}),

    # ── 9. Git Operations (4) ──
    ('git status', 'git_status', {}),
    ('uncommitted files dikhao', 'git_status', {}),
    ('git diff summary', 'git_diff_summary', {}),
    ('commit changes with message fix login bug', 'git_commit', {}),

    # ── 10. Codebase and Files (4) ──
    ('index codebase in current directory', 'index_codebase', {}),
    ('search codebase for authentication', 'search_codebase', {}),
    ('delete file temp.log', 'delete_file', {}),
    ('restore quarantined file backup_abc123', 'restore_quarantined_file', {}),

    # ── 11. System and Multi-Task (5) ──
    ('get system telemetry', 'get_system_telemetry', {}),
    ('battery status', 'get_system_telemetry', {}),
    ('send notification reminder to drink water', 'send_notification', {}),
    ('open brave and open spotify', 'multi_task', {}),
    ('screenshot lo aur desktop dikhao', 'multi_task', {}),
]


class TestGoldenUtterances(unittest.TestCase):
    def setUp(self):
        self.interpreter = RuleBasedInterpreter()
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_golden_utterances(self):
        passed = 0
        failed_cases = []

        for query, expected_intent, expected_entities in GOLDEN_UTTERANCES:
            with self.subTest(query=query):
                result = self.loop.run_until_complete(self.interpreter.interpret(query))
                actual_intent = result.get('intent')

                if actual_intent != expected_intent:
                    failed_cases.append(
                        f"FAIL '{query}': expected intent='{expected_intent}', got='{actual_intent}'"
                    )
                else:
                    passed += 1
                    actual_entities = result.get('entities', {})
                    for ek, ev in expected_entities.items():
                        self.assertEqual(
                            actual_entities.get(ek), ev,
                            f"Entity '{ek}' mismatch for '{query}': expected {ev}, got {actual_entities.get(ek)}"
                        )

        total = len(GOLDEN_UTTERANCES)
        if failed_cases:
            msg = f"{len(failed_cases)} of {total} utterances failed:\n" + "\n".join(failed_cases)
            self.fail(msg)

        print(f"\n[PASS] All {passed}/{total} Golden Utterances matched expected intents successfully.")


if __name__ == '__main__':
    unittest.main()
