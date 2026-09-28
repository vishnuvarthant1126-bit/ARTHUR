"""Phase 6: tool interface, registry, permissions, audit log and built-in tools."""

import asyncio

import httpx
import pytest
from pydantic import BaseModel

from app.database.database import Database
from app.security.audit import AuditLog, redact
from app.security.permissions import PermissionPolicy
from app.tools.base import PermissionLevel, Tool, ToolContext, ToolError
from app.tools.calculator import evaluate
from app.tools.registry import ToolRegistry
from app.tools.time_tool import CurrentTimeTool, TimeInput
from app.tools.weather import WeatherInput, WeatherTool

# ---------- calculator ----------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("482 * 29", 13978),
        ("482 × 29", 13978),
        ("482 x 29", 13978),
        ("25 * 50", 1250),
        ("2^10", 1024),
        ("(1 + 2) * 3", 9),
        ("10 / 4", 2.5),
        ("0.1 + 0.2", 0.3),
        ("sqrt(16) + abs(-2)", 6.0),
        ("round(pi, 2)", 3.14),
        ("-5 + +2", -3),
        ("15% of 2480", 372.0),
        ("12.5 % of 80", 10.0),
        ("10 % 3", 1),  # still the modulo operator
        ("2,480 * 2", 4960),
        ("round(1,2)", 1),  # a comma between arguments is not a thousands separator
    ],
)
def test_calculator_evaluates(expression, expected):
    assert evaluate(expression) == expected


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("__import__('os').system('dir')", "Unsupported"),
        ("open('secrets.txt')", "Unsupported"),
        ("(1).__class__", "Unsupported"),
        ("x + 1", "Unsupported"),
        ("1 / 0", "Division by zero"),
        ("9 ** 9 ** 9", "too large"),
        ("factorial(100000)", "limited"),
        ("2 +", "Not a valid"),
        ("1" * 201, "too long"),
    ],
)
def test_calculator_rejects_unsafe_or_invalid(expression, message):
    with pytest.raises(ToolError, match=message):
        evaluate(expression)


# ---------- time ----------


async def test_time_tool_with_timezone():
    result = await CurrentTimeTool().run(TimeInput(timezone="Asia/Singapore"), ToolContext())
    assert result["timezone"] == "Asia/Singapore"
    assert result["utc_offset"] == "+0800"


async def test_time_tool_rejects_unknown_timezone():
    with pytest.raises(ToolError, match="Unknown time zone"):
        await CurrentTimeTool().run(TimeInput(timezone="Mars/Olympus"), ToolContext())


# ---------- weather (mocked HTTP) ----------


def weather_client(geocode_results: list) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding" in request.url.host:
            return httpx.Response(200, json={"results": geocode_results})
        return httpx.Response(
            200,
            json={
                "current": {
                    "time": "2026-09-26T14:00",
                    "temperature_2m": 31.2,
                    "apparent_temperature": 36.0,
                    "precipitation": 0.4,
                    "weather_code": 61,
                    "wind_speed_10m": 12.0,
                },
                "daily": {
                    "temperature_2m_max": [32.0],
                    "temperature_2m_min": [26.0],
                    "precipitation_probability_max": [80],
                },
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_weather_tool_combines_geocoding_and_forecast():
    client = weather_client(
        [{"name": "Singapore", "country": "Singapore", "latitude": 1.29, "longitude": 103.85}]
    )

    result = await WeatherTool(client).run(WeatherInput(location="Singapore"), ToolContext())

    assert result["location"] == "Singapore, Singapore"
    assert result["conditions"] == "light rain"
    assert result["temperature_c"] == 31.2
    assert result["today_rain_chance_percent"] == 80


async def test_weather_tool_unknown_place():
    with pytest.raises(ToolError, match="Couldn't find"):
        await WeatherTool(weather_client([])).run(WeatherInput(location="Atlantis"), ToolContext())


async def test_weather_tool_offline():
    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ToolError, match="unreachable"):
        await WeatherTool(client).run(WeatherInput(location="Singapore"), ToolContext())


# ---------- registry, permissions, audit ----------


class EchoInput(BaseModel):
    text: str


def make_tool(tool_name: str, level: PermissionLevel, behaviour=None):
    class _Tool(Tool[EchoInput]):
        name = tool_name
        description = "test tool"
        input_model = EchoInput
        permission_level = level
        timeout_seconds = 0.2

        async def run(self, args, context):
            if behaviour:
                return await behaviour(args)
            return {"echo": args.text}

    return _Tool()


@pytest.fixture
def audit() -> AuditLog:
    db = Database(":memory:")
    db.create_tables()
    return AuditLog(db)


def registry_with(*tools, audit=None, policy=None) -> ToolRegistry:
    registry = ToolRegistry(policy or PermissionPolicy(), audit)
    for tool in tools:
        registry.register(tool)
    return registry


async def test_read_only_tool_runs(audit):
    registry = registry_with(make_tool("echo", PermissionLevel.READ_ONLY), audit=audit)

    result = await registry.execute("echo", {"text": "hi"})

    assert result.status == "ok"
    assert result.output == {"echo": "hi"}
    [entry] = audit.recent()
    assert (entry.tool, entry.decision, entry.success) == ("echo", "allowed", True)


async def test_unknown_tool_is_an_error(audit):
    result = await registry_with(audit=audit).execute("rm_rf", {})
    assert result.status == "error"
    assert "Unknown tool" in result.error
    assert audit.recent()[0].decision == "denied"


async def test_invalid_arguments_are_rejected_before_running():
    ran = []

    async def behaviour(args):
        ran.append(args)

    registry = registry_with(make_tool("echo", PermissionLevel.READ_ONLY, behaviour))

    result = await registry.execute("echo", {"text": 123, "extra": True})

    assert result.status == "error"
    assert "Invalid arguments" in result.error
    assert ran == []


async def test_level_2_needs_confirmation_then_runs(audit):
    registry = registry_with(make_tool("delete_thing", PermissionLevel.CONFIRM), audit=audit)

    first = await registry.execute("delete_thing", {"text": "report.pdf"})
    assert first.status == "needs_confirmation"
    assert "report.pdf" in first.preview

    second = await registry.execute(
        "delete_thing", {"text": "report.pdf"}, ToolContext(confirmed=True)
    )
    assert second.status == "ok"
    assert [e.decision for e in audit.recent()] == ["allowed", "needs_confirmation"]


async def test_level_3_is_always_denied_even_if_confirmed():
    registry = registry_with(make_tool("transfer_money", PermissionLevel.SENSITIVE))

    result = await registry.execute("transfer_money", {"text": "$500"}, ToolContext(confirmed=True))

    assert result.status == "denied"
    assert "never performs" in result.error


async def test_confirmation_cannot_be_configured_away():
    policy = PermissionPolicy(auto_approve_max_level=3)
    registry = registry_with(make_tool("delete_thing", PermissionLevel.CONFIRM), policy=policy)

    result = await registry.execute("delete_thing", {"text": "x"})

    assert result.status == "needs_confirmation"


async def test_blocked_tool_is_denied_and_hidden_from_llm():
    policy = PermissionPolicy(blocked_tools={"echo"})
    registry = registry_with(make_tool("echo", PermissionLevel.READ_ONLY), policy=policy)

    result = await registry.execute("echo", {"text": "hi"})

    assert result.status == "denied"
    assert registry.llm_schemas() == []


async def test_slow_tool_times_out():
    async def slow(args):
        await asyncio.sleep(5)

    registry = registry_with(make_tool("slow", PermissionLevel.READ_ONLY, slow))

    result = await registry.execute("slow", {"text": "x"})

    assert result.status == "error"
    assert "timed out" in result.error


async def test_crashing_tool_does_not_crash_arthur():
    async def crash(args):
        raise KeyError("internal detail")

    registry = registry_with(make_tool("buggy", PermissionLevel.READ_ONLY, crash))

    result = await registry.execute("buggy", {"text": "x"})

    assert result.status == "error"
    assert result.error == "Tool 'buggy' failed unexpectedly."


def test_duplicate_registration_is_rejected():
    registry = registry_with(make_tool("echo", PermissionLevel.READ_ONLY))
    with pytest.raises(ValueError):
        registry.register(make_tool("echo", PermissionLevel.READ_ONLY))


def test_llm_schema_format():
    schema = make_tool("echo", PermissionLevel.READ_ONLY).llm_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "echo"
    assert schema["function"]["parameters"]["properties"]["text"]["type"] == "string"


def test_audit_redacts_secrets():
    assert redact({"user": "v", "password": "hunter2", "nested": {"api_key": "sk"}}) == {
        "user": "v",
        "password": "***",
        "nested": {"api_key": "***"},
    }


# ---------- API ----------


async def test_tools_api_lists_builtin_tools(client):
    names = {t["name"] for t in (await client.get("/tools")).json()}
    assert {"calculator", "current_time", "weather", "search_memory", "save_memory"} <= names


async def test_tools_api_runs_calculator_and_audits(client):
    response = await client.post(
        "/tools/calculator/run", json={"arguments": {"expression": "482 * 29"}}
    )

    assert response.json()["status"] == "ok"
    assert response.json()["output"]["result"] == 13978
    audit = (await client.get("/audit")).json()
    assert audit[0]["tool"] == "calculator"


async def test_tools_api_delete_memory_requires_confirmation(client):
    saved = (await client.post("/memories", json={"content": "The user likes tea."})).json()

    first = await client.post(
        "/tools/delete_memory/run", json={"arguments": {"memory_id": saved["id"]}}
    )
    assert first.json()["status"] == "needs_confirmation"
    assert (await client.get("/memories")).json()["count"] == 1

    second = await client.post(
        "/tools/delete_memory/run",
        json={"arguments": {"memory_id": saved["id"]}, "confirmed": True},
    )
    assert second.json()["status"] == "ok"
    assert (await client.get("/memories")).json()["count"] == 0


async def test_tools_api_unknown_tool_404(client):
    assert (await client.post("/tools/nope/run", json={})).status_code == 404
