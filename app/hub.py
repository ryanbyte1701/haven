"""Central state store: holds devices, routes commands to adapters, fans out updates."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any

from .config import DATA_DIR
from .models import Capability, Device

log = logging.getLogger("hub")

HISTORY_POINTS = 288      # 24h at 5-minute resolution
HISTORY_INTERVAL = 300


def _clean_view(v: dict) -> dict:
    """A custom view: name + icon + rules. A device matches if hand-picked, or if it passes every
    non-empty rule (rooms, types, sources)."""
    lst = lambda k, n=200: [str(x)[:200] for x in (v.get(k) or [])][:n]   # noqa: E731
    return {"id": str(v["id"])[:40], "name": str(v.get("name") or "View")[:40], "icon": str(v.get("icon") or "dashboard")[:40],
            "rooms": lst("rooms"), "types": lst("types", 20), "sources": lst("sources", 20), "devices": lst("devices")}


class Hub:
    def __init__(self) -> None:
        self.devices: dict[str, Device] = {}
        self.adapters: dict[str, Any] = {}
        self.history: dict[tuple[str, str], deque] = {}
        self.recorder: Any = None   # app.history.History, set at startup (persistent storage)
        # Callables(device_id, {capability: (old, new)}) run on every value change (e.g. automations).
        self.listeners: list = []
        self._subs: set[asyncio.Queue] = set()
        self._overrides_path = DATA_DIR / "overrides.json"
        self.overrides: dict[str, dict] = self._load_overrides()
        self._layout_path = DATA_DIR / "layout.json"
        self.layout: dict = self._load_layout()

    # ---------- persistence of user edits (name / room / hidden) ----------
    def _load_overrides(self) -> dict:
        try:
            return json.loads(self._overrides_path.read_text())
        except Exception:
            return {}

    # ---------- dashboard layout (room order, device order, favorites) ----------
    def _load_layout(self) -> dict:
        try:
            data = json.loads(self._layout_path.read_text())
            return {"rooms": list(data.get("rooms", [])), "order": dict(data.get("order", {})),
                    "pinned": list(data.get("pinned", [])), "views": list(data.get("views", []))}
        except Exception:
            return {"rooms": [], "order": {}, "pinned": [], "views": []}

    def set_layout(self, layout: dict) -> dict:
        clean = {
            "rooms": [str(r)[:80] for r in layout.get("rooms", [])][:200],
            "order": {str(r)[:80]: [str(i)[:200] for i in ids][:500]
                      for r, ids in list((layout.get("order") or {}).items())[:200]},
            "pinned": [str(i)[:200] for i in layout.get("pinned", [])][:100],
            "views": [_clean_view(v) for v in (layout.get("views") or [])[:30] if isinstance(v, dict) and v.get("id")],
        }
        self.layout = clean
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self._layout_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, indent=2))
        tmp.replace(self._layout_path)
        for q in list(self._subs):
            try:
                q.put_nowait({"type": "layout", "layout": clean})
            except asyncio.QueueFull:
                pass
        return clean

    def set_override(self, device_id: str, **fields) -> Device | None:
        o = self.overrides.setdefault(device_id, {})
        if fields.pop("auto_categories", None):
            o.pop("categories", None)
        o.update({k: v for k, v in fields.items() if v is not None})
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._overrides_path.write_text(json.dumps(self.overrides, indent=2))
        dev = self.devices.get(device_id)
        if dev:
            self._apply_overrides(dev)
            self._publish(dev)
        return dev

    def _apply_overrides(self, dev: Device) -> None:
        for k, v in self.overrides.get(dev.id, {}).items():
            if k in ("name", "room", "hidden"):
                setattr(dev, k, v)
        dev.categories = self.overrides.get(dev.id, {}).get("categories")

    # ---------- adapter-facing API ----------
    def upsert_device(self, dev: Device) -> None:
        old = self.devices.get(dev.id)
        if old:
            for k, c in dev.capabilities.items():
                if c.value is None and k in old.capabilities:
                    c.value = old.capabilities[k].value
                    c.updated = old.capabilities[k].updated
            for k, c in old.capabilities.items():
                dev.capabilities.setdefault(k, c)
            if not dev.room:
                dev.room = old.room
        self._apply_overrides(dev)
        self.devices[dev.id] = dev
        self._publish(dev)

    def set_values(self, device_id: str, values: dict[str, Any], online: bool | None = True) -> None:
        dev = self.devices.get(device_id)
        if not dev:
            return
        now = time.time()
        changes: dict[str, tuple[Any, Any]] = {}
        for k, v in values.items():
            if v is None:
                continue
            c = dev.capabilities.get(k)
            if c is None:
                c = dev.capabilities[k] = Capability(name=k)
            if c.value != v:
                changes[k] = (c.value, v)
            c.value = v
            c.updated = now
            self._record(device_id, k, v, now)
        dev.last_seen = now
        if online is not None:
            dev.online = online
        self._publish(dev)
        if changes:
            for fn in list(self.listeners):
                try:
                    fn(device_id, changes)
                except Exception as e:  # noqa: BLE001 - a listener must never break device updates
                    log.warning("listener failed: %s", e)

    def broadcast(self, msg: dict) -> None:
        """Send an arbitrary message (e.g. a notification) to every connected browser."""
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def remove_source(self, source: str) -> None:
        """Drop all devices from an integration that was turned off."""
        for did in [d.id for d in self.devices.values() if d.source == source]:
            self.devices.pop(did, None)
            for q in list(self._subs):
                try:
                    q.put_nowait({"type": "remove", "id": did})
                except asyncio.QueueFull:
                    pass

    def set_attributes(self, device_id: str, attrs: dict[str, Any], publish: bool = False) -> None:
        dev = self.devices.get(device_id)
        if dev:
            dev.attributes.update(attrs)
            if publish:
                self._publish(dev)

    def _record(self, device_id: str, cap_name: str, v: Any, now: float) -> None:
        if self.recorder is not None:
            self.recorder.record(device_id, cap_name, v, now)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return
        buf = self.history.setdefault((device_id, cap_name), deque(maxlen=HISTORY_POINTS))
        if buf and now - buf[-1][0] < HISTORY_INTERVAL:
            buf[-1] = (buf[-1][0], v)   # keep latest value within the bucket
        else:
            buf.append((now, v))

    # ---------- commands ----------
    async def command(self, device_id: str, capability: str, value: Any) -> None:
        dev = self.devices.get(device_id)
        if not dev:
            raise KeyError(f"unknown device {device_id}")
        c = dev.capabilities.get(capability)
        if not c or not c.writable:
            raise ValueError(f"{capability} is not controllable on {dev.name}")
        if c.options and value not in c.options:
            raise ValueError(f"value must be one of {c.options}")
        adapter = self.adapters.get(dev.source)
        if not adapter:
            raise RuntimeError(f"adapter {dev.source} not running")
        log.info("command %s %s=%s", device_id, capability, value)
        await adapter.command(dev, capability, value)

    # ---------- live updates ----------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _publish(self, dev: Device) -> None:
        msg = {"type": "device", "device": dev.model_dump()}
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass
