"""Reminders (Phase 18): storage, the scheduler loop, tools, API and WebSocket delivery.

A fake clock is moved forward by hand, so no test waits for real time.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.database.database import Database
from app.scheduler.reminders import MAX_ACTIVE, ReminderError, ReminderService
from app.scheduler.runner import NotificationHub, ReminderScheduler
from app.scheduler.when import Repeat
from app.security.permissions import PermissionPolicy
from app.tools.base import ToolContext
from app.tools.registry import ToolRegistry
from app.tools.reminder_tools import CancelReminderTool, ListRemindersTool, SetReminderTool

SGT = timezone(timedelta(hours=8))


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, 10, 30, tzinfo=SGT)  # a Thursday morning

    def __call__(self) -> datetime:
        return self.now

    def forward(self, **delta) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    database.create_tables()
    return database


@pytest.fixture
def service(db, clock) -> ReminderService:
    return ReminderService(db, clock)


class Tab:
    """Stands in for an open browser tab."""

    def __init__(self, broken: bool = False) -> None:
        self.received: list[dict] = []
        self.broken = broken

    async def __call__(self, message: dict) -> None:
        if self.broken:
            raise RuntimeError("tab closed")
        self.received.append(message)


# ---------- storage ----------


def test_create_and_list(service, clock):
    reminder = service.create("  call   mum ", "5pm")
    assert reminder.text == "call mum"
    assert reminder.due_at == datetime(2026, 10, 1, 17, 0, tzinfo=SGT)
    assert reminder.status == "scheduled"
    assert service.describe(reminder) == "today at 5:00 pm (in 6 h 30 min)"

    service.create("stretch", "in 20 minutes")
    assert [r.text for r in service.upcoming()] == ["stretch", "call mum"]  # soonest first
    assert service.due() == []


def test_reminders_survive_a_restart(tmp_path, clock):
    path = tmp_path / "arthur.db"
    first = Database(path)
    first.create_tables()
    ReminderService(first, clock).create("call mum", "5pm")
    first.close()  # ARTHUR is switched off

    second = Database(path)  # ...and started again
    second.create_tables()
    restarted = ReminderService(second, clock)
    assert [r.text for r in restarted.upcoming()] == ["call mum"]
    assert restarted.upcoming()[0].due_at == datetime(2026, 10, 1, 17, 0, tzinfo=SGT)
    second.close()


@pytest.mark.parametrize(
    ("text", "when", "message"),
    [
        ("", "5pm", "What should"),
        ("x" * 301, "5pm", "under 300"),
        ("my password is hunter2", "5pm", "secrets"),
        ("call mum", "soon", "couldn't understand"),
        ("call mum", "today 9am", "in the past"),
    ],
)
def test_bad_reminders_are_refused(service, text, when, message):
    with pytest.raises(ReminderError, match=message):
        service.create(text, when)
    assert service.upcoming() == []


def test_there_is_a_limit(service):
    for i in range(MAX_ACTIVE):
        service.create(f"thing {i}", "5pm")
    with pytest.raises(ReminderError, match="already 100"):
        service.create("one too many", "5pm")


def test_cancel(service):
    reminder = service.create("call mum", "5pm")
    assert service.cancel(reminder.id)
    assert not service.cancel(reminder.id)  # already cancelled
    assert not service.cancel(999)
    assert service.upcoming() == []


# ---------- the scheduler loop ----------


async def test_due_reminder_is_delivered_once(service, clock):
    hub, tab = NotificationHub(), Tab()
    hub.subscribe(tab)
    scheduler = ReminderScheduler(service, hub)
    service.create("call mum", "5pm")

    assert await scheduler.tick() == 0  # not yet
    clock.forward(hours=6, minutes=30)
    assert await scheduler.tick() == 1
    assert await scheduler.tick() == 0  # never twice

    message = tab.received[0]
    assert message["type"] == "reminder"
    assert message["text"] == "call mum"
    assert message["due"] == "today at 5:00 pm"
    assert message["late"] is False
    assert service.upcoming() == []
    assert [r.text for r in service.recent()] == ["call mum"]


async def test_nothing_is_lost_when_no_tab_is_open(service, clock):
    hub = NotificationHub()
    scheduler = ReminderScheduler(service, hub)
    service.create("call mum", "5pm")
    clock.forward(hours=9)  # 7:30 pm - ARTHUR had no open tab at 5 pm

    assert await scheduler.tick() == 0
    assert len(service.due()) == 1  # still waiting

    tab = Tab()
    hub.subscribe(tab)  # you open ARTHUR again
    assert await scheduler.tick() == 1
    assert tab.received[0]["late"] is True


async def test_a_closed_tab_does_not_count_as_delivered(service, clock):
    hub = NotificationHub()
    hub.subscribe(Tab(broken=True))
    scheduler = ReminderScheduler(service, hub)
    service.create("call mum", "in 1 minute")
    clock.forward(minutes=2)

    assert await scheduler.tick() == 0
    assert len(service.due()) == 1
    assert not hub.has_listeners  # the dead tab was dropped


async def test_repeating_reminder_moves_to_its_next_time(service, clock):
    hub, tab = NotificationHub(), Tab()
    hub.subscribe(tab)
    scheduler = ReminderScheduler(service, hub)
    reminder = service.create("take vitamins", "every day at 8am")
    assert reminder.repeat == Repeat.DAILY
    assert reminder.due_at == datetime(2026, 10, 2, 8, 0, tzinfo=SGT)

    clock.forward(days=5)  # ARTHUR was off for five days
    assert await scheduler.tick() == 1
    assert len(tab.received) == 1  # one reminder, not five
    upcoming = service.upcoming()
    assert upcoming[0].due_at == datetime(2026, 10, 7, 8, 0, tzinfo=SGT)
    assert "repeats daily" in service.describe(upcoming[0])


async def test_scheduler_loop_runs_and_stops(service, clock):
    hub, tab = NotificationHub(), Tab()
    hub.subscribe(tab)
    scheduler = ReminderScheduler(service, hub, interval_seconds=60)
    scheduler.start()
    service.create("stretch", "in 1 minute")
    clock.forward(minutes=2)
    scheduler.poke()  # don't wait for the 60 s interval
    for _ in range(50):
        if tab.received:
            break
        await asyncio.sleep(0.02)
    await scheduler.stop()
    assert [m["text"] for m in tab.received] == ["stretch"]


# ---------- tools ----------


@pytest.fixture
def registry(service) -> ToolRegistry:
    registry = ToolRegistry(PermissionPolicy(), None)
    registry.register(SetReminderTool(service))
    registry.register(ListRemindersTool(service))
    registry.register(CancelReminderTool(service))
    return registry


async def test_set_and_list_tools(registry):
    saved = await registry.execute("set_reminder", {"text": "call mum", "when": "5pm"})
    assert saved.status == "ok"
    assert saved.output["due"] == "today at 5:00 pm (in 6 h 30 min)"

    weekly = await registry.execute(
        "set_reminder", {"text": "gym", "when": "monday 7am", "repeat": "weekly"}
    )
    assert "repeats weekly" in weekly.output["due"]

    listed = await registry.execute("list_reminders", {})
    assert listed.output["upcoming"][0] == "#1 call mum - today at 5:00 pm (in 6 h 30 min)"
    assert listed.output["upcoming"][1].startswith("#2 gym - ")


async def test_set_reminder_explains_unclear_times(registry):
    result = await registry.execute("set_reminder", {"text": "call mum", "when": "later"})
    assert result.status == "error"
    assert "in 20 minutes" in result.error  # tells the model what works


async def test_cancel_finds_the_reminder_by_its_words(registry, service):
    """Seen live: asked to cancel "the email one", the model cancelled "call mum"
    because it mixed up list positions and ids. Now Python matches the words."""
    service.create("call mum", "5pm")
    service.create("check my email", "every weekday 8am")

    asked = await registry.execute("cancel_reminder", {"reminder": "the email one"})
    assert asked.status == "needs_confirmation"
    assert asked.preview.startswith('Cancel the reminder "check my email"')
    assert len(service.upcoming()) == 2  # nothing happens before the user's "yes"

    done = await registry.execute(
        "cancel_reminder", {"reminder": "the email one"}, ToolContext(confirmed=True)
    )
    assert done.output == {"cancelled": "check my email"}
    assert [r.text for r in service.upcoming()] == ["call mum"]


async def test_cancel_never_guesses(registry, service):
    first = service.create("call mum", "5pm")
    service.create("call the bank", "6pm")

    unclear = await registry.execute("cancel_reminder", {"reminder": "call"})
    assert unclear.status == "error"  # two match: no confirmation question, no guess
    assert "More than one" in unclear.error
    assert "call the bank" in unclear.error

    unknown = await registry.execute("cancel_reminder", {"reminder": "dentist"})
    assert unknown.status == "error"

    by_id = await registry.execute("cancel_reminder", {"reminder": f"#{first.id}"})
    assert by_id.status == "needs_confirmation"
    assert '"call mum"' in by_id.preview


# ---------- API ----------


async def test_reminder_api(client):
    headers = {"Origin": "http://test"}
    created = await client.post(
        "/reminders", json={"text": "call mum", "when": "in 2 hours"}, headers=headers
    )
    assert created.status_code == 201, created.text
    reminder_id = created.json()["id"]
    assert created.json()["due"].startswith(("today", "tomorrow"))

    listed = (await client.get("/reminders")).json()
    assert [r["text"] for r in listed["upcoming"]] == ["call mum"]

    bad = await client.post("/reminders", json={"text": "x", "when": "whenever"}, headers=headers)
    assert bad.status_code == 422

    assert (await client.delete(f"/reminders/{reminder_id}", headers=headers)).status_code == 204
    assert (await client.delete(f"/reminders/{reminder_id}", headers=headers)).status_code == 404


# ---------- WebSocket ----------


def test_open_tab_receives_reminders(ws_client):
    state = ws_client.app.state
    state.reminders.clock = lambda: datetime(2026, 10, 1, 10, 30, tzinfo=SGT)
    state.reminders.create("call mum", "in 1 minute")
    state.reminders.clock = lambda: datetime(2026, 10, 1, 10, 35, tzinfo=SGT)

    with ws_client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "session"
        assert state.hub.has_listeners
        ws.portal.call(state.scheduler.tick)
        pushed = ws.receive_json()
    assert pushed["type"] == "reminder"
    assert pushed["text"] == "call mum"
    assert pushed["late"] is True
