import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import app.automations as au
from app.automations import AutomationEngine, validate
from app.hub import Hub
from app.models import Device, cap
from app import timesync

PHX = ZoneInfo("America/Phoenix")


class Clock:
    def __init__(self, dt):
        self.t, self.tz = dt.timestamp(), dt.tzinfo

    def now(self):
        return self.t

    def local(self, ts=None):
        return datetime.fromtimestamp(self.t if ts is None else ts, self.tz)


def rule(trigger_dev="ecowitt:gw_leak1", action=("yale:door", "lock", "unlock"), **extra):
    return {"name": "r", "trigger": {"type": "device", "device_id": trigger_dev, "capability": "leak",
                                     "op": "becomes", "value": True},
            "actions": [{"type": "command", "device_id": action[0], "capability": action[1], "value": action[2]}], **extra}


def test_unsafe_rule_rejected_unless_allowed():
    for dev in ("ecowitt:gw_leak1", "mqtt:z2m_door"):
        for act in (("yale:door", "lock", "unlock"), ("ring:alarm", "alarm_panel", "disarm"), ("flo:v", "valve", "open")):
            with pytest.raises(ValueError, match="Allow anyway"):
                validate(rule(dev, act))
            assert validate(rule(dev, act, allow_unsafe=True))["allow_unsafe"] is True
    # a low-trust device in a condition counts too
    r = rule("yale:door", ("yale:door", "lock", "unlock"))
    r["trigger"]["capability"] = "contact"
    r["conditions"] = [{"type": "device", "device_id": "mqtt:x", "capability": "presence", "op": "equals", "value": True}]
    with pytest.raises(ValueError, match="unauthenticated"):
        validate(r)
    # safe combinations are fine
    assert validate(rule("ecowitt:gw", ("yale:door", "lock", "lock")))["allow_unsafe"] is False
    assert validate(rule("ecowitt:gw", ("flo:v", "valve", "close")))
    assert validate(rule("yolink:leak", ("yale:door", "lock", "unlock")))
    assert validate(rule("ecowitt:gw", ("hue:l", "light", True)))


def test_allow_unsafe_persisted_and_old_unsafe_rule_disabled_on_load(tmp_path):
    e = AutomationEngine(Hub(), Clock(datetime(2026, 11, 16, 12, 0, tzinfo=PHX)), tmp_path)
    a = e.upsert(rule(allow_unsafe=True))
    saved = json.loads((tmp_path / "automations.json").read_text())["automations"][0]
    assert saved["allow_unsafe"] is True
    # a rule written before the check existed: kept but disabled
    bad = {**rule(), "id": "old1", "enabled": True}
    (tmp_path / "automations.json").write_text(json.dumps({"automations": [saved, bad]}))
    e2 = AutomationEngine(Hub(), Clock(datetime(2026, 11, 16, 12, 0, tzinfo=PHX)), tmp_path)
    assert e2.items[a["id"]]["enabled"] and not e2.items["old1"]["enabled"]


def hub_with(*devs):
    h = Hub()
    for did, c in devs:
        h.upsert_device(Device(id=did, name=did, source=did.split(":")[0], capabilities={c: cap(c)}))
    h.commands = []

    async def command(did, capability, value):
        h.commands.append((did, capability, value))
    h.command = command
    return h


def test_min_cooldown_for_device_triggers(tmp_path):
    async def go():
        h = hub_with(("t:sw", "switch"), ("t:l", "light"))
        e = AutomationEngine(h, Clock(datetime(2026, 11, 16, 12, 0, tzinfo=PHX)), tmp_path)
        h.listeners.append(e.on_change)
        e.upsert({"name": "x", "cooldown": 0, "trigger": {"type": "device", "device_id": "t:sw", "capability": "switch",
                                                          "op": "changes"},
                  "actions": [{"type": "command", "device_id": "t:l", "capability": "light", "value": True}]})
        h.set_values("t:sw", {"switch": False})
        for v in (True, False, True):
            h.set_values("t:sw", {"switch": v})
            await asyncio.sleep(0.02)
        assert len(h.commands) == 1 and e.runs[0]["skipped"] == "cooldown"
        e.clock.t += au.MIN_DEVICE_COOLDOWN
        h.set_values("t:sw", {"switch": False})
        await asyncio.sleep(0.02)
        assert len(h.commands) == 2
        # interval rules keep cooldown 0
        assert au.effective_cooldown({"trigger": {"type": "interval"}, "cooldown": 0}) == 0
        assert au.effective_cooldown({"trigger": {"type": "device"}, "cooldown": 600}) == 600
        await e.stop()
    asyncio.run(go())


def test_automated_unlock_notifies(tmp_path):
    async def go():
        h = hub_with(("yale:door", "lock"))
        e = AutomationEngine(h, Clock(datetime(2026, 11, 16, 12, 0, tzinfo=PHX)), tmp_path)
        a = e.upsert({"name": "Let in", "trigger": {"type": "interval", "minutes": 5},
                      "actions": [{"type": "command", "device_id": "yale:door", "capability": "lock", "value": "unlock"}]})
        await e.run(a["id"], "t", force=True)
        assert h.commands == [("yale:door", "lock", "unlock")]
        assert e.notifications[0]["title"] == "Automation: Let in" and "unlock" in e.notifications[0]["message"]
        b = e.upsert({"name": "Lock up", "trigger": {"type": "interval", "minutes": 5},
                      "actions": [{"type": "command", "device_id": "yale:door", "capability": "lock", "value": "lock"}]})
        await e.run(b["id"], "t", force=True)
        assert len(e.notifications) == 1                         # locking is not reported
        await e.stop()
    asyncio.run(go())


def test_webhook_pinned_to_vetted_ip(monkeypatch):
    async def go():
        ips = await au.check_webhook_url("http://192.168.1.20:8123/hook")
        assert ips == ["192.168.1.20"]
        url, headers, ext = au.pinned_request("https://hooks.lan:8443/x?y=1", "192.168.1.9")
        assert url == "https://192.168.1.9:8443/x?y=1" and headers == {"Host": "hooks.lan:8443"}
        assert ext == {"sni_hostname": "hooks.lan"}
        url, headers, ext = au.pinned_request("http://h/x", "fd00::5")
        assert url == "http://[fd00::5]/x" and headers == {"Host": "h"} and ext == {}
        # Haven's own port on its own address is refused; other ports there stay allowed
        import ipaddress
        monkeypatch.setattr(au, "_own_addresses", lambda: {ipaddress.ip_address("192.168.1.50")})
        monkeypatch.setenv("HAVEN_PORT", "1701")
        with pytest.raises(ValueError, match="Haven itself"):
            await au.check_webhook_url("http://192.168.1.50:1701/api/settings")
        assert await au.check_webhook_url("http://192.168.1.50:1880/node-red") == ["192.168.1.50"]
    asyncio.run(go())


def test_ntp_big_offset_ignored():
    async def go():
        ts = timesync.TimeService()
        ts.servers = ["a"]
        ts.query = lambda host: (0.02, 0.01)
        assert await ts.sync_once() and abs(ts.offset - 0.02) < 1e-9
        ts.query = lambda host: (3600.0, 0.01)                   # an hour off: not applied
        assert not await ts.sync_once()
        assert abs(ts.offset - 0.02) < 1e-9
        assert "clock offset too large: +3600s — fix the NAS clock" in ts.status_line
    asyncio.run(go())
