"""
Unit tests for Asia/Kolkata Time Parsing Engine.
Validates 30+ scenarios across Hindi, Hinglish, and English time expressions.
"""

import datetime
import unittest

from assistant.time_parser import parse_natural_time, IST


class TestAsiaKolkataTimeParsing(unittest.TestCase):
    def setUp(self):
        # Base reference time: 2026-09-27 20:00:00 IST (8:00 PM evening)
        self.ref_evening = datetime.datetime(2026, 9, 27, 20, 0, 0, tzinfo=IST)
        # Morning reference time: 2026-09-27 10:00:00 IST (10:00 AM morning)
        self.ref_morning = datetime.datetime(2026, 9, 27, 10, 0, 0, tzinfo=IST)

    # 1. Subah (Morning AM) Rules
    def test_subah_8_baje(self):
        res = parse_natural_time("subah 8 baje water reminder", self.ref_evening)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 8)
        self.assertEqual(res.minute, 0)
        self.assertTrue(res.is_tomorrow)  # 8 AM passed when ref is 8 PM

    def test_subah_8_30_baje(self):
        res = parse_natural_time("subah 8:30 baje dawai leni hai", self.ref_evening)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 8)
        self.assertEqual(res.minute, 30)

    def test_subah_11_am(self):
        res = parse_natural_time("subah 11 baje meeting", self.ref_morning)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 11)
        self.assertFalse(res.is_tomorrow)  # 11 AM is future when ref is 10 AM

    def test_subah_12_baje(self):
        res = parse_natural_time("subah 12 baje", self.ref_morning)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 0)

    # 2. Dopahar (Afternoon PM) Rules
    def test_dopahar_12_baje_noon(self):
        res = parse_natural_time("dopahar 12 baje lunch", self.ref_morning)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 12)
        self.assertEqual(res.minute, 0)
        self.assertFalse(res.is_tomorrow)

    def test_dopahar_12_30_baje(self):
        res = parse_natural_time("dopahar 12:30 baje break", self.ref_morning)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 12)
        self.assertEqual(res.minute, 30)

    def test_dopahar_1_baje(self):
        res = parse_natural_time("dopahar 1 baje project update", self.ref_morning)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 13)

    def test_dopahar_2_baje(self):
        res = parse_natural_time("dopahar 2 baje class", self.ref_morning)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 14)

    def test_dopahar_4_baje(self):
        res = parse_natural_time("dopahar 4 baje chai", self.ref_evening)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 16)
        self.assertTrue(res.is_tomorrow)  # 4 PM passed when ref is 8 PM

    # 3. Shaam (Evening PM) Rules
    def test_shaam_5_baje(self):
        res = parse_natural_time("shaam 5 baje gym", self.ref_morning)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 17)

    def test_shaam_6_baje(self):
        res = parse_natural_time("shaam 6 baje walk", self.ref_evening)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 18)
        self.assertTrue(res.is_tomorrow)  # 6 PM passed when ref is 8 PM

    def test_shaam_7_baje(self):
        res = parse_natural_time("shaam 7 baje call mom", self.ref_morning)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 19)
        self.assertFalse(res.is_tomorrow)

    # 4. Raat (Night / Late-Night) Rules
    def test_raat_7_baje(self):
        res = parse_natural_time("raat 7 baje dinner", self.ref_morning)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 19)

    def test_raat_8_baje(self):
        res = parse_natural_time("raat 8 baje medicine", self.ref_morning)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 20)

    def test_raat_11_baje(self):
        res = parse_natural_time("raat 11 baje sona hai", self.ref_evening)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 23)
        self.assertFalse(res.is_tomorrow)  # 11 PM is future when ref is 8 PM

    def test_raat_12_baje_midnight(self):
        # "raat 12 baje" must be 00:00 midnight (next day when said at 8 PM)
        res = parse_natural_time("raat 12 baje birthday wish", self.ref_evening)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 0)
        self.assertEqual(res.minute, 0)
        self.assertTrue(res.is_tomorrow)

    def test_raat_1_baje(self):
        # "raat 1 baje" must be 01:00 AM (next calendar day when said at 8 PM)
        res = parse_natural_time("raat 1 baje check server", self.ref_evening)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 1)
        self.assertTrue(res.is_tomorrow)

    def test_raat_2_baje(self):
        # Critical requirement: "raat 2 baje" must be 02:00 AM
        res = parse_natural_time("raat 2 baje padhai karni hai", self.ref_evening)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 2)
        self.assertEqual(res.minute, 0)
        self.assertTrue(res.is_tomorrow)

    def test_raat_3_baje(self):
        res = parse_natural_time("raat 3 baje water break", self.ref_evening)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 3)
        self.assertTrue(res.is_tomorrow)

    def test_raat_4_baje(self):
        res = parse_natural_time("raat 4 baje sleep", self.ref_evening)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 4)
        self.assertTrue(res.is_tomorrow)

    # 5. Standalone 12 Baje Rule
    def test_standalone_12_baje_in_morning(self):
        # If current hour < 12 -> 12:00 PM today (noon)
        res = parse_natural_time("12 baje call karna", self.ref_morning)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 12)
        self.assertFalse(res.is_tomorrow)

    def test_standalone_12_baje_in_evening(self):
        # If current hour >= 12 -> 00:00 midnight tonight
        res = parse_natural_time("12 baje call karna", self.ref_evening)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 0)
        self.assertTrue(res.is_tomorrow)

    def test_english_12_pm(self):
        res = parse_natural_time("at 12 pm", self.ref_morning)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 12)

    def test_english_12_am(self):
        res = parse_natural_time("at 12 am", self.ref_evening)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 0)

    # 6. Automatic Next-Day Rollover
    def test_past_time_rolls_to_tomorrow(self):
        # Ref is 8 PM (20:00), user asks "6 pm"
        res = parse_natural_time("6 pm meeting", self.ref_evening)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 18)
        self.assertTrue(res.is_tomorrow)

    # 7. Explicit Dates (kal, parso, aaj)
    def test_kal_morning(self):
        res = parse_natural_time("kal subah 9 baje interview", self.ref_morning)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 9)
        self.assertTrue(res.is_tomorrow)
        self.assertEqual(res.dt_ist.day, 28)

    def test_parso_evening(self):
        res = parse_natural_time("parso shaam 6 baje movie", self.ref_morning)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 18)
        self.assertEqual(res.dt_ist.day, 29)

    def test_aaj_evening(self):
        res = parse_natural_time("aaj shaam 7 baje event", self.ref_morning)
        self.assertIsNotNone(res)
        self.assertEqual(res.hour, 19)
        self.assertEqual(res.dt_ist.day, 27)

    # 8. Relative Offsets
    def test_relative_minutes(self):
        res = parse_natural_time("15 minute baad coffee", self.ref_morning)
        self.assertIsNotNone(res)
        expected = self.ref_morning + datetime.timedelta(minutes=15)
        self.assertEqual(res.dt_ist, expected)
        self.assertIn("15 minute baad", res.spoken_time)

    def test_relative_hours(self):
        res = parse_natural_time("2 ghante baad break", self.ref_morning)
        self.assertIsNotNone(res)
        expected = self.ref_morning + datetime.timedelta(hours=2)
        self.assertEqual(res.dt_ist, expected)

    def test_english_in_minutes(self):
        res = parse_natural_time("in 30 mins submit assignment", self.ref_morning)
        self.assertIsNotNone(res)
        expected = self.ref_morning + datetime.timedelta(minutes=30)
        self.assertEqual(res.dt_ist, expected)

    # 9. UTC Conversion Accuracy
    def test_utc_conversion(self):
        res = parse_natural_time("subah 10 baje", self.ref_morning)
        self.assertIsNotNone(res)
        # IST is UTC+5:30 -> UTC is 10:00 - 5h30m = 04:30
        self.assertEqual(res.dt_utc.hour, 4)
        self.assertEqual(res.dt_utc.minute, 30)

    # 10. Boilerplate Cleanup
    def test_reminder_text_cleanup(self):
        res = parse_natural_time("subah 8 baje mujhe paani peene ki yaad dilana", self.ref_evening)
        self.assertIsNotNone(res)
        self.assertNotIn("subah 8 baje", res.reminder_text)
        self.assertNotIn("yaad dilana", res.reminder_text)
        self.assertIn("paani", res.reminder_text)


if __name__ == "__main__":
    unittest.main()
