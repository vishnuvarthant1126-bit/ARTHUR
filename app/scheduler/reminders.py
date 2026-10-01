"""Reminders stored in SQLite, so they survive a restart.

A reminder only ever TELLS the user something. It never runs a tool by itself -
an unattended timer must not be able to act on the computer.

Times: the database holds UTC; `clock()` gives "now" in the computer's own time
zone, which is what "5 pm" means to the user.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import func, select

from app.database.database import Database
from app.database.models import ReminderRecord
from app.memory.policy import contains_sensitive_data
from app.scheduler.when import Repeat, WhenError, describe, next_occurrence, parse_when

MAX_TEXT_CHARS = 300
MAX_ACTIVE = 100


class ReminderError(Exception):
    """A problem that is safe to show the user."""


class Reminder(BaseModel):
    id: int
    text: str
    due_at: datetime  # timezone-aware
    repeat: Repeat
    status: str
    created_at: datetime
    delivered_at: datetime | None = None


def local_now() -> datetime:
    return datetime.now().astimezone()


class ReminderService:
    """Synchronous (callers run it in a thread), like the other database code."""

    def __init__(self, db: Database, clock: Callable[[], datetime] = local_now) -> None:
        self.db = db
        self.clock = clock  # tests pass a fake clock

    def create(self, text: str, when: str, repeat: Repeat | None = None) -> Reminder:
        """`when` is the user's own wording ("5pm", "in 20 minutes", "every day at 8")."""
        text = " ".join(text.split())
        if not text:
            raise ReminderError("What should the reminder say?")
        if len(text) > MAX_TEXT_CHARS:
            raise ReminderError(f"Keep the reminder under {MAX_TEXT_CHARS} characters.")
        if contains_sensitive_data(text):
            raise ReminderError("Don't put passwords, card numbers or other secrets in reminders.")
        try:
            due_at, parsed_repeat = parse_when(when, self.clock())
        except WhenError as exc:
            raise ReminderError(str(exc)) from exc
        if parsed_repeat == Repeat.NONE and repeat:
            parsed_repeat = repeat
        with self.db.session() as s:
            active = s.scalar(select(func.count()).select_from(ReminderRecord).where(_scheduled()))
            if active >= MAX_ACTIVE:
                raise ReminderError(f"There are already {MAX_ACTIVE} reminders. Cancel some first.")
            record = ReminderRecord(
                text=text, due_at=due_at.astimezone(UTC), repeat=parsed_repeat.value
            )
            s.add(record)
            s.flush()
            return _reminder(record)

    def get(self, reminder_id: int) -> Reminder | None:
        with self.db.session() as s:
            record = s.get(ReminderRecord, reminder_id)
            return _reminder(record) if record else None

    def upcoming(self, limit: int = 50) -> list[Reminder]:
        with self.db.session() as s:
            query = select(ReminderRecord).where(_scheduled()).order_by(ReminderRecord.due_at)
            return [_reminder(r) for r in s.scalars(query.limit(limit))]

    def recent(self, limit: int = 5) -> list[Reminder]:
        """Reminders that were already delivered, newest first."""
        with self.db.session() as s:
            query = (
                select(ReminderRecord)
                .where(ReminderRecord.status == "delivered")
                .order_by(ReminderRecord.delivered_at.desc())
            )
            return [_reminder(r) for r in s.scalars(query.limit(limit))]

    def due(self) -> list[Reminder]:
        now = self.clock().astimezone(UTC)
        with self.db.session() as s:
            query = (
                select(ReminderRecord)
                .where(_scheduled(), ReminderRecord.due_at <= now)
                .order_by(ReminderRecord.due_at)
            )
            return [_reminder(r) for r in s.scalars(query)]

    def mark_delivered(self, reminder_id: int) -> Reminder | None:
        """One-time: finished. Repeating: moved to its next time (never a pile of old ones)."""
        now = self.clock()
        with self.db.session() as s:
            record = s.get(ReminderRecord, reminder_id)
            if record is None or record.status != "scheduled":
                return None
            record.delivered_at = now.astimezone(UTC)
            repeat = Repeat(record.repeat)
            if repeat == Repeat.NONE:
                record.status = "delivered"
            else:
                due_local = _aware(record.due_at).astimezone(now.tzinfo)
                record.due_at = next_occurrence(due_local, repeat, now).astimezone(UTC)
            s.flush()
            return _reminder(record)

    def cancel(self, reminder_id: int) -> bool:
        with self.db.session() as s:
            record = s.get(ReminderRecord, reminder_id)
            if record is None or record.status != "scheduled":
                return False
            record.status = "cancelled"
            return True

    def describe(self, reminder: Reminder) -> str:
        """ "today at 5:00 pm (in 6 h 30 min)", plus the repeat if there is one."""
        text = describe(reminder.due_at, self.clock())
        if reminder.repeat != Repeat.NONE:
            text += f", repeats {reminder.repeat.value}"
        return text


def _scheduled():
    return ReminderRecord.status == "scheduled"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)  # SQLite returns naive UTC


def _reminder(record: ReminderRecord) -> Reminder:
    return Reminder(
        id=record.id,
        text=record.text,
        due_at=_aware(record.due_at),
        repeat=Repeat(record.repeat),
        status=record.status,
        created_at=_aware(record.created_at),
        delivered_at=_aware(record.delivered_at) if record.delivered_at else None,
    )
