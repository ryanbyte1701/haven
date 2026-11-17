"""What the HACS edition (ryanbyte1701/haven-hacs) ships. Read by tools/hacs/build.py; the rule is in docs/hacs.md.

THE RULE
- Core modules (app/*.py) and the dashboard (app/static/**) are DEFAULT-ALLOW: a new core module ships to both
  editions automatically. List a module in OG_ONLY_CORE only if it can't work inside Home Assistant.
- Integrations (app/adapters/*.py) are DEFAULT-DENY: a new integration you add to Haven ships only in the Docker
  edition ("haven-og"). The HACS edition gets every device from Home Assistant, so only the modules listed in
  HACS_ADAPTERS ship there. Never add a brand integration (yolink, ecobee, tapo, ...) to HACS_ADAPTERS. The
  exceptions are services that aren't a brand Home Assistant connects for you: the NUT client, the weather forecast
  and outdoor air quality. A module listed here must also be in app/edition.py HACS_ADAPTER_MODULES to load.

tests/test_hacs_build.py checks all of this, so a mistake fails CI instead of shipping.
"""

# app/*.py that are Docker-edition only (default-allow for everything else in app/).
OG_ONLY_CORE = {
    "privdrop.py": "drops root privileges inside the Docker container",
    "serve.py": "runs uvicorn on Haven's own ports (HTTP/HTTPS); Home Assistant serves the HACS edition",
    "aqara_login.py": "sign-in helper of the Aqara integration (not shipped)",
    "ring_login.py": "sign-in helper of the Ring integration (not shipped)",
}

# Shipped although the HACS edition doesn't use them, because app/main.py imports them unconditionally.
# They stay inert: their Settings sections are removed (app/edition.py trim_settings) and their API paths
# are never forwarded (app/edition.py HACS_BLOCKED_PATHS).
INERT_CORE = {
    "ha.py": "high availability between two Docker Havens (main.py imports it; inert: no 'ha' section, /api/ha blocked)",
    "timesync.py": "NTP check (main.py imports it; turned off, Home Assistant keeps the time and time zone)",
    "auth.py": "Haven's own accounts (main.py imports it; replaced at run time by Home Assistant's sign-in)",
    "auth_api.py": "Haven's own sign-in API (main.py imports it; /api/auth and /api/users are blocked)",
}

# app/adapters/*.py shipped in the HACS edition: DEFAULT-DENY, everything else stays in the Docker edition.
HACS_ADAPTERS = {
    "__init__.py": "the adapter registry (edition-aware, app/edition.py)",
    "base.py": "the Adapter base class",
    "homeassistant.py": "entity mapping (map_entity) and service calls, reused by hass_local",
    "ha_rules.py": "what kind of device a Home Assistant device is (one table of rules, used by homeassistant.py)",
    "hass_local.py": "the HACS edition's device source: Home Assistant itself, in-process",
    "nut.py": "UPS monitoring (pure-Python NUT protocol client; also parse_status, used by homeassistant.py)",
    "weather.py": "weather forecast from Open-Meteo (httpx) for the Home page",
    "airquality.py": "outdoor air quality from Open-Meteo / AirNow (httpx); weather.py geocodes with it",
}

# app/static/** files that only make sense in the Docker edition.
OG_ONLY_STATIC = {
    "ha-splash.js": "high-availability 'open the active Haven' splash page",
}

# Python packages the HACS edition installs into Home Assistant (manifest.json "requirements").
# Their version specifiers are copied from requirements.txt, so both editions move together. Everything else the
# shipped core imports (pydantic, aiohttp, PyYAML, cryptography, anyio) already comes with Home Assistant.
# httpx: automation webhooks, Prometheus history, weather and outdoor air quality (Home Assistant pins it but no
# longer installs it itself). nut.py needs nothing beyond the standard library.
HACS_REQUIREMENTS = ["fastapi", "starlette", "httpx"]

# The integration
DOMAIN = "haven"
NAME = "Haven"
SOURCE_REPO = "ryanbyte1701/haven"
HACS_REPO = "ryanbyte1701/haven-hacs"
CODEOWNERS = ["@ryanbyte1701"]
MIN_HOME_ASSISTANT = "2026.3.0"      # local brand images (custom_components/haven/brand); tested on 2026.11.2
