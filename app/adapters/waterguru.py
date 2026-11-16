"""WaterGuru pool / spa chemistry monitor (WaterGuru SENSE).

WaterGuru has no public API. Like the Home Assistant community integration (which builds on
bdwilson/waterguru-api), Haven signs in to WaterGuru's AWS Cognito user pool with your app email
and password, then calls the same "dashboard" function the app uses. Readings update a few times
a day (each test uses the cartridge), so polling every 10 minutes is plenty.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

from ..models import Device, cap, f_to_c
from .base import Adapter

REGION = "us-west-2"
USER_POOL = "us-west-2_icsnuWQWw"
CLIENT_ID = "7pk5du7fitqb419oabb3r92lni"
IDENTITY_POOL = "us-west-2:691e3287-5776-40f2-a502-759de65a8f1c"
DASHBOARD_FN = "prod-getDashboardView"

# WaterGuru measurement type -> (capability, unit)
MEASURE_MAP = {
    "PH": ("ph", None),
    "FREE_CL": ("free_chlorine", "ppm"),
    "ORP": ("orp", "mV"),
    "TA": ("total_alkalinity", "ppm"),
    "CYA": ("cyanuric_acid", "ppm"),
    "SKIMMER_FLOW": ("skimmer_flow", None),
}


def _jwt_claims(token: str) -> dict:
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def parse_waterbody(wb: dict) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict]]:
    """Return (values, attributes, extra capability specs) for one entry of data['waterBodies']."""
    values: dict[str, Any] = {}
    specs: dict[str, dict] = {}
    if wb.get("waterTemp") is not None:
        values["temperature"] = f_to_c(wb["waterTemp"])        # WaterGuru reports °F
    problems = []
    for m in wb.get("measurements") or []:
        mtype = str(m.get("type", "")).upper()
        name, unit = MEASURE_MAP.get(mtype, (mtype.lower(), None))
        v = m.get("floatValue", m.get("intValue"))
        if v is None or not name:
            continue
        values[name] = round(float(v), 2)
        specs[name] = {"unit": (m.get("cfg") or {}).get("unit") or unit, "label": m.get("title") or None}
        if str(m.get("status", "GREEN")).upper() != "GREEN":
            alerts = m.get("alerts") or [{}]
            summary = (((alerts[0] or {}).get("advice") or {}).get("action") or {}).get("summary")
            problems.append(summary or m.get("firstAlertCondition") or f"{m.get('title', name)} out of range")
    values["status_text"] = problems[0] if problems else "In range"
    pods = wb.get("pods") or [{}]
    pod = pods[0] or {}
    days = None
    for r in pod.get("refillables") or []:
        if r.get("type") == "BATT" and r.get("pctLeft") is not None:
            values["battery"] = int(r["pctLeft"])
        if r.get("type") == "LAB" and r.get("pctLeft") is not None:
            values["cartridge_life"] = int(r["pctLeft"])
            text = str(r.get("timeLeftText") or "")
            try:
                n = int(text.split()[0])
                days = n * 7 if "week" in text else n * 30 if "month" in text else n
            except (ValueError, IndexError):
                pass
    attrs = {"kind": "pool", "waterguru_id": wb.get("waterBodyId"), "overall_status": wb.get("status"),
             "last_measurement": wb.get("latestMeasureTime"), "cartridge_days_left": days,
             "problems": problems, "serial": str(((pod.get("pod") or {}).get("podId")) or "") or None}
    return values, attrs, specs


class WaterGuruClient:
    """Blocking client (boto3); call from a thread."""

    def __init__(self, email: str, password: str):
        self.email, self.password = email, password
        self.lambda_ = None
        self.user_id = None
        self.expires = 0.0

    def _login(self) -> None:
        import boto3
        from pycognito.aws_srp import AWSSRP
        idp = boto3.client("cognito-idp", region_name=REGION)
        tokens = AWSSRP(username=self.email, password=self.password, pool_id=USER_POOL,
                        client_id=CLIENT_ID, client=idp).authenticate_user()
        id_token = tokens["AuthenticationResult"]["IdToken"]
        self.user_id = _jwt_claims(id_token).get("cognito:username")
        ident = boto3.client("cognito-identity", region_name=REGION)
        logins = {f"cognito-idp.{REGION}.amazonaws.com/{USER_POOL}": id_token}
        iid = ident.get_id(IdentityPoolId=IDENTITY_POOL, Logins=logins)["IdentityId"]
        c = ident.get_credentials_for_identity(IdentityId=iid, Logins=logins)["Credentials"]
        self.lambda_ = boto3.client("lambda", region_name=REGION, aws_access_key_id=c["AccessKeyId"],
                                    aws_secret_access_key=c["SecretKey"], aws_session_token=c["SessionToken"])
        exp = c.get("Expiration")
        self.expires = exp.timestamp() if hasattr(exp, "timestamp") else time.time() + 3000

    def dashboard(self) -> dict:
        if not self.lambda_ or time.time() > self.expires - 300:
            self._login()
        r = self.lambda_.invoke(FunctionName=DASHBOARD_FN, Payload=json.dumps(
            {"userId": self.user_id, "clientType": "WEB_APP", "clientVersion": "0.2.3"}).encode())
        return json.loads(r["Payload"].read())


class WaterGuruAdapter(Adapter):
    source = "waterguru"

    def __init__(self, hub, cfg):
        super().__init__(hub, cfg)
        self.email, self.password = cfg.get("email") or "", cfg.get("password") or ""
        self.poll_seconds = max(120, int(cfg.get("poll_seconds") or 600))
        self.client = WaterGuruClient(self.email, self.password)

    async def start(self):
        if not (self.email and self.password):
            self.status = "needs email and password (Settings > WaterGuru)"
            return
        self.spawn("poll", self._poll_loop)

    def ingest(self, data: dict) -> None:
        for wb in (data or {}).get("waterBodies", []):
            values, attrs, specs = parse_waterbody(wb)
            did = self.dev_id(str(wb.get("waterBodyId")))
            if did not in self.hub.devices:
                caps = {k: cap(k, **({"unit": specs[k]["unit"]} if k in specs and specs[k]["unit"] else {}),
                               **({"label": specs[k]["label"]} if k in specs and specs[k]["label"] else {}))
                        for k in values}
                caps["status_text"] = cap("status_text", label="Status")
                if "orp" in caps:
                    caps["orp"].unit = "mV"
                if "cartridge_life" in caps:
                    caps["cartridge_life"].unit, caps["cartridge_life"].label = "%", "Cartridge"
                pod = ((wb.get("pods") or [{}])[0] or {}).get("pod") or {}
                self.hub.upsert_device(Device(id=did, name=wb.get("name") or "Pool", source=self.source,
                                              model=pod.get("product") or "WaterGuru SENSE", room="Pool",
                                              capabilities=caps))
            self.hub.set_attributes(did, attrs)
            self.hub.set_values(did, values)

    async def _poll_loop(self):
        while True:
            self.ingest(await asyncio.to_thread(self.client.dashboard))
            self.status = "ok"
            await asyncio.sleep(self.poll_seconds)


ADAPTER = WaterGuruAdapter
SETTINGS = {
    "id": "waterguru", "label": "WaterGuru", "icon": "science",
    "help": "Use the email and password you sign in to the WaterGuru app with. Haven reads the same pool dashboard "
            "the app shows: water temperature, pH, free chlorine and any other readings, plus cartridge and battery "
            "levels. WaterGuru tests a few times a day, so values change slowly.",
    "fields": [
        {"key": "email", "label": "Email", "type": "text"},
        {"key": "password", "label": "Password", "type": "password"},
        {"key": "poll_seconds", "label": "Refresh every (seconds)", "type": "number", "default": 600},
    ],
}
