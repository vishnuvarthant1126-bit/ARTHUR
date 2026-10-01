"""The scheduler loop: every few seconds, deliver the reminders that are due.

    ReminderScheduler --(due reminders)--> NotificationHub --(push)--> open browser tabs

A reminder counts as delivered only when at least one tab received it. If no tab
is open (or ARTHUR was switched off), it stays due and is delivered - marked
"late" - as soon as a tab connects. Nothing is silently lost.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from app.observability.logging import get_logger
from app.scheduler.reminders import Reminder, ReminderService
from app.scheduler.when import describe

log = get_logger(__name__)

Listener = Callable[[dict], Awaitable[None]]
LATE_AFTER_SECONDS = 120


class NotificationHub:
    """The open browser tabs that want to hear about reminders."""

    def __init__(self) -> None:
        self._listeners: set[Listener] = set()

    def subscribe(self, listener: Listener) -> None:
        self._listeners.add(listener)

    def unsubscribe(self, listener: Listener) -> None:
        self._listeners.discard(listener)

    @property
    def has_listeners(self) -> bool:
        return bool(self._listeners)

    async def broadcast(self, message: dict) -> int:
        """Send to every tab; returns how many were reached."""
        reached = 0
        for listener in list(self._listeners):
            try:
                await listener(message)
                reached += 1
            except Exception:  # a closed tab must not stop the others
                self._listeners.discard(listener)
        return reached


class ReminderScheduler:
    def __init__(
        self, service: ReminderService, hub: NotificationHub, *, interval_seconds: float = 5.0
    ) -> None:
        self.service = service
        self.hub = hub
        self.interval_seconds = interval_seconds
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="arthur-reminders")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def poke(self) -> None:
        """Check right now (a tab just connected, or a reminder was just created)."""
        self._wake.set()

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # one bad tick must never kill the scheduler
                log.exception("reminder_tick_failed")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_seconds)

    async def tick(self) -> int:
        """Deliver everything that is due. Returns how many reminders were delivered."""
        if not self.hub.has_listeners:
            return 0  # nobody to tell - keep them due
        delivered = 0
        for reminder in await asyncio.to_thread(self.service.due):
            if await self.hub.broadcast(self._message(reminder)):
                await asyncio.to_thread(self.service.mark_delivered, reminder.id)
                delivered += 1
                log.info("reminder_delivered", id=reminder.id, repeat=reminder.repeat.value)
        return delivered

    def _message(self, reminder: Reminder) -> dict:
        now = self.service.clock()
        late_seconds = (now - reminder.due_at).total_seconds()
        local = reminder.due_at.astimezone(now.tzinfo)
        return {
            "type": "reminder",
            "id": reminder.id,
            "text": reminder.text,
            "due_at": local.isoformat(timespec="minutes"),
            "due": describe(reminder.due_at, now).split(" (")[0],  # "today at 5:00 pm"
            "late": late_seconds > LATE_AFTER_SECONDS,
            "repeat": reminder.repeat.value,
        }
