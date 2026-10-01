"""Reminder endpoints: see, add and cancel reminders (used by the Reminders panel).

Like memories, reminders are fully visible to the user and can always be removed.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.scheduler.reminders import Reminder, ReminderError, ReminderService

router = APIRouter(prefix="/reminders", tags=["reminders"])


class ReminderIn(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    when: str = Field(min_length=1, max_length=100, examples=["5pm", "in 20 minutes"])


class ReminderOut(Reminder):
    due: str  # "today at 5:00 pm (in 6 h 30 min)"


class ReminderList(BaseModel):
    upcoming: list[ReminderOut]
    recent: list[ReminderOut]


def _service(request: Request) -> ReminderService:
    return request.app.state.reminders


def _out(service: ReminderService, reminder: Reminder) -> ReminderOut:
    return ReminderOut(**reminder.model_dump(), due=service.describe(reminder))


@router.get("", response_model=ReminderList)
async def list_reminders(request: Request) -> ReminderList:
    service = _service(request)
    upcoming = await asyncio.to_thread(service.upcoming)
    recent = await asyncio.to_thread(service.recent)
    return ReminderList(
        upcoming=[_out(service, r) for r in upcoming], recent=[_out(service, r) for r in recent]
    )


@router.post("", response_model=ReminderOut, status_code=status.HTTP_201_CREATED)
async def add_reminder(body: ReminderIn, request: Request) -> ReminderOut:
    service = _service(request)
    try:
        reminder = await asyncio.to_thread(service.create, body.text, body.when)
    except ReminderError as exc:
        raise HTTPException(422, str(exc)) from exc
    request.app.state.scheduler.poke()
    return _out(service, reminder)


@router.delete("/{reminder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_reminder(reminder_id: int, request: Request) -> Response:
    if not await asyncio.to_thread(_service(request).cancel, reminder_id):
        raise HTTPException(404, "No upcoming reminder with that id.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
