# Haven for Home Assistant (the HACS edition)

Haven comes in two editions built from this one repository:

- **Haven (Docker)**, "haven-og": this repository. Its own web server, its own integrations, accounts, backups and
  high availability.
- **Haven for Home Assistant**: [ryanbyte1701/haven-hacs](https://github.com/ryanbyte1701/haven-hacs), a Home
  Assistant custom integration installed with HACS. It adds a **Haven** item to Home Assistant's sidebar and gets
  **every device from Home Assistant**. That repository is **generated** from this one by `tools/hacs/build.py`.
  Never edit it by hand.

## Install in Home Assistant

1. HACS → ⋮ → **Custom repositories** → Repository `https://github.com/ryanbyte1701/haven-hacs`, Type
   **Integration** → Add.
2. Find **Haven** in HACS → **Download** → restart Home Assistant.
3. Settings → Devices & services → **Add integration → Haven** (nothing to fill in; one per Home Assistant).
4. **Haven** shows up in the sidebar for every user.

Home Assistant installs FastAPI (and httpx) the first time the integration loads; those are the only extra Python
packages. Everything else Haven uses (pydantic, aiohttp, PyYAML, cryptography, anyio) comes with Home Assistant.

## How it works

```
Home Assistant (one process)
├── Home Assistant's event loop
│   ├── aiohttp web server ── /api/haven/<x>       HavenApiView (requires_auth) ─┐
│   │                        /api/haven/websocket  HavenWebSocketView ───────────┤ bridge.py
│   │                        /haven_static/…       Haven's app/static            │ (aiohttp ⇄ ASGI)
│   │                        /haven_panel/…        panel.js, the dashboard page  │
│   ├── sidebar panel "Haven" (panel_custom → <haven-panel>)                     │
│   └── states, registries, event bus, services ◄────────┐                       │
└── thread "haven": Haven's own event loop               │                       │
    └── Haven's FastAPI app (core/main.py, unchanged) ◄──┼───────────────────────┘
        ├── core/adapters/hass_local.py ─────────────────┘ reads and controls Home Assistant in-process
        ├── history (SQLite), automations, layout, settings → <config>/haven/
        └── sign-in: auth_bridge.py (Home Assistant's users and tokens)
```

- **Haven's own FastAPI app runs in-process, unchanged.** FastAPI and Starlette install fine alongside Home
  Assistant's pinned packages (checked with HA 2026.11.2 and its package constraints: pydantic 2.14, aiohttp 3.14,
  anyio 4.15/4.16; FastAPI 0.163 and Starlette 1.10 resolve with them). So instead of porting Haven's ~60 routes to
  Home Assistant views, `bridge.py` hands Home Assistant's aiohttp requests and WebSockets to the ASGI app. Every
  API change in this repository reaches the Home Assistant edition with no extra work.
- **No second port.** Everything is served by Home Assistant's own web server, so HTTPS, reverse proxies, Nabu Casa
  remote access and the mobile app work with no extra setup. There are no mixed-content or CORS problems.
- **Haven runs in its own thread with its own event loop.** Haven reads and writes small files and its history
  database on its loop. That's fine on its own server but must not happen on Home Assistant's loop, where Home
  Assistant reports it as blocking and it would slow Home Assistant down. Requests, WebSocket messages and Home
  Assistant events cross between the two loops in `bridge.py` and `hass_local.py`. Anything that touches `hass`
  runs in Home Assistant's loop.
- **Devices**: `app/adapters/hass_local.py` subclasses the Docker edition's Home Assistant integration
  (`app/adapters/homeassistant.py`) and answers the WebSocket API calls that code makes (registries, states,
  `call_service`) from Home Assistant's objects. The mapping of entities to Haven capabilities (`map_entity`), the
  grouping into one card per Home Assistant device in its area, and the service calls are therefore the same code
  in both editions. Live updates come from `state_changed`. New, renamed, moved and removed devices come from the
  registry-updated events, re-read after a 2-second pause. Unlike the Docker edition nothing is skipped as
  "native": every device comes from Home Assistant. Hidden, disabled, configuration and diagnostic entities are
  left out as before (battery levels are kept).
- **Sign-in**: the panel passes the signed-in user's Home Assistant access token to Haven's page. API calls send
  it as `Authorization: Bearer`. Home Assistant checks it (`requires_auth`), and so does Haven's middleware through
  `auth_bridge.py`, which replaces Haven's account store. The WebSocket signs in with its first message, like Home
  Assistant's own; the token is swapped for a ticket that lives as long as the sign-in, so the 30-minute token
  expiry doesn't disconnect the dashboard. **Home Assistant administrators are Haven admins; every other Home
  Assistant user is a Regular user** (sees and controls devices). There are no Haven passwords, guests or view-only
  accounts, and Haven's sign-in page and "choose your own password" step never appear.
- **The panel** (`frontend/panel.js`) is a small custom element. It shows Haven's own page in a same-origin frame
  that fills Home Assistant's main area, so Home Assistant's sidebar stays and Haven's rail and phone bottom bar
  work as usual. On phones it adds a slim bar with the button that opens Home Assistant's menu. `hacs-shim.js`,
  loaded before Haven's scripts, sends `/api/…` to `/api/haven/…` with the token, sends `/ws` to
  `/api/haven/websocket`, keeps Haven's preferences under a `haven:` key prefix, makes "Auto" light/dark follow
  Home Assistant's theme, and replaces the account pages with a note about Home Assistant's accounts. Haven's
  scripts themselves are not changed.
- **Storage**: `<config>/haven/`: `settings.json`, `layout.json`, `overrides.json`, `automations.json`,
  `history.db`, and `base_config.yaml`, which the integration writes at every start with the time zone and units
  from Home Assistant. Home Assistant's backups include the folder.

## What's in each edition

| | Haven (Docker) | Haven for Home Assistant |
|---|---|---|
| Dashboard: all pages, device pop-ups, history charts, home page, custom views, appearance | ✓ | ✓ |
| Devices | Haven's integrations (YoLink, Aqara, Ecowitt, Hue, Ring, ecobee, Tapo, MQTT, Matter, …) and optionally Home Assistant over the network | Every Home Assistant device, in-process |
| Automations (Haven's own rules on Haven's devices) | ✓ | ✓ |
| History (SQLite / memory / Prometheus) | ✓ | ✓ (in `<config>/haven/history.db`) |
| `/metrics` for Prometheus | ✓ | – |
| Accounts, roles, guest viewing | Haven's own | Home Assistant's (admin → Admin, others → Regular) |
| Date & time (NTP check, time zone) | ✓ | – (Home Assistant's time zone) |
| Backups (SFTP/SCP/FTPS/FTP) | ✓ | – (Home Assistant's backups include `<config>/haven`) |
| High availability (two NASes) | ✓ | – |
| Ecowitt push endpoint (`/data/report`) | ✓ | – |
| Demo devices | ✓ | – |
| Web server, HTTPS, ports | Haven's own (uvicorn) | Home Assistant's |

Settings in the Home Assistant edition shows only **Home Assistant** (area, entity type and entity filters, Refresh
now), **History**, **Home page & menu** and **Appearance**.

## The edition switch (core)

`app/edition.py`: `HAVEN_EDITION=hacs` (set by the Home Assistant wrapper before it imports anything from the core;
the Docker edition never sets it) selects the Home Assistant edition. It decides:

- which adapter modules may register (`adapter_allowed`): in the Home Assistant edition only `hass_local`, and in the
  Docker edition everything except `hass_local`. Used by `app/adapters/__init__.py`;
- which Settings sections exist (`trim_settings`, used by `app/settings.py`): the edition's adapters plus `history`,
  without the `/metrics` fields;
- which API paths the bridge never forwards (`HACS_BLOCKED_PATHS`): Haven's own sign-in and users, high
  availability, Ecowitt, `/metrics`, `/data/report`, and the sign-ins of integrations that aren't shipped.

## What ships where: the allow-list rule

The list is `tools/hacs/manifest.py`. `tests/test_hacs_build.py` enforces it.

- **Core is default-allow.** Every `app/*.py` and everything in `app/static/` ships in both editions, so core
  improvements reach Home Assistant users automatically. Only modules that can't work inside Home Assistant are
  listed as Docker-only (`OG_ONLY_CORE`: `privdrop.py`, `serve.py`, and the Aqara/Ring sign-in helpers), plus
  `ha-splash.js` in `OG_ONLY_STATIC`. `ha.py`, `timesync.py`, `auth.py` and `auth_api.py` still ship because
  `main.py` imports them, but they stay inert (`INERT_CORE` says why for each).
- **Integrations are default-deny.** Of `app/adapters/`, only `__init__.py`, `base.py`, `homeassistant.py` (the
  mapping) and `hass_local.py` ship (`HACS_ADAPTERS`). **A new integration you add to `app/adapters/` ships only in
  the Docker edition**, with nothing to do. Never add a brand integration to `HACS_ADAPTERS`: in the Home Assistant
  edition, Home Assistant already connects the brands.
- Extra Python packages for the Home Assistant edition are named in `HACS_REQUIREMENTS`. Their versions are copied
  from `requirements.txt`.

## How the haven-hacs repository is kept in sync

- `tools/hacs/build.py --out <haven-hacs checkout>` writes `custom_components/haven/` (the wrapper from
  `tools/hacs/integration/`, the core under `core/`, the dashboard page under `frontend/`, `manifest.json`),
  `hacs.json` and `README.md` (from `tools/hacs/repo/`), and `LICENSE` if this repository has one. Stdlib only.
- **Version**: `YYYY.M.D.N`, in `manifest.json` and as the release tag `vYYYY.M.D.N`. It only changes when the
  generated files change: a commit that only touches the Docker edition (a new integration, say) builds the same
  files and keeps the version, so nothing is published.
- **In haven-hacs**, `.github/workflows/sync.yml` runs every 30 minutes (and by hand). It checks out this public
  repository (no secret needed), skips a commit whose **HACS edition** check is red or still running, runs the
  generator, and if anything changed commits, tags `v<version>`, publishes a GitHub release (so HACS offers the
  update), then runs `validate.yml` (HACS's `hacs/action` and Home Assistant's `hassfest`). It uses the haven-hacs
  `GITHUB_TOKEN` with `contents: write`.
- **In this repository**, `.github/workflows/hacs.yml` (check name **HACS edition**) runs the generator's and the
  edition's tests, builds the repository, checks the panel's JavaScript and runs `hassfest` on every push and pull
  request, so a core change can't break the Home Assistant edition unnoticed.
- The workflow files of haven-hacs come from `tools/hacs/repo/.github/workflows/`. GitHub doesn't let a
  `GITHUB_TOKEN` change workflow files, so the sync never writes them: after changing them here, run
  `python tools/hacs/build.py --out ../haven-hacs --with-workflows` and push haven-hacs by hand.

## Developing

```
pytest tests/test_hacs_build.py tests/test_hass_local.py      # generator, edition switch, bridge, device source
python tools/hacs/build.py --out /tmp/haven-hacs               # look at what would be published
```

`tests/test_hacs_build.py` builds the repository, imports the shipped core on its own (no checkout on the path,
`HAVEN_EDITION=hacs`), and drives the real FastAPI app through the bridge, in its own thread, against a fake Home
Assistant (`tests/hacs_fakes.py`). It covers roles, devices, commands, blocked paths and live updates.
`tests/test_hass_local.py` covers the device source: import, live updates, commands, registry changes, filters,
and the two-loop mode.

To try it in a real Home Assistant: `uv venv -p 3.14 /tmp/havenv && VIRTUAL_ENV=/tmp/havenv uv pip install
homeassistant`, copy `custom_components/haven` from a build into a config folder's `custom_components/`, start
`hass -c <folder>`, finish onboarding and add the Haven integration.
