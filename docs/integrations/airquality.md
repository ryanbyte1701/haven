# Outdoor air quality (by ZIP / postal code)

Shows the outdoor Air Quality Index (AQI) and the pollutants behind it for your area, refreshed every hour.

## Set up
1. Haven → **Settings** → **Outdoor air quality**.
2. Enter your **ZIP / postal code** (and **Country code** if you're outside the US, e.g. `CA`, `GB`, `DE`).
3. Pick a **Data source**:
   - **open-meteo** (default, free, no account): works worldwide. Uses modelled data, so it's available
     everywhere, though it can differ a little from official monitors. Includes PM2.5, PM10, ozone, NO₂,
     SO₂, carbon monoxide, dust, UV index and an hourly AQI forecast for the next 24 hours.
   - **airnow** (US only): the US EPA's official AQI for your reporting area, the same figures as
     airnow.gov. Needs a free API key: sign up at <https://docs.airnowapi.org/>, then paste the key into
     *AirNow API key*. It reports AQI per pollutant (PM2.5, PM10, ozone) plus a daily forecast.
4. Turn it on and **Save**. A device called "Outdoor air (your town)" appears in the *Outdoor* room.

## What you get
| Reading | Notes |
|---|---|
| AQI | US EPA scale. The status line shows the category: Good (0–50), Moderate (51–100), Unhealthy for sensitive groups (101–150), Unhealthy (151–200), Very unhealthy (201–300), Hazardous (301+). |
| PM2.5, PM10, Ozone, NO₂, SO₂, CO, Dust | µg/m³ (Open-Meteo). With AirNow these are AQI values per pollutant instead. |
| UV index | Open-Meteo only. |
| Main pollutant | The pollutant driving the AQI right now. |
| Forecast | Next 24 h hourly (Open-Meteo) or the next days (AirNow). |

## Notes
- The refresh interval can be changed (minimum 15 minutes). Both services update roughly hourly, so
  refreshing faster doesn't give newer data.
- Postal codes are looked up with zippopotam.us, falling back to Open-Meteo's geocoder.
- If the ZIP can't be found, or AirNow has no monitor within 25 miles, the Settings page shows why.
