"""
Time & Schedule Natural Language Parser for Kate Assistant.
Provides resilient Asia/Kolkata timezone support, natural Hinglish and English
time expressions, 12 o'clock resolution, and automatic next-day rollover.
"""

import datetime
import re
from dataclasses import dataclass
from typing import Optional

# Resilient Asia/Kolkata timezone loader with UTC+5:30 fallback
try:
    import zoneinfo
    try:
        IST = zoneinfo.ZoneInfo("Asia/Kolkata")
    except Exception:
        IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="Asia/Kolkata")
except Exception:
    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="Asia/Kolkata")

UTC = datetime.timezone.utc


@dataclass
class ParsedTimeResult:
    dt_ist: datetime.datetime
    dt_utc: datetime.datetime
    hour: int
    minute: int
    is_tomorrow: bool
    spoken_time: str
    reminder_text: str


def get_now_ist() -> datetime.datetime:
    """Returns current localized timestamp in Asia/Kolkata timezone."""
    return datetime.datetime.now(IST)


def parse_natural_time(query: str, ref_time: Optional[datetime.datetime] = None) -> Optional[ParsedTimeResult]:
    """
    Parses a natural language query containing time or reminder expressions.
    Supports Hindi/Hinglish (subah, dopahar, shaam, raat, baje, baad) and English (am, pm, in X min).

    Time Modifiers:
    - subah: AM (e.g. 'subah 8 baje' = 08:00 AM)
    - dopahar: PM (e.g. 'dopahar 2 baje' = 02:00 PM, 'dopahar 12 baje' = 12:00 PM)
    - shaam: PM (e.g. 'shaam 6 baje' = 06:00 PM, 'shaam 7 baje' = 07:00 PM)
    - raat:
        - 12 -> 00:00 midnight
        - 1, 2, 3, 4 -> 01:00, 02:00, 03:00, 04:00 AM (calendar next day if ref_time >= 12:00)
        - 7, 8, 9, 10, 11 -> 19:00, 20:00, 21:00, 22:00, 23:00 PM
    - 12 O'Clock Rules:
        - 'dopahar 12 baje' / '12 pm' = 12:00 PM (noon)
        - 'raat 12 baje' / '12 am' = 00:00 AM (midnight)
        - standalone '12 baje': if current hour < 12 -> 12:00 PM today; else -> 00:00 midnight tonight
    - Rollover: Any time that has already passed today in IST automatically rolls over to tomorrow.
    """
    if not query or not isinstance(query, str):
        return None

    if ref_time is None:
        ref_time = get_now_ist()
    elif ref_time.tzinfo is None:
        ref_time = ref_time.replace(tzinfo=IST)
    else:
        ref_time = ref_time.astimezone(IST)

    q = query.lower().strip()

    # 1. Relative offset expressions (e.g., '15 minute baad', 'in 30 minutes', '2 ghante baad')
    rel_match = re.search(
        r"\b(?:in\s+)?(\d+)\s*(minutes?|mins?|minute|hours?|hrs?|ghante?|ghanto)\s*(?:baad|me|later)?\b",
        q,
        re.IGNORECASE
    )
    if rel_match:
        val = int(rel_match.group(1))
        unit = rel_match.group(2).lower()
        if any(u in unit for u in ("min", "minute")):
            delta = datetime.timedelta(minutes=val)
            spoken = f"{val} minute baad"
        else:
            delta = datetime.timedelta(hours=val)
            spoken = f"{val} ghante baad"

        target_ist = ref_time + delta
        target_utc = target_ist.astimezone(UTC)
        is_tomorrow = target_ist.date() > ref_time.date()

        clean_reminder = re.sub(re.escape(rel_match.group(0)), "", query, flags=re.IGNORECASE)
        clean_reminder = _clean_reminder_boilerplate(clean_reminder)

        return ParsedTimeResult(
            dt_ist=target_ist,
            dt_utc=target_utc,
            hour=target_ist.hour,
            minute=target_ist.minute,
            is_tomorrow=is_tomorrow,
            spoken_time=spoken,
            reminder_text=clean_reminder
        )

    # 2. Date offset extraction (kal, parso, aaj, tomorrow)
    day_offset = 0
    explicit_date = False
    if re.search(r"\b(?:parso|day after tomorrow)\b", q):
        day_offset = 2
        explicit_date = True
    elif re.search(r"\b(?:kal|tomorrow)\b", q):
        day_offset = 1
        explicit_date = True
    elif re.search(r"\b(?:aaj|today)\b", q):
        day_offset = 0
        explicit_date = True

    # 3. Absolute clock patterns
    clock_pat = r"\b(?:(?:at|around)\s+)?(subah|dopahar|shaam|raat)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|baje)?\b"
    matches = list(re.finditer(clock_pat, q))

    valid_m = None
    for m in matches:
        mod, h_str, min_str, suf = m.groups()
        if not h_str:
            continue
        # Require either modifier, suffix, minute, or 'baje'
        if mod or suf or min_str or (suf == "baje"):
            valid_m = m
            break

    if not valid_m:
        return None

    modifier, hour_str, min_str, suffix = valid_m.groups()
    raw_h = int(hour_str)
    minute = int(min_str) if min_str else 0

    if raw_h > 24 or minute >= 60:
        return None

    hour = raw_h
    is_night_early_morning = False

    if modifier == "subah":
        hour = 0 if raw_h == 12 else raw_h
    elif modifier == "dopahar":
        hour = 12 if raw_h == 12 else (raw_h + 12 if raw_h < 12 else raw_h)
    elif modifier == "shaam":
        hour = raw_h + 12 if raw_h < 12 else raw_h
    elif modifier == "raat":
        if raw_h == 12:
            hour = 0
            is_night_early_morning = True
        elif 1 <= raw_h <= 4:
            hour = raw_h
            is_night_early_morning = True
        elif 7 <= raw_h <= 11:
            hour = raw_h + 12
        elif raw_h < 12:
            hour = raw_h + 12
    elif suffix == "am":
        hour = 0 if raw_h == 12 else raw_h
    elif suffix == "pm":
        hour = 12 if raw_h == 12 else (raw_h + 12 if raw_h < 12 else raw_h)
    else:
        # Bare 'baje' or unspecified AM/PM
        if raw_h == 12:
            # Standalone 12 baje:
            # If current hour < 12 -> 12:00 PM noon today
            # If current hour >= 12 -> 00:00 midnight tonight
            if ref_time.hour < 12:
                hour = 12
            else:
                hour = 0
                is_night_early_morning = True
        elif 1 <= raw_h <= 6:
            # If said in afternoon/evening and hour is 1-6, user usually means PM today or tomorrow
            if ref_time.hour > raw_h and (raw_h + 12) > ref_time.hour:
                hour = raw_h + 12
            else:
                hour = raw_h
        else:
            hour = raw_h

    # Construct target date
    target_date = ref_time.date() + datetime.timedelta(days=day_offset)
    tentative_dt = datetime.datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=IST)

    # Rollover logic when no explicit date (kal/parso) was given:
    if not explicit_date:
        if is_night_early_morning and ref_time.hour >= 12:
            # 'raat 2 baje' or 'raat 12 baje' said in afternoon/evening -> tonight's midnight/early morning (tomorrow)
            tentative_dt += datetime.timedelta(days=1)
        elif tentative_dt <= ref_time:
            # Time already passed today -> auto-advance to tomorrow
            tentative_dt += datetime.timedelta(days=1)

    is_tomorrow = (tentative_dt.date() > ref_time.date())
    target_utc = tentative_dt.astimezone(UTC)

    # Human-friendly spoken time format
    time_display = tentative_dt.strftime("%I:%M %p").lstrip("0")
    if is_tomorrow:
        day_str = "kal "
    elif tentative_dt.date() == ref_time.date():
        day_str = "aaj "
    else:
        day_str = f"{tentative_dt.strftime('%d %B')} ko "

    mod_display = f"{modifier} " if modifier else ""
    spoken_time = f"{day_str}{mod_display}{time_display}".strip()

    clean_reminder = re.sub(re.escape(valid_m.group(0)), "", query, flags=re.IGNORECASE)
    clean_reminder = _clean_reminder_boilerplate(clean_reminder)

    return ParsedTimeResult(
        dt_ist=tentative_dt,
        dt_utc=target_utc,
        hour=hour,
        minute=minute,
        is_tomorrow=is_tomorrow,
        spoken_time=spoken_time,
        reminder_text=clean_reminder
    )


def _clean_reminder_boilerplate(text: str) -> str:
    """Strips reminder command trigger phrases leaving only the core reminder subject."""
    cleaned = text
    patterns = [
        r"\b(?:reminder\s+lagao|yaad\s+dilana|yaad\s+dila\s+dena|remind\s+me\s+to|remind\s+me)\b",
        r"\b(?:mujhe\s+yaad\s+dilana|ki\s+yaad\s+dilana|ka\s+reminder)\b",
        r"\b(?:kal|aaj|parso|tomorrow|today)\b",
        r"\b(?:at|around|ko|pe)\b"
    ]
    for pat in patterns:
        cleaned = re.sub(pat, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "Reminder"
