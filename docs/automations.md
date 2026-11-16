# Automations & time

## Automations
Menu → **Automations** shows every rule in a table: on/off, name, *When*, *Only if*, *Then*, last run, and
buttons to run it now (this skips the conditions, for testing), edit it or delete it. Under the table, **Recent runs** lists the
last 200 runs, including skipped ones and the reason they were skipped.

A rule is **when** something happens, **if** (optional) conditions hold, **then** do steps in order:

| | Options |
|---|---|
| **When** | a device *changes* / *becomes* a value / *goes above* / *drops below* a number · **at a time** (HH:MM) on chosen days · **repeatedly** every N minutes |
| **Only if** | time of day (between A and B; can wrap past midnight) · days of the week · a device *is* / *is above* / *is below* |
| **Then** | control a device (any control the dashboard has) · send a notification (shown in every open dashboard) · wait N seconds (max 1 h) · call a webhook (GET/POST) |

Device triggers fire only on **transitions**: "becomes open" runs once when the door opens, not on every
repeated "open" report. Under *Advanced*, a cooldown stops a rule from running again for N seconds.
Rules triggered by a device run at most once every 30 seconds, whatever the cooldown says.

**Safety:** Ecowitt and MQTT sensors can be faked by anything on your network. A rule where one of them (as the
trigger or a condition) unlocks a door, disarms the alarm or opens a valve is only saved after you tick
*Allow anyway*, and every automated unlock, disarm or valve-open sends a notification.

Rules are stored in `data/automations.json`.

API (admins; send the session token as `Authorization: Bearer …`):
```
GET    /api/automations              list
POST   /api/automations              create  {name, enabled, cooldown, allow_unsafe, trigger, conditions[], actions[]}
PUT    /api/automations/{id}         update
DELETE /api/automations/{id}
POST   /api/automations/{id}/run     run now (skips trigger & conditions)
GET    /api/automations/log          recent runs
GET    /api/notifications            recent notifications
```
Example rule:
```json
{"name": "Front door at night",
 "trigger": {"type": "device", "device_id": "yolink:abc", "capability": "contact", "op": "becomes", "value": "open"},
 "conditions": [{"type": "time", "after": "22:00", "before": "06:00"}],
 "actions": [{"type": "notify", "message": "Front door opened"},
             {"type": "command", "device_id": "hue:porch", "capability": "light", "value": true}]}
```

## Date & time (NTP)
Settings → **Date & time**: timezone (IANA name, e.g. `America/Phoenix`), 12/24 h clock, NTP servers (default
`pool.ntp.org`, `time.cloudflare.com`; your router or NAS works too) and how often to check.

A container can't set the computer's clock. Instead, Haven measures how far the clock is off (the median of several
SNTP samples) and uses the corrected time for scheduled rules. The status shows the offset, e.g.
`ok · offset +12 ms · America/Phoenix`. If the offset is large, fix the host's time; on Synology that's
Control Panel → Regional Options → Time → sync with an NTP server. Haven needs outbound UDP port 123 for this.
`GET /api/time` returns the full status.
