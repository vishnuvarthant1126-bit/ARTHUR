"""Turn "5 pm", "in 20 minutes", "tomorrow at 9" into an exact moment.

Language models are unreliable at date arithmetic, so the model passes the user's
words as they are and this plain Python code does the calculation.

Understood (case doesn't matter):
    in 20 minutes / in 2 hours / in 1 hour 30 minutes / in half an hour / in 3 days
    5pm / 5:30 pm / 17:00 / noon / midnight        -> today, or tomorrow if already past
    at 5                                           -> the next 5 o'clock (am or pm)
    today 6pm / tonight / tomorrow / tomorrow 9am / tomorrow morning
    friday / next friday 3pm / on monday at 8
    2026-10-05 / 2026-10-05 14:30 / 5 october 9am / oct 5
    every day at 8am / daily 8am / every weekday 9am / every monday 8am   (repeat)
"""

import re
from datetime import date, datetime, time, timedelta
from enum import StrEnum

DEFAULT_TIME = time(9, 0)  # "tomorrow" without a time
MAX_AHEAD = timedelta(days=366 * 2)


class Repeat(StrEnum):
    NONE = "none"
    DAILY = "daily"
    WEEKDAYS = "weekdays"  # Monday-Friday
    WEEKLY = "weekly"


class WhenError(ValueError):
    """The time couldn't be understood - the message explains what works."""


HELP = (
    "Use a time like '5pm', '17:30', 'in 20 minutes', 'tomorrow 9am', 'friday 3pm', "
    "'2026-10-05 14:30' or 'every day at 8am'."
)

_UNITS = {
    "second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600,
    "day": 86400, "week": 604800, "month": 30 * 86400, "year": 365 * 86400,
}  # fmt: skip
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30}  # fmt: skip
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]  # fmt: skip
_NAMED_TIMES = {"noon": time(12), "midday": time(12), "midnight": time(0), "morning": time(9),
                "afternoon": time(15), "evening": time(18), "tonight": time(20),
                "night": time(21)}  # fmt: skip

_RELATIVE = re.compile(
    r"(\d+(?:\.\d+)?|" + "|".join(_NUMBER_WORDS) + r")\s*"
    r"(second|sec|minute|min|hour|hr|day|week|month|year)s?\b"
)
_CLOCK = re.compile(r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?![\d:])")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")


def parse_when(text: str, now: datetime) -> tuple[datetime, Repeat]:
    """Return (moment, repeat). `now` must be timezone-aware; the result uses its zone."""
    original = text
    text = " ".join(text.lower().replace(",", " ").split())
    if not text:
        raise WhenError(HELP)

    try:  # exact timestamps like 2026-10-05T14:30:00 (what some models send)
        exact = datetime.fromisoformat(original.strip())
        if "t" in text or ":" in text:
            return _checked(exact if exact.tzinfo else exact.replace(tzinfo=now.tzinfo), now), (
                Repeat.NONE
            )
    except ValueError:
        pass

    repeat, text = _take_repeat(text)

    if re.match(r"^(in|after)\b", text) or "from now" in text:
        amount = re.sub(r"\bhalf (an )?hour\b", "30 minutes", text)
        seconds = sum(_number(n) * _UNITS[unit] for n, unit in _RELATIVE.findall(amount))
        if seconds > 0:  # (not "in the morning" - that is handled below)
            return _checked(now + timedelta(seconds=seconds), now), repeat

    day, text = _take_day(text, now)
    clock, explicit_half = _take_time(text)

    if day is None and clock is None:
        raise WhenError(f"I couldn't understand '{original}'. {HELP}")
    if clock is None:
        clock = DEFAULT_TIME
    if day is not None:
        moment = datetime.combine(day, clock, tzinfo=now.tzinfo)
        if moment <= now and day == now.date() and not explicit_half and clock.hour < 12:
            moment += timedelta(hours=12)  # "today at 5" said in the afternoon means 5 pm
        if moment <= now and repeat != Repeat.NONE:
            moment = next_occurrence(moment, repeat, now)
        return _checked(moment, now), repeat

    # Only a time: the next moment the clock shows it.
    candidates = [datetime.combine(now.date(), clock, tzinfo=now.tzinfo)]
    if not explicit_half and clock.hour < 12:  # "at 5": could be 5:00 or 17:00
        candidates.append(candidates[0] + timedelta(hours=12))
    candidates += [c + timedelta(days=1) for c in candidates]
    moment = min(c for c in candidates if c > now)
    return _checked(moment, now), repeat


def next_occurrence(moment: datetime, repeat: Repeat, now: datetime) -> datetime:
    """The next time a repeating reminder is due, strictly after `now` (same clock time)."""
    step = timedelta(days=7 if repeat == Repeat.WEEKLY else 1)
    wall = moment.replace(tzinfo=None)  # step in local clock time (keeps 8:00 across DST)
    limit = now.replace(tzinfo=None)
    while wall <= limit or (repeat == Repeat.WEEKDAYS and wall.weekday() >= 5):
        wall += step
    return wall.replace(tzinfo=moment.tzinfo)


def describe(moment: datetime, now: datetime) -> str:
    """ "today at 5:00 pm (in 6 h 32 min)" - what ARTHUR says back to the user."""
    local = moment.astimezone(now.tzinfo)
    days = (local.date() - now.date()).days
    day = {0: "today", 1: "tomorrow"}.get(days) or local.strftime("%A %d %B %Y").replace(" 0", " ")
    clock = local.strftime("%I:%M %p").lstrip("0").lower()
    return f"{day} at {clock} ({_until(local - now)})"


def _until(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "overdue"
    if seconds < 90:
        return f"in {seconds} s"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"in {minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"in {hours} h {minutes} min" if minutes else f"in {hours} h"
    return f"in {round(hours / 24)} days"


def _checked(moment: datetime, now: datetime) -> datetime:
    if moment <= now:
        raise WhenError("That time is already in the past.")
    if moment - now > MAX_AHEAD:
        raise WhenError("That is more than two years away.")
    return moment.replace(microsecond=0)


def _number(text: str) -> float:
    return _NUMBER_WORDS[text] if text in _NUMBER_WORDS else float(text)


def _take_repeat(text: str) -> tuple[Repeat, str]:
    if re.search(r"\b(every ?day|daily|each day|every (morning|evening|night))\b", text):
        text = re.sub(r"\b(every ?day|daily|each day)\b", " ", text)
        return Repeat.DAILY, re.sub(r"\bevery (?=morning|evening|night)", "", text).strip()
    if re.search(r"\b(every|each) (weekday|week ?day|working day)s?\b", text):
        weekdays = r"\b(every|each) (weekday|week ?day|working day)s?\b"
        return Repeat.WEEKDAYS, re.sub(weekdays, " ", text)
    if re.search(r"\b(every|each) week\b|\bweekly\b", text):
        return Repeat.WEEKLY, re.sub(r"\b(every|each) week\b|\bweekly\b", " ", text)
    if re.search(r"\b(every|each) (" + "|".join(_WEEKDAYS) + r")s?\b", text):
        return Repeat.WEEKLY, re.sub(r"\b(every|each) ", "", text)
    return Repeat.NONE, text


def _take_day(text: str, now: datetime) -> tuple[date | None, str]:
    """Find a day in the text; return (date or None, text without it)."""
    today = now.date()
    if match := _ISO_DATE.search(text):
        try:
            day = today.replace(*map(int, match.groups()))
        except ValueError as exc:
            raise WhenError("That date doesn't exist.") from exc
        return day, text.replace(match.group(0), " ")
    if "day after tomorrow" in text:
        return today + timedelta(days=2), text.replace("day after tomorrow", " ")
    for word, offset in (("tomorrow", 1), ("tmrw", 1), ("today", 0), ("tonight", 0)):
        if re.search(rf"\b{word}\b", text):
            keep = " tonight " if word == "tonight" else " "  # "tonight" is also a time
            return today + timedelta(days=offset), re.sub(rf"\b{word}\b", keep, text)
    for index, name in enumerate(_WEEKDAYS):
        if match := re.search(rf"\b(next )?({name}|{name[:3]})s?\b", text):
            rest = text.replace(match.group(0), " ")
            ahead = (index - today.weekday()) % 7
            if ahead == 0 and _clock_passed(rest, now):
                ahead = 7  # "friday 3pm" said on Friday at 4 pm means next week
            return today + timedelta(days=ahead), rest
    for index, name in enumerate(_MONTHS, start=1):
        month = rf"(?:{name}|{name[:3]})"
        number = r"(\d{1,2})(?:st|nd|rd|th)?"
        pattern = rf"\b(?:{number} (?:of )?{month}|{month} {number})\b"  # "5 oct" or "oct 5"
        if match := re.search(pattern, text):
            try:
                day = today.replace(month=index, day=int(match.group(1) or match.group(2)))
                if day < today:
                    day = day.replace(year=today.year + 1)
            except ValueError as exc:
                raise WhenError("That date doesn't exist.") from exc
            return day, text.replace(match.group(0), " ")
    return None, text


def _clock_passed(text: str, now: datetime) -> bool:
    clock, _ = _take_time(text)
    return (clock or DEFAULT_TIME) <= now.time()


def _take_time(text: str) -> tuple[time | None, bool]:
    """Find a clock time; the bool says whether am/pm (or 24-hour) was explicit."""
    for name, value in _NAMED_TIMES.items():
        if re.search(rf"\b{name}\b", text) and not _CLOCK.search(text):
            return value, True
    match = _CLOCK.search(text)
    if not match:
        return None, False
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    half = (match.group(3) or "").replace(".", "")
    if not half and re.search(r"\b(evening|tonight|night|afternoon)\b", text) and hour < 12:
        half = "pm"
    elif not half and re.search(r"\bmorning\b", text) and hour <= 12:
        half = "am"
    if half:
        if not 1 <= hour <= 12:
            raise WhenError("Hours with am/pm go from 1 to 12.")
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        raise WhenError("That isn't a valid clock time.")
    return time(hour, minute), bool(half) or hour >= 13 or hour == 0
