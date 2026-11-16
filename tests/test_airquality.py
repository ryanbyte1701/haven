import asyncio
import time

import httpx
import pytest

from app.adapters import airquality as aq
from app.hub import Hub

NOW = int(time.time()) // 3600 * 3600

ZIPPO = {"post code": "85004", "country": "United States", "country abbreviation": "US",
         "places": [{"place name": "Phoenix", "longitude": "-112.0691", "state": "Arizona",
                     "state abbreviation": "AZ", "latitude": "33.4511"}]}
OPEN_METEO = {
    "latitude": 33.45, "longitude": -112.07, "timezone": "America/Phoenix",
    "current_units": {"us_aqi": "USAQI", "pm2_5": "μg/m³"},
    "current": {"time": NOW, "interval": 3600, "us_aqi": 63, "pm2_5": 12.34, "pm10": 30.1, "ozone": 98.7,
                "nitrogen_dioxide": 21.0, "sulphur_dioxide": 2.2, "carbon_monoxide": 310.0, "uv_index": 4.55,
                "dust": None, "us_aqi_pm2_5": 51, "us_aqi_pm10": 28, "us_aqi_ozone": 63, "us_aqi_nitrogen_dioxide": 20,
                "us_aqi_sulphur_dioxide": 3, "us_aqi_carbon_monoxide": 4},
    "hourly": {"time": [NOW - 7200 + 3600 * i for i in range(30)], "us_aqi": [40 + i for i in range(29)] + [None],
               "pm2_5": [10.0] * 30, "ozone": [90.0] * 30},
}
AIRNOW_OBS = [
    {"DateObserved": "2026-11-16 ", "HourObserved": 9, "LocalTimeZone": "MST", "ReportingArea": "Phoenix", "StateCode": "AZ",
     "Latitude": 33.45, "Longitude": -112.07, "ParameterName": "O3", "AQI": 41, "Category": {"Number": 1, "Name": "Good"}},
    {"DateObserved": "2026-11-16 ", "HourObserved": 9, "LocalTimeZone": "MST", "ReportingArea": "Phoenix", "StateCode": "AZ",
     "Latitude": 33.45, "Longitude": -112.07, "ParameterName": "PM2.5", "AQI": 108, "Category": {"Number": 3, "Name": "Unhealthy for Sensitive Groups"}},
    {"DateObserved": "2026-11-16 ", "HourObserved": 9, "LocalTimeZone": "MST", "ReportingArea": "Phoenix", "StateCode": "AZ",
     "Latitude": 33.45, "Longitude": -112.07, "ParameterName": "PM10", "AQI": 55, "Category": {"Number": 2, "Name": "Moderate"}},
]
AIRNOW_FC = [
    {"DateIssue": "2026-11-16 ", "DateForecast": "2026-11-17 ", "ReportingArea": "Phoenix", "ParameterName": "PM2.5", "AQI": 90, "Category": {"Number": 2}},
    {"DateIssue": "2026-11-16 ", "DateForecast": "2026-11-17 ", "ReportingArea": "Phoenix", "ParameterName": "O3", "AQI": 45, "Category": {"Number": 1}},
    {"DateIssue": "2026-11-16 ", "DateForecast": "2026-11-18 ", "ReportingArea": "Phoenix", "ParameterName": "PM2.5", "AQI": -1, "Category": {"Number": 3}},
]


def transport(calls, routes):
    def handler(req: httpx.Request):
        calls.append(str(req.url))
        for prefix, resp in routes.items():
            if str(req.url).startswith(prefix):
                return resp(req) if callable(resp) else httpx.Response(200, json=resp)
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("aqi,name", [(0, "Good"), (50, "Good"), (51, "Moderate"), (100, "Moderate"),
                                      (101, "Unhealthy for sensitive groups"), (150, "Unhealthy for sensitive groups"),
                                      (151, "Unhealthy"), (200, "Unhealthy"), (201, "Very unhealthy"), (300, "Very unhealthy"),
                                      (301, "Hazardous"), (480, "Hazardous"), (None, None)])
def test_category_boundaries(aqi, name):
    assert aq.category(aqi) == name


def test_parse_open_meteo():
    out = aq.parse_open_meteo(OPEN_METEO, now=NOW)
    v = out["values"]
    assert v["aqi"] == 63 and v["pm25"] == 12.3 and v["pm10"] == 30.1 and v["ozone"] == 98.7
    assert v["no2"] == 21.0 and v["so2"] == 2.2 and v["co"] == 310.0 and v["uv"] in (4.5, 4.6)
    assert "dust" not in v                       # null values are skipped
    assert out["labels"]["no2"] == "NO₂" and out["units"]["pm25"] == "µg/m³" and out["units"]["aqi"] is None
    assert out["dominant"] == "Ozone"            # highest US AQI sub-index (63)
    assert out["observed"] == NOW
    f = out["forecast"]
    assert f[0] == {"t": NOW - 3600, "aqi": 41}  # starts 1 h back, ends 24 h ahead, null dropped
    assert all(NOW - 3600 <= x["t"] <= NOW + 86400 for x in f) and len(f) == 26


def test_parse_airnow():
    out = aq.parse_airnow(AIRNOW_OBS, AIRNOW_FC)
    v = out["values"]
    assert v["aqi"] == 108 and v["pm25_aqi"] == 108 and v["ozone_aqi"] == 41 and v["pm10_aqi"] == 55
    assert out["dominant"] == "PM2.5" and out["labels"]["ozone_aqi"] == "Ozone (AQI)"
    assert [x["aqi"] for x in out["forecast"]] == [90, 125]   # max per day; category midpoint when AQI = -1
    assert out["observed"] is not None


def run(coro):
    return asyncio.run(coro)


def test_open_meteo_end_to_end():
    calls = []
    hub = Hub()
    a = aq.AirQualityAdapter(hub, {"zip_code": "85004", "name": "Outdoor air"},
                             transport=transport(calls, {"https://api.zippopotam.us/us/85004": ZIPPO,
                                                         aq.OPENMETEO_AQ: OPEN_METEO}))

    async def go():
        await a.refresh()
        await a.refresh()                        # geocode is cached
        await a.http.aclose()
    run(go())
    d = hub.devices["airquality:85004"]
    assert d.name == "Outdoor air (Phoenix, AZ)" and d.room == "Outdoor"
    assert d.capabilities["aqi"].value == 63 and d.capabilities["status_text"].value == "Moderate"
    assert d.capabilities["no2"].label == "NO₂"
    assert d.attributes["kind"] == "aqi_station" and d.attributes["lat"] == 33.4511 and d.attributes["dominant"] == "Ozone"
    assert isinstance(d.attributes["forecast"], list) and set(d.attributes["forecast"][0]) == {"t", "aqi"}
    assert sum("zippopotam" in c for c in calls) == 1
    q = [c for c in calls if c.startswith(aq.OPENMETEO_AQ)][0]
    assert "latitude=33.4511" in q and "timeformat=unixtime" in q and "us_aqi" in q
    assert a.status.startswith("ok · AQI 63")


def test_geocode_fallback_to_open_meteo():
    calls = []
    a = aq.AirQualityAdapter(Hub(), {"zip_code": "10115", "country": "DE"},
                             transport=transport(calls, {
                                 "https://api.zippopotam.us/": lambda r: httpx.Response(404, json={}),
                                 aq.GEO_OPENMETEO: {"results": [{"name": "Berlin", "admin1": "Land Berlin", "latitude": 52.53, "longitude": 13.38}]}}))
    g = run(a.geocode())
    assert g == {"lat": 52.53, "lon": 13.38, "place": "Berlin, Land Berlin"}


def test_airnow_end_to_end_and_bad_key():
    calls = []
    hub = Hub()
    a = aq.AirQualityAdapter(hub, {"zip_code": "85004", "provider": "airnow", "airnow_api_key": "k"},
                             transport=transport(calls, {aq.AIRNOW_OBS: AIRNOW_OBS, aq.AIRNOW_FC: AIRNOW_FC}))
    run(a.refresh())
    d = hub.devices["airquality:85004"]
    assert d.capabilities["aqi"].value == 108 and d.capabilities["status_text"].value == "Unhealthy for sensitive groups"
    assert d.model == "AirNow (EPA)" and "Phoenix, AZ" in d.name
    bad = aq.AirQualityAdapter(Hub(), {"zip_code": "85004", "provider": "airnow", "airnow_api_key": "x"},
                               transport=transport([], {aq.AIRNOW_OBS: lambda r: httpx.Response(401, json={})}))
    with pytest.raises(RuntimeError, match="rejected the API key"):
        run(bad.refresh())


def test_error_keeps_last_values_and_reports_status():
    calls, fail = [], {"on": False}

    def om(req):
        return httpx.Response(503, json={}) if fail["on"] else httpx.Response(200, json=OPEN_METEO)
    hub = Hub()
    a = aq.AirQualityAdapter(hub, {"zip_code": "85004"},
                             transport=transport(calls, {"https://api.zippopotam.us/": ZIPPO, aq.OPENMETEO_AQ: om}))
    a.poll = 0.05                                # poll fast for the test (normal minimum is 15 min)

    async def go():
        await a.start()
        await asyncio.sleep(0.3)
        assert hub.devices["airquality:85004"].capabilities["aqi"].value == 63
        fail["on"] = True
        await asyncio.sleep(1.0)
        status = a.status
        await a.stop()
        return status
    status = run(go())
    assert status.startswith("error")
    assert hub.devices["airquality:85004"].capabilities["aqi"].value == 63   # last good value kept


def test_missing_zip_and_missing_key_status():
    a = aq.AirQualityAdapter(Hub(), {})
    run(a.start())
    assert a.status == "needs a ZIP code (Settings > Outdoor air quality)" and not a._tasks
    b = aq.AirQualityAdapter(Hub(), {"zip_code": "85004", "provider": "airnow"})
    run(b.start())
    assert "AirNow API key" in b.status and not b._tasks
    run(a.http.aclose()); run(b.http.aclose())


def test_poll_minimum_and_registration():
    assert aq.AirQualityAdapter(Hub(), {"zip_code": "1", "poll_minutes": 1}).poll == 15 * 60
    from app.adapters import ADAPTERS
    from app.settings import SCHEMA
    assert ADAPTERS["airquality"] is aq.AirQualityAdapter
    sec = next(s for s in SCHEMA if s["id"] == "airquality")
    assert {f["key"] for f in sec["fields"]} == {"zip_code", "country", "provider", "airnow_api_key", "name", "poll_minutes"}
