import asyncio
import httpx

from app.adapters.weather import WeatherAdapter, parse_forecast, condition
from app.hub import Hub

FC = {"current": {"temperature_2m": 31.2, "relative_humidity_2m": 12, "apparent_temperature": 29.8, "weather_code": 2,
                  "is_day": 1, "wind_speed_10m": 6.1, "wind_gusts_10m": 14.0, "wind_direction_10m": 250,
                  "precipitation": 0, "uv_index": 7.1, "pressure_msl": 1011.2},
      "daily": {"time": [1794816000, 1794902400], "weather_code": [2, 61], "temperature_2m_max": [33.0, 28.5],
                "temperature_2m_min": [18.2, 17.0], "precipitation_probability_max": [0, 60], "precipitation_sum": [0, 0.2],
                "sunrise": [1794840000, 1794926400], "sunset": [1794879000, 1794965400], "uv_index_max": [7.5, 5.0]}}


def handler(req: httpx.Request):
    if "zippopotam" in str(req.url):
        return httpx.Response(200, json={"places": [{"place name": "Phoenix", "state abbreviation": "AZ", "latitude": "33.4", "longitude": "-112.0"}]})
    if "api.open-meteo.com" in str(req.url):
        assert req.url.params["wind_speed_unit"] == "mph"
        return httpx.Response(200, json=FC)
    return httpx.Response(404)


def test_parse_and_device():
    w = parse_forecast(FC, True)
    assert w["values"]["temperature"] == 31.2 and w["values"]["status_text"] == "Partly cloudy"
    assert w["daily"][1]["text"] == "Light rain" and w["daily"][1]["pop"] == 60
    assert condition(None)[0] == "—" and condition(999)[0] == "—"

    async def run():
        hub = Hub()
        a = WeatherAdapter(hub, {"zip_code": "85001"}, transport=httpx.MockTransport(handler))
        await a.refresh("85001", "US")
        await a.stop()
        return hub, a
    hub, a = asyncio.run(run())
    d = hub.devices["weather:85001"]
    assert d.attributes["kind"] == "weather" and d.attributes["place"] == "Phoenix, AZ" and len(d.attributes["daily"]) == 2
    assert d.capabilities["wind_speed"].unit == "mph" and d.capabilities["feels_like"].unit == "°C"
    assert a.status.startswith("ok")


def test_uses_air_quality_zip_when_empty():
    hub = Hub()
    hub.adapters["airquality"] = type("AQ", (), {"zip": "10001", "country": "US"})()
    a = WeatherAdapter(hub, {})
    assert a._location() == ("10001", "US")
    asyncio.run(a.http.aclose())
