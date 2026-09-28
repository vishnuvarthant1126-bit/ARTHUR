"""Current date/time tool. LLMs don't know what time it is - they have no clock."""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from app.tools.base import PermissionLevel, Tool, ToolContext, ToolError


class TimeInput(BaseModel):
    timezone: str | None = Field(
        default=None,
        max_length=64,
        description="IANA time zone like 'Asia/Singapore' or 'Europe/London'. "
        "Omit for the user's local time.",
    )


class CurrentTimeTool(Tool[TimeInput]):
    name = "current_time"
    description = "Get the current date, time and weekday, optionally in a specific time zone."
    input_model = TimeInput
    permission_level = PermissionLevel.READ_ONLY
    timeout_seconds = 2.0

    async def run(self, args: TimeInput, context: ToolContext) -> dict:
        if args.timezone:
            try:
                now = datetime.now(ZoneInfo(args.timezone))
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ToolError(
                    f"Unknown time zone '{args.timezone}'. Use a name like 'Asia/Singapore'."
                ) from exc
            zone = args.timezone
        else:
            now = datetime.now().astimezone()  # the computer's local time zone
            zone = now.tzname() or "local"
        return {
            "timezone": zone,
            "iso": now.isoformat(timespec="seconds"),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"),
            "weekday": now.strftime("%A"),
            "utc_offset": now.strftime("%z"),
        }

    def summarize(self, output: dict) -> str:
        return f"{output['weekday']} {output['date']} {output['time']} ({output['timezone']})"
