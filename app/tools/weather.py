"""Weather tool using Open-Meteo - free, no API key, no account.

Two HTTP calls: place name -> coordinates (geocoding), then coordinates ->
current weather + today's forecast.
"""

import httpx
from pydantic import BaseModel, Field

from app.tools.base import PermissionLevel, Tool, ToolContext, ToolError

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather codes -> words (subset covering all code groups)
_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow",
    75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}  # fmt: skip


class WeatherInput(BaseModel):
    location: str = Field(min_length=2, max_length=100, description="City name, e.g. 'Singapore'")


class WeatherTool(Tool[WeatherInput]):
    name = "weather"
    description = (
        "Get current weather and today's forecast (temperature, rain chance, wind) for a city."
    )
    input_model = WeatherInput
    permission_level = PermissionLevel.READ_ONLY
    timeout_seconds = 15.0

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def run(self, args: WeatherInput, context: ToolContext) -> dict:
        place = await self._geocode(args.location)
        data = await self._get(
            FORECAST_URL,
            {
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,precipitation,"
                "weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": 1,
            },
        )
        current, daily = data.get("current", {}), data.get("daily", {})
        return {
            "location": ", ".join(
                p for p in (place.get("name"), place.get("admin1"), place.get("country")) if p
            ),
            "time": current.get("time"),
            "conditions": _CODES.get(current.get("weather_code"), "unknown"),
            "temperature_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "precipitation_mm": current.get("precipitation"),
            "wind_kmh": current.get("wind_speed_10m"),
            "today_max_c": _first(daily.get("temperature_2m_max")),
            "today_min_c": _first(daily.get("temperature_2m_min")),
            "today_rain_chance_percent": _first(daily.get("precipitation_probability_max")),
            "source": "Open-Meteo (open-meteo.com)",
        }

    def summarize(self, output: dict) -> str:
        parts = [output.get("conditions"), f"{output.get('temperature_c')} °C"]
        if output.get("today_rain_chance_percent") is not None:
            parts.append(f"{output['today_rain_chance_percent']}% rain chance today")
        return f"{output.get('location')}: " + ", ".join(p for p in parts if p)

    async def _geocode(self, location: str) -> dict:
        data = await self._get(GEOCODING_URL, {"name": location, "count": 1, "format": "json"})
        results = data.get("results") or []
        if not results:
            raise ToolError(f"Couldn't find a place called '{location}'.")
        return results[0]

    async def _get(self, url: str, params: dict) -> dict:
        try:
            response = await self.client.get(url, params=params, timeout=10.0)
        except httpx.HTTPError as exc:
            raise ToolError("The weather service is unreachable. Are you online?") from exc
        if response.status_code != 200:
            raise ToolError(f"The weather service returned HTTP {response.status_code}.")
        return response.json()


def _first(values: list | None):
    return values[0] if values else None
