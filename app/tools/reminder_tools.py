"""Reminder tools (Phase 18).

    set_reminder     level 1  runs (easy to undo)
    list_reminders   level 0
    cancel_reminder  level 2  asks first

The model passes the user's time wording unchanged; Python works out the moment.
"""

import asyncio
from collections.abc import Callable

from pydantic import BaseModel, Field

from app.scheduler.reminders import Reminder, ReminderError, ReminderService
from app.scheduler.when import Repeat
from app.tools.base import PermissionLevel, Tool, ToolContext, ToolError


def _view(service: ReminderService, reminder: Reminder) -> dict:
    return {"id": reminder.id, "text": reminder.text, "due": service.describe(reminder)}


class SetReminderInput(BaseModel):
    text: str = Field(min_length=1, max_length=300, description="What to remind the user of")
    when: str = Field(
        min_length=1,
        max_length=100,
        description="The time in the user's OWN words - do not convert it: '5pm', "
        "'in 20 minutes', 'tomorrow 9am', 'friday 3pm', 'every day at 8am'",
    )
    repeat: Repeat = Field(default=Repeat.NONE, description="Only if the user wants it repeated")


class SetReminderTool(Tool[SetReminderInput]):
    name = "set_reminder"
    description = (
        "Set a reminder ('remind me at 5pm to call mum', 'in 20 minutes', 'every Monday 8am'). "
        "Pass the user's time wording as it is; the system calculates the exact time and "
        "tells you. The reminder appears in the chat when it is due."
    )
    input_model = SetReminderInput
    permission_level = PermissionLevel.LOW_RISK

    def __init__(self, service: ReminderService, on_change: Callable[[], None] = lambda: None):
        self.service = service
        self.on_change = on_change  # lets the scheduler check immediately

    async def run(self, args: SetReminderInput, context: ToolContext) -> dict:
        try:
            reminder = await asyncio.to_thread(
                self.service.create, args.text, args.when, args.repeat
            )
        except ReminderError as exc:
            raise ToolError(str(exc)) from exc
        self.on_change()
        return {"saved": True, **_view(self.service, reminder)}

    def summarize(self, output: dict) -> str:
        return f"{output['text']} - {output['due']}"


class ListRemindersInput(BaseModel):
    pass


class ListRemindersTool(Tool[ListRemindersInput]):
    name = "list_reminders"
    description = (
        "List the user's upcoming reminders and the last few delivered ones. "
        "Use it when the user asks what is scheduled."
    )
    input_model = ListRemindersInput
    permission_level = PermissionLevel.READ_ONLY

    def __init__(self, service: ReminderService) -> None:
        self.service = service

    async def run(self, args: ListRemindersInput, context: ToolContext) -> dict:
        upcoming = await asyncio.to_thread(self.service.upcoming)
        recent = await asyncio.to_thread(self.service.recent)
        return {
            "upcoming": [f"#{r.id} {r.text} - {self.service.describe(r)}" for r in upcoming],
            "recently_delivered": [r.text for r in recent],
        }

    def summarize(self, output: dict) -> str:
        return f"{len(output['upcoming'])} upcoming"


class CancelReminderInput(BaseModel):
    reminder: str = Field(
        min_length=1,
        max_length=100,
        description="Words from the reminder the user means ('email', 'call mum'), or its #id",
    )


_FILLER = {"the", "my", "a", "an", "one", "reminder", "reminders", "to", "about", "for", "that"}


class CancelReminderTool(Tool[CancelReminderInput]):
    name = "cancel_reminder"
    description = (
        "Cancel one upcoming reminder. Pass the user's words for it ('the email one' -> "
        "'email'); the system finds the matching reminder and asks the user to confirm."
    )
    input_model = CancelReminderInput
    permission_level = PermissionLevel.CONFIRM

    def __init__(self, service: ReminderService) -> None:
        self.service = service

    async def _find(self, wanted: str) -> Reminder:
        """Match by the reminder's WORDS - the model once mixed up list positions and ids."""
        upcoming = await asyncio.to_thread(self.service.upcoming)
        listing = "; ".join(f"#{r.id} {r.text}" for r in upcoming) or "none"
        wanted = wanted.strip().lower()
        if wanted.lstrip("#").isdigit():
            matches = [r for r in upcoming if r.id == int(wanted.lstrip("#"))]
        else:
            words = [w for w in wanted.replace(",", " ").split() if w not in _FILLER]
            matches = [r for r in upcoming if words and all(w in r.text.lower() for w in words)]
        if len(matches) == 1:
            return matches[0]
        problem = (
            "No upcoming reminder matches" if not matches else "More than one reminder matches"
        )
        raise ToolError(f"{problem} '{wanted}'. Upcoming reminders: {listing}")

    async def required_level(self, args: CancelReminderInput) -> PermissionLevel:
        await self._find(args.reminder)  # never ask about a reminder that isn't there
        return self.permission_level

    async def preview(self, args: CancelReminderInput) -> str:
        reminder = await self._find(args.reminder)
        return f'Cancel the reminder "{reminder.text}" ({self.service.describe(reminder)})'

    async def run(self, args: CancelReminderInput, context: ToolContext) -> dict:
        reminder = await self._find(args.reminder)
        if not await asyncio.to_thread(self.service.cancel, reminder.id):
            raise ToolError("That reminder is no longer upcoming.")
        return {"cancelled": reminder.text}
