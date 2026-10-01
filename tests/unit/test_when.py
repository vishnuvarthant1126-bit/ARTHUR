"""The time parser (Phase 18): plain Python turns "5 pm" into an exact moment."""

from datetime import datetime, timedelta, timezone

import pytest

from app.scheduler.when import Repeat, WhenError, describe, next_occurrence, parse_when

SGT = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 1, 10, 30, tzinfo=SGT)  # a Thursday morning


def at(day: int, hour: int, minute: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=SGT)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # relative
        ("in 20 minutes", at(1, 10, 50)),
        ("in 2 hours", at(1, 12, 30)),
        ("in 1 hour 30 minutes", at(1, 12)),
        ("in half an hour", at(1, 11)),
        ("in an hour", at(1, 11, 30)),
        ("in 3 days", at(4, 10, 30)),
        ("20 minutes from now", at(1, 10, 50)),
        # a time only: the next moment the clock shows it
        ("5pm", at(1, 17)),
        ("5 PM", at(1, 17)),
        ("at 5:30 pm", at(1, 17, 30)),
        ("17:00", at(1, 17)),
        ("9am", at(2, 9)),  # 9 am has passed -> tomorrow
        ("at 5", at(1, 17)),  # 5:00 has passed, 17:00 hasn't
        ("at 11", at(1, 11)),
        ("noon", at(1, 12)),
        ("midnight", at(2, 0)),
        ("5 p.m.", at(1, 17)),
        # days
        ("today 6pm", at(1, 18)),
        ("today at 5", at(1, 17)),  # afternoon is the only 5 o'clock left today
        ("tonight", at(1, 20)),
        ("tonight at 8", at(1, 20)),
        ("tomorrow", at(2, 9)),
        ("tomorrow 9am", at(2, 9)),
        ("tomorrow at 14:15", at(2, 14, 15)),
        ("tomorrow morning", at(2, 9)),
        ("tomorrow evening", at(2, 18)),
        ("in the evening", at(1, 18)),
        ("day after tomorrow 8am", at(3, 8)),
        ("friday 3pm", at(2, 15)),
        ("on monday at 8", at(5, 8)),
        ("next monday 8am", at(5, 8)),
        ("thursday 9am", at(8, 9)),  # today is Thursday and 9 am has passed
        ("thursday 3pm", at(1, 15)),
        # dates
        ("2026-10-05", at(5, 9)),
        ("2026-10-05 14:30", at(5, 14, 30)),
        ("2026-10-05T14:30:00", at(5, 14, 30)),
        ("5 october 9am", at(5, 9)),
        ("oct 5 at 6pm", at(5, 18)),
        ("3rd of november 10am", at(3, 10, month=11)),
    ],
)
def test_parse_when(text, expected):
    moment, repeat = parse_when(text, NOW)
    assert moment == expected
    assert repeat == Repeat.NONE


@pytest.mark.parametrize(
    ("text", "expected", "repeat"),
    [
        ("every day at 8am", at(2, 8), Repeat.DAILY),
        ("daily 6pm", at(1, 18), Repeat.DAILY),
        ("every morning", at(2, 9), Repeat.DAILY),
        ("every weekday 9am", at(2, 9), Repeat.WEEKDAYS),
        ("every monday 8am", at(5, 8), Repeat.WEEKLY),
        ("every week friday 5pm", at(2, 17), Repeat.WEEKLY),
    ],
)
def test_parse_repeating(text, expected, repeat):
    assert parse_when(text, NOW) == (expected, repeat)


@pytest.mark.parametrize(
    "text",
    ["", "soon", "when I get home", "today 9am", "2020-01-01 10:00", "25:00", "13pm", "in 5 years"],
)
def test_unclear_or_impossible_times_are_refused(text):
    with pytest.raises(WhenError):
        parse_when(text, NOW)


def test_next_occurrence():
    eight = at(1, 8)
    assert next_occurrence(eight, Repeat.DAILY, NOW) == at(2, 8)
    assert next_occurrence(eight, Repeat.WEEKLY, NOW) == at(8, 8)
    friday = at(2, 9)
    after_friday = at(2, 9, 1)
    assert next_occurrence(friday, Repeat.WEEKDAYS, after_friday) == at(5, 9)  # skips Sat, Sun
    # ARTHUR was off for a week: jump straight to the next future time, not 7 old ones.
    assert next_occurrence(eight, Repeat.DAILY, at(9, 12)) == at(10, 8)


def test_describe():
    assert describe(at(1, 17), NOW) == "today at 5:00 pm (in 6 h 30 min)"
    assert describe(at(1, 10, 50), NOW) == "today at 10:50 am (in 20 min)"
    assert describe(at(2, 9), NOW) == "tomorrow at 9:00 am (in 22 h 30 min)"
    assert describe(at(5, 8), NOW).startswith("Monday 5 October 2026 at 8:00 am (in 4 days")
