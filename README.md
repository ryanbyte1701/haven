# Haven

A self-hosted home dashboard that runs as a single Docker app, with no Home Assistant needed.
It brings **YoLink, Aqara, Ecowitt, X-Sense, Ring, Philips Hue, UniFi UPS (NUT) and anything on MQTT**
into one live view. That covers temperature, humidity, soil moisture, leaks, doors and windows, presence,
smoke and CO, lighting, batteries and UPS power. You can also control locks, valves, lights and switches.

```
 YoLink cloud ──(REST + MQTT push)───┐
 Aqara cloud ───(Open API poll)──────┤
 X-Sense cloud ─(python-xsense)──────┤
 Ring cloud ────(ring_doorbell+push)─┤──► Hub ──► REST + WebSocket ──► Web dashboard (Material 3)
 Hue Bridge ────(local CLIP v2 + SSE)┤      │
 Ecowitt GW ────(local HTTP push)────┤      ├──► History: SQLite / Prometheus  ·  /metrics
 UniFi UPS ─────(NUT client)─────────┤      └──► Built-in NUT server :3493 (Synology, Proxmox, upsmon…)
 MQTT broker ───(HA discovery+topics)┘           Zigbee2MQTT · ESPHome · Tasmota · Shelly · ring-mqtt
```

## Quick start

Haven ships as a multi-arch image (Intel/AMD + ARM64) built by GitHub Actions on every push:
**`ghcr.io/ryanbyte1701/haven:latest`**.

```bash
mkdir haven && cd haven && mkdir data
curl -O https://raw.githubusercontent.com/ryanbyte1701/haven/main/docker-compose.yml
# edit docker-compose.yml: `user:` (run `id`; Synology is usually 1026:100), DASH_TOKEN password, TZ
docker compose up -d
```

Open `http://<docker-host>:8080`, go to **Settings**, and switch on your accounts. Each card says where to
find the keys, and Ring, Aqara and Hue have a guided **Sign in / Pair** button. Settings are saved to
`data/settings.json` and take effect immediately. Update with `docker compose pull && docker compose up -d`.

**Synology NAS:** see [SYNOLOGY.md](SYNOLOGY.md) for step-by-step Container Manager setup.

**From source:** `git clone https://github.com/ryanbyte1701/haven`, then in `docker-compose.yml` swap the
`image:` line for `build: .`. Prefer files over the Settings page? Every option is documented in
`config/config.example.yaml`; copy it to `config/config.yaml` (Settings values win over the file).

## Brands

| Brand | Setup (all in Settings) | Updates |
|---|---|---|
| **YoLink** | YoLink app → Settings → Account → Advanced Settings → **User Access Credentials** → UAID + Secret Key | Instant (YoLink MQTT), plus a full refresh every 15 min |
| **Aqara** | developer.aqara.com project → App ID, App Key, Key ID → **Sign in** (code is sent to your Aqara account) | Polled every 60 s. Tokens refresh automatically |
| **Ecowitt** | Gateway → Weather Services → **Customized**: Ecowitt protocol, server = Haven's IP, port 8080, path `/data/report`, interval 60 s | Every push from the gateway |
| **X-Sense** | A **second** X-Sense account your devices are shared to (one login per account) | Polled every 2 min |
| **Ring** | **Sign in** with email, password and the verification code Ring sends | Motion and doorbell presses are instant (push). Battery and settings are polled |
| **Philips Hue** | Enter the bridge IP (or let Haven find it) → **Pair bridge** → press the bridge's link button | Instant (bridge event stream), runs locally |
| **UPS (NUT)** | UniFi Network → the UPS → Settings → enable **NUT Server**, then add its IP in Haven | Every 5 s |
| **MQTT** | Broker host. Devices that publish Home Assistant discovery (Zigbee2MQTT, ESPHome, Tasmota, Shelly, ring-mqtt) appear on their own. Custom topics go in `config.yaml` | Instant |

**Tips**
- **Aqara Zigbee sensors:** pairing them to Zigbee2MQTT (instead of an Aqara hub) keeps them local and instant.
- **Ring Alarm** (contact sensors, keypad, alarm mode): run [ring-mqtt](https://github.com/tsightler/ring-mqtt) next to Haven. Its devices, including the alarm panel with Off/Home/Away, appear through MQTT.

## Controls

| Type | Supported via |
|---|---|
| Locks (lock/unlock) | YoLink Lock / LockV2, MQTT (Z2M, Z-Wave JS UI), Aqara (where the API allows) |
| Valves (open/close) | YoLink valve controllers, MQTT, Aqara via resource mapping |
| Lights (on/off, brightness, color temperature, color) | Philips Hue (lights, rooms, zones), MQTT lights (Zigbee2MQTT etc.) |
| Switches / outlets | YoLink outlets, Aqara relays, MQTT |
| Alarm panel (Off / Home / Away) | Ring Alarm via ring-mqtt, any MQTT `alarm_control_panel` |
| Floodlight, siren, snapshot | Ring cameras |
| Smoke alarm test / mute | X-Sense |

Locks, valves, sirens, alarm modes and alarm tests ask for confirmation before they run.

## UPS and the built-in NUT server

The **Power** page shows each UPS's charge, runtime, load, voltages and charge/load history.
Haven also runs a **NUT server on port 3493** that re-shares every UPS it monitors, so your other machines can
shut down safely when the power goes out:

- **Synology:** Control Panel → Hardware & Power → UPS → Enable UPS support → *Synology UPS server* → Haven's IP.
  The defaults match what Synology expects: UPS name `ups`, user `monuser`, password `secret`.
- **Proxmox / TrueNAS / Unraid / upsmon:** `ups@<haven-ip>:3493`.
- Clients get `ERR DATA-STALE` if Haven loses contact with the UPS, so nothing acts on old data.
  Forced shutdown (FSD) from a primary client is passed on to the others.
- If your NAS's own "network UPS server" is switched on, it already uses port 3493. Change the left port number
  in `docker-compose.yml` or switch the NAS's server off.

## Dashboard

- **Pages:** Home, Climate, Lights, Water, Doors (contact sensors only), Security, Controls, Batteries (every battery device, lowest first) and Power (UPS + NUT server), plus Settings.
  The desktop side bar shows them all. Phones get a bottom bar (Home, Climate, Lights, Doors, More).
- **Layouts:** **Cards** or **Table**. Switch with the icon in the top bar or under Settings → Appearance. The table can be sorted by device, room, status, battery or last update.
  All cards are the same height.
- **Material Design 3 look:** light, dark or automatic theme and five theme colors. Fonts and icons are stored in the container, so it works offline.
  To add theme colors, see `tools/theme/`.
- **Alerts:** live updates. Rooms with an alarm sort to the top, and alarms turn cards and tiles red. Badges in the side bar count open doors, low batteries, lights on and UPS on battery.
- **History:** tap any sparkline for a history chart (1 h to 1 y, depending on what you store).
- **Arranging:** tap ✎, then drag cards to reorder them or move them to another room, and drag room titles to reorder rooms.
  Drop cards on ★ Favorites to pin them to the top of Home. The ✎ on a card renames or hides it.
  The layout is saved on the server (`data/layout.json`), so every screen shows the same arrangement.
  Rooms with an active alarm still jump to the top.
- **Devices tab:** every device (hidden ones too) with the built-in tabs it appears in. Tabs are chosen automatically
  from what each device reports (a contact sensor goes to Doors and Security, a smart plug to Controls, and so on).
  Tap a tab to add or remove a device, for example to put a smart plug that runs lamps under Lights or take a
  mailbox sensor out of Security. Changed devices are marked *Custom* and have a reset button. You can also rename,
  move, pin and hide from here, and filter by *Tabs changed*, *In no tab* or *Hidden*. Every visible device still
  appears on Home. Saved per device in `data/overrides.json`.
- **Custom views:** **+ New view** in the side menu. Name it, pick an icon, then choose rooms, device types and
  brands (a device must match every group you fill in), and/or pick devices by hand. Views are saved on the
  server, so they appear on every screen; on phones they're under More. Edit or delete a view with the
  sliders button at the top of its page.
- **Card styles:** *Standard* (readings, sparkline, full controls) or *Minimal* (compact tiles with a one-tap control),
  chosen under Settings → Appearance for each screen.

## History & metrics

Readings are kept for charts according to **Settings → History & metrics** (or `history:` in `config.yaml`):

| Backend | Keeps | Needs |
|---|---|---|
| `sqlite` (default) | `retention_days` (default 365) in `data/history.db` | nothing |
| `memory` | last 24 h, lost on restart | nothing |
| `prometheus` | whatever Prometheus retains (`PROM_RETENTION`, default 1y) | the optional `prometheus` service |

Numeric values are saved at most every `sample_seconds` (default 60). On/off states (doors, locks, leaks,
valves, UPS status) are saved as numbers the moment they change, so they can be charted and alerted on.

Haven always publishes current values at **`/metrics`** in Prometheus format (`haven_value`,
`haven_device_online`, `haven_battery_percent`, `haven_ups_*`, `haven_last_update_timestamp_seconds`).
To run the bundled Prometheus: `docker compose --profile metrics up -d`. It scrapes Haven every 30 s using
`prometheus/prometheus.yml`. Set a **/metrics bearer token** in Settings to protect the endpoint, and put the
same token in `prometheus/prometheus.yml`. You can point Grafana at `http://<host>:9090` for dashboards.

## Security

- **Password:** set `DASH_TOKEN` in `docker-compose.yml` to require a password for the dashboard, its controls and the Settings page.
  Until you do, anyone on your network can change accounts, and the Settings page warns about this.
- **Stored credentials:** they live in `data/settings.json` with owner-only permissions, and are never sent back to the browser.
- **Remote access:** don't expose port 8080 to the internet. Use a reverse proxy with HTTPS, Tailscale or a VPN.
- **Ecowitt:** its endpoint stays open because gateways can't send a token. Restrict it with *Only accept these gateway PASSKEYs*.
- **MQTT:** the bundled broker allows anonymous access on your LAN. Add a password file (see `mosquitto/mosquitto.conf`) if other people share your network.
- **NUT server:** limit it to your network under Settings → UPS (NUT) → *Allowed client networks*.

## API

```
GET   /api/health
GET   /api/devices
GET   /api/devices/{id}/history/{capability}?range=1h|24h|7d|30d|1y
GET   /api/history/info            active history backend and available ranges
POST  /api/devices/{id}/command    {"capability": "valve", "value": "close"}
PATCH /api/devices/{id}            {"name": "...", "room": "...", "hidden": false}
GET   /api/layout                  PUT /api/layout {"rooms": [...], "order": {room: [ids]}, "pinned": [ids]}
GET   /api/settings                integrations, fields and status (secrets masked)
PUT   /api/settings/{section}      {"enabled": true, "values": {...}}  saves + restarts that integration
POST  /api/settings/ring/login     {"email","password","code"?}
POST  /api/settings/aqara/login    {"account","code"?}
GET   /api/settings/hue/discover   POST /api/settings/hue/pair {"host"}
GET   /api/nut/server              NUT server status and connected clients
GET   /api/ring/{id}/snapshot      JPEG
GET   /metrics                     Prometheus exposition (own optional bearer token)
WS    /ws                          snapshot + live device updates
POST  /data/report                 Ecowitt push
```

## Adding a brand

Create `app/adapters/<brand>.py` with a subclass of `Adapter` that calls `hub.upsert_device()` and
`hub.set_values()`, and implement `command()` if the brand has controls. Register it in
`app/adapters/__init__.py`, and add a section to `SCHEMA` in `app/settings.py` so it appears in Settings.

## What's been tested

- **Tested end to end:** Ecowitt, MQTT (discovery, manual topics, lights, alarm panel), the NUT client and server (against a simulated UniFi UPS and Home Assistant's `aionut` client, including power loss, data going stale and recovery), history storage, `/metrics` and the Settings page.
- **Written against their published APIs but not yet run with real accounts:** YoLink, Aqara, X-Sense, Ring and Philips Hue.
  Check `docker logs haven` on first run. Aqara resource IDs vary by model; use `/api/aqara/resources/<model>` to map any that don't appear.

Tests: `pip install -r requirements.txt pytest && pytest tests`
