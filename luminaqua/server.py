"""LuminAqua - local web controller for Week Aqua lights.

Model (matches the official app):
  * Daily timer  = the app's "Mode setting": manual mode + a colour + an on/off window + sunrise ramp.
                   The light runs it on its own. This is the normal mode.
  * Smooth curve = LuminAqua only: anchor points through the day; LuminAqua sends the interpolated
                   level every minute. The light's own window = the curve's span, so it still
                   switches off on time if the Pi stops.
  * Advanced     = the app's "Advanced setting": up to 8 periods stored on the light (optional).
  * Override     = LuminAqua only: on/off right now for N minutes, then put the normal mode back.

Run:  uvicorn server:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import List, Optional

from bleak import BleakClient, BleakScanner
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, validator  # pydantic 1.x (Python 3.7 on the Pi)

import protocol as P

BASE = Path(__file__).parent
STATE_FILE = BASE / "state.json"
WRITE_GAP = 0.5       # seconds between writes (the official app uses 500 ms)
IDLE_DISCONNECT = 45  # free the light for the phone app after this many idle seconds

log = logging.getLogger("luminaqua")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Defaults for a newly added light: green, 11 AM - 6 PM, no fade.
DEFAULT_TIMER = {"start": "11:00", "end": "18:00", "ramp": 0, "preset": "Green",
                 "ch": P.raw_to_pct(P.APP_PRESETS["Green"])}
# A starting smooth curve (sunrise, midday peak, sunset) in the user's usual colour.
DEFAULT_CURVE = {"mode": "single", "preset": "Green", "ch": P.raw_to_pct(P.APP_PRESETS["Green"]), "points": [
    {"t": "11:00", "pct": 0}, {"t": "12:30", "pct": 35}, {"t": "14:00", "pct": 65},
    {"t": "17:00", "pct": 65}, {"t": "18:30", "pct": 25}, {"t": "19:30", "pct": 0}]}


def _pt(t, pct, ch):
    return {"t": t, "pct": pct, "preset": None, "ch": list(ch)}


# Built-in starters (per-point colour): warm, UV-free sunrise/sunset into a low-UV daylight peak.
WARM = [75, 45, 18, 0]
DAYLIGHT = [65, 79, 74, 26]     # 99.8% of the power budget; x 65% peak -> UV ~17% effective
CRISP = [54, 71, 86, 34]        # 99.5% of the power budget; x 50% peak -> UV ~17% effective
CURVE_TEMPLATES = {
    "Balanced High-Tech ~65% Peak": {"mode": "per_point", "preset": None, "ch": DAYLIGHT, "points": [
        _pt("11:00", 0, WARM), _pt("12:00", 25, WARM), _pt("13:30", 65, DAYLIGHT),
        _pt("16:30", 65, DAYLIGHT), _pt("18:00", 25, WARM), _pt("19:00", 0, WARM)]},
    "Algae Recovery ~50% Peak": {"mode": "per_point", "preset": None, "ch": CRISP, "points": [
        _pt("12:00", 0, WARM), _pt("13:00", 20, WARM), _pt("14:00", 50, CRISP),
        _pt("17:00", 50, CRISP), _pt("18:00", 20, WARM), _pt("18:30", 0, WARM)]},
    "Simple Green (single colour)": DEFAULT_CURVE,
}
CURVE_STEP = 60       # seconds between live updates while the curve is changing
CURVE_IDLE = 150      # stay connected this long between curve updates (reconnecting is slow)
# Weak-signal hardening (-80 to -85 dBm). Writes to the light get no acknowledgement, so a dropped
# packet is silent: besides retrying on errors, LuminAqua re-sends the current level and the light's
# own off-window regularly, and sends an explicit "off" level once the curve has ended.
SEND_ATTEMPTS = 3     # tries per send (reconnecting between tries)
RETRY_DELAY = 2       # seconds between tries
CURVE_RETRY = 15      # if a whole send failed, try the curve step again this soon (not a minute later)
CURVE_REFRESH = 300   # re-send an unchanged level this often while the curve is on
WINDOW_REFRESH = 1800 # re-send the light's on/off window (the failsafe) this often


def default_light(name: str) -> dict:
    return {
        "name": name,
        "timer": json.loads(json.dumps(DEFAULT_TIMER)),
        "mode": "timer",    # "timer" | "curve" | "advanced"
        "curve": json.loads(json.dumps(DEFAULT_CURVE)),
        "advanced": [],     # [{start, end, ch}] up to 8
        "override": None,   # {"until": epoch, "on": bool, "ch": [...], "preset": str|None}
    }


DEFAULT_STATE = {
    "lights": {},           # mac -> light; added from the page ("+ Add light" scans for them)
    "presets": {"Moonlight": [0, 0, 8, 0], "Feeding": [60, 60, 60, 0]},
    "curve_profiles": {},   # name -> curve, saved by the user
}


def load_state() -> dict:
    state = json.loads(json.dumps(DEFAULT_STATE))
    if not STATE_FILE.exists():
        return state
    try:
        saved = json.loads(STATE_FILE.read_text())
    except Exception:  # noqa: BLE001
        log.exception("state.json unreadable, using defaults")
        return state
    state["presets"] = saved.get("presets", state["presets"])
    state["curve_profiles"] = saved.get("curve_profiles", {})
    for mac, old in (saved.get("lights") or {}).items():
        light = default_light(old.get("name") or mac)
        for k in light:
            if k in old:
                light[k] = old[k]
        # migrate from the version that kept one timer/mode for all lights
        if "timer" not in old and isinstance(saved.get("timer"), dict):
            light["timer"] = saved["timer"]
            light["mode"] = saved.get("mode", "timer")
            light["advanced"] = saved.get("advanced", [])
        state["lights"][mac.upper()] = light
    return state


def save_state():
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(STATE, indent=2))
    tmp.replace(STATE_FILE)


STATE = load_state()

# BlueZ allows only one scan/connect at a time, so those steps are serialised.
_radio_lock = None


def radio_lock() -> asyncio.Lock:
    global _radio_lock
    if _radio_lock is None:
        _radio_lock = asyncio.Lock()
    return _radio_lock


async def release_stale(mac: str):
    """Drop a connection BlueZ holds to this light that no client of ours owns."""
    with contextlib.suppress(Exception):
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", "disconnect", mac,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), 10)
        log.info("%s released stale BlueZ connection", mac)
        await asyncio.sleep(1)


# ---- BLE ------------------------------------------------------------------------
class Light:
    def __init__(self, mac: str):
        self.mac = mac
        self.client = None
        self._lock = None
        self.last_used = 0.0
        self.rssi = None
        self.last_seen = 0.0
        self.last_error = None
        self.last_ok = 0.0
        self.curve_raw = None    # curve level last sent (None = must resend)
        self.curve_sent = 0.0    # last curve attempt (paces the loop)
        self.level_sent = 0.0    # last successful level write
        self.window_sent = 0.0   # last successful on/off-window write

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    async def _connect(self):
        if self.connected:
            return
        async with radio_lock():
            dev = await BleakScanner.find_device_by_address(self.mac, timeout=12)
            if dev is None:
                await release_stale(self.mac)
                dev = await BleakScanner.find_device_by_address(self.mac, timeout=8)
            if dev is None:
                raise RuntimeError("not found (out of range, or the phone app is connected)")
            client = BleakClient(dev, timeout=20, disconnected_callback=lambda _c: log.info("%s disconnected", self.mac))
            await client.connect()
        self.client = client
        log.info("%s connected", self.mac)

    async def send(self, cmds: list):
        """Connect if needed and write; on any Bluetooth error reconnect and retry (SEND_ATTEMPTS)."""
        async with self.lock:
            last_exc = None
            for attempt in range(SEND_ATTEMPTS):
                try:
                    await self._connect()
                    for c in cmds:
                        await self.client.write_gatt_char(P.WRITE_CHAR, c, response=False)
                        log.info("%s -> %s", self.mac, c.hex())
                        await asyncio.sleep(WRITE_GAP)
                    self.last_used = self.last_ok = time.time()
                    self.last_error = None
                    return
                except Exception as e:  # noqa: BLE001
                    last_exc = e
                    log.warning("%s attempt %d failed: %s", self.mac, attempt + 1, e)
                    await self._drop()
                    if attempt < SEND_ATTEMPTS - 1:
                        await asyncio.sleep(RETRY_DELAY)
            self.last_error = str(last_exc)
            raise RuntimeError(f"{self.mac}: {last_exc}")

    async def _drop(self):
        if self.client:
            with contextlib.suppress(Exception):
                await self.client.disconnect()
        self.client = None

    async def idle_check(self, idle: float = IDLE_DISCONNECT):
        if self.connected and not self.lock.locked() and time.time() - self.last_used > idle:
            async with self.lock:
                await self._drop()


LIGHTS = {mac: Light(mac) for mac in STATE["lights"]}


def resolve(targets) -> list:
    macs = targets or list(LIGHTS)
    unknown = [m for m in macs if m not in LIGHTS]
    if unknown:
        raise HTTPException(404, f"unknown light(s): {unknown}")
    return macs


async def send_each(per_light: dict) -> dict:
    """per_light: {mac: [commands]} - each light gets its own commands, in parallel."""
    if not per_light:
        return {}
    macs = list(per_light)
    results = await asyncio.gather(*(LIGHTS[m].send(per_light[m]) for m in macs), return_exceptions=True)
    out = {m: ("ok" if r is None else str(r)) for m, r in zip(macs, results)}
    if all(v != "ok" for v in out.values()):
        raise HTTPException(502, out)
    return out


async def send_to(targets, cmds: list) -> dict:
    return await send_each({m: cmds for m in resolve(targets)})


def light_state(mac: str) -> dict:
    return STATE["lights"][mac]


def normal_cmds(mac: str) -> list:
    """What this light should run when it has no override."""
    s = light_state(mac)
    LIGHTS[mac].curve_raw = None  # anything sent now may change the level; recompute the curve
    if s["mode"] == "curve":
        now = dt.datetime.now()
        L = LIGHTS[mac]
        L.curve_raw = P.curve_raw(s["curve"], P.minute_of_day(now))
        L.curve_sent = L.level_sent = L.window_sent = time.time()
        return P.curve_start(s["curve"], now)
    return P.daily_timer(s["timer"]) if s["mode"] == "timer" else P.advanced_mode()


def curve_active(mac: str) -> bool:
    """Curve mode, no override, and inside the curve's span (the light is off outside it)."""
    s = light_state(mac)
    if s["mode"] != "curve" or s["override"]:
        return False
    start, end = P.curve_window(s["curve"])
    return P._mins(start) <= P.minute_of_day() <= P._mins(end)


async def curve_tick():
    """Once a minute per curve light: send the interpolated level when it changed (or is due a
    refresh), re-send the failsafe window when due, and send 0 once after the curve ends."""
    due = {}
    now = time.time()
    for mac in list(STATE["lights"]):
        s, L = light_state(mac), LIGHTS[mac]
        if s["mode"] != "curve" or s["override"] or L.lock.locked() or now - L.curve_sent < CURVE_STEP:
            continue
        raw = P.curve_raw(s["curve"], P.minute_of_day())   # (0,0,0,0) outside the curve
        cmds, sent_level = [], False
        if raw != L.curve_raw or (curve_active(mac) and now - L.level_sent >= CURVE_REFRESH):
            cmds.append(P.raw_levels(raw))
            sent_level = True
        if curve_active(mac) and now - L.window_sent >= WINDOW_REFRESH:
            cmds.append(P.manual_window(*P.curve_window(s["curve"]), 0))
        if cmds:
            due[mac] = (raw, cmds, sent_level)
    if not due:
        return
    results = await asyncio.gather(*(LIGHTS[m].send(c) for m, (_r, c, _l) in due.items()),
                                   return_exceptions=True)
    for (m, (raw, cmds, sent_level)), res in zip(due.items(), results):
        L, t = LIGHTS[m], time.time()
        if res is None:
            L.curve_sent = t
            if sent_level:
                L.curve_raw, L.level_sent = raw, t
            if len(cmds) > sent_level:
                L.window_sent = t
        else:  # every retry failed: try this step again soon instead of skipping a whole minute
            L.curve_sent = t - CURVE_STEP + CURVE_RETRY
            log.warning("%s curve step failed, retrying in %ss: %s", m, CURVE_RETRY, res)


async def end_override(macs: list) -> dict:
    for m in macs:
        light_state(m)["override"] = None
    save_state()
    return await send_each({m: normal_cmds(m) for m in macs})


# ---- background jobs ------------------------------------------------------------
async def background():
    last_sync_day = None
    last_scan = 0.0
    while True:
        try:
            for mac, L in list(LIGHTS.items()):
                await L.idle_check(CURVE_IDLE if curve_active(mac) else IDLE_DISCONNECT)
            await curve_tick()
            expired = [m for m, s in STATE["lights"].items()
                       if s.get("override") and time.time() >= s["override"]["until"]]
            if expired:
                log.info("override expired on %s, restoring normal mode", expired)
                with contextlib.suppress(Exception):
                    await end_override(expired)
            now = dt.datetime.now()
            if now.hour == 3 and last_sync_day != now.date():  # keep the lights' clocks accurate
                last_sync_day = now.date()
                with contextlib.suppress(Exception):
                    await send_to(None, P.time_sync())
            if (time.time() - last_scan > 60 and not radio_lock().locked()
                    and not any(L.lock.locked() or L.connected for L in list(LIGHTS.values()))):
                last_scan = time.time()
                with contextlib.suppress(Exception):
                    async with radio_lock():
                        found = await BleakScanner.discover(timeout=5, return_adv=True)
                    for addr, (_d, adv) in found.items():
                        if addr.upper() in LIGHTS:
                            LIGHTS[addr.upper()].rssi = adv.rssi
                            LIGHTS[addr.upper()].last_seen = time.time()
        except Exception:  # noqa: BLE001
            log.exception("background loop")
        await asyncio.sleep(5)


@contextlib.asynccontextmanager
async def lifespan(_app):
    async def initial_sync():
        # Free the lights from any Bluetooth link left over from a previous run.
        for m in list(LIGHTS):
            await release_stale(m)
        # Curve lights also get today's level and window again (e.g. after a Pi reboot).
        with contextlib.suppress(Exception):
            await send_each({m: normal_cmds(m) if light_state(m)["mode"] == "curve" and not light_state(m)["override"]
                             else P.time_sync() for m in LIGHTS})

    task = asyncio.create_task(background())
    asyncio.create_task(initial_sync())
    yield
    task.cancel()
    for L in LIGHTS.values():
        await L._drop()


app = FastAPI(title="LuminAqua", lifespan=lifespan)


# ---- API models -----------------------------------------------------------------
def _check_ch(v):
    if any(not 0 <= x <= 100 for x in v):
        raise ValueError("channel values must be 0-100")
    return v


def _check_time(v):
    P._hm(v)  # raises ValueError on bad input
    return v


class Timer(BaseModel):
    start: str
    end: str
    ramp: int = Field(0, ge=0, le=len(P.RAMP_HOURS) - 1)
    preset: Optional[str] = None
    ch: List[float] = Field(..., min_items=4, max_items=4)
    _c = validator("ch", allow_reuse=True)(_check_ch)
    _t = validator("start", "end", allow_reuse=True)(_check_time)

    @validator("preset")
    def _known(cls, v):  # noqa: N805
        return v if v in P.APP_PRESETS else None


class TimerReq(Timer):
    targets: Optional[List[str]] = None


class Targets(BaseModel):
    targets: Optional[List[str]] = None


class Override(BaseModel):
    on: bool = True
    minutes: int = Field(..., ge=1, le=24 * 60)
    ch: List[float] = Field([0, 0, 0, 0], min_items=4, max_items=4)
    preset: Optional[str] = None
    targets: Optional[List[str]] = None
    _c = validator("ch", allow_reuse=True)(_check_ch)


class Slot(BaseModel):
    start: str
    end: str
    ch: List[float] = Field(..., min_items=4, max_items=4)
    _c = validator("ch", allow_reuse=True)(_check_ch)
    _t = validator("start", "end", allow_reuse=True)(_check_time)


class Advanced(BaseModel):
    slots: List[Slot] = Field(..., max_items=P.SLOTS)
    targets: Optional[List[str]] = None


def _factory_or_none(v):
    return v if v in P.APP_PRESETS else None


class Point(BaseModel):
    t: str
    pct: float = Field(..., ge=0, le=100)
    # Per-point colour (used in "per_point" mode). Factory preset name, or None for custom/saved %.
    preset: Optional[str] = None
    ch: Optional[List[float]] = Field(None, min_items=4, max_items=4)
    _t = validator("t", allow_reuse=True)(_check_time)
    _p = validator("preset", allow_reuse=True)(_factory_or_none)

    @validator("ch")
    def _ch(cls, v):  # noqa: N805
        return v if v is None else _check_ch(v)


class CurveData(BaseModel):
    mode: str = Field("single", regex="^(single|per_point)$")
    points: List[Point] = Field(..., min_items=2, max_items=24)
    preset: Optional[str] = None
    ch: List[float] = Field(..., min_items=4, max_items=4)
    _c = validator("ch", allow_reuse=True)(_check_ch)
    _p = validator("preset", allow_reuse=True)(_factory_or_none)

    @validator("points")
    def _ordered(cls, v):  # noqa: N805
        mins = [P._mins(p.t) for p in v]
        if any(b <= a for a, b in zip(mins, mins[1:])):
            raise ValueError("points must be in time order, each later than the one before")
        return v


class Curve(CurveData):
    targets: Optional[List[str]] = None


class CurveProfile(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    curve: CurveData


class Preset(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    ch: List[float] = Field(..., min_items=4, max_items=4)
    _c = validator("ch", allow_reuse=True)(_check_ch)


class Rename(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)


# ---- API --------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


ICONS = ("favicon.png", "apple-touch-icon.png", "icon-192.png")


@app.get("/favicon.ico")
def favicon_ico():
    return FileResponse(BASE / "static" / "favicon.png", media_type="image/png")


@app.get("/{name}.png")
def icon(name: str):
    if f"{name}.png" not in ICONS:
        raise HTTPException(404)
    return FileResponse(BASE / "static" / f"{name}.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/state")
def get_state():
    lights = []
    for mac, s in STATE["lights"].items():
        L = LIGHTS[mac]
        lights.append({
            "mac": mac, "name": s["name"], "connected": L.connected,
            "rssi": L.rssi, "last_seen": L.last_seen or None,
            "last_ok": L.last_ok or None, "last_error": L.last_error,
            "timer": s["timer"], "mode": s["mode"], "advanced": s["advanced"], "override": s["override"],
            "curve": s["curve"],
            "curve_now": round(P.curve_pct(s["curve"]["points"], P.minute_of_day()), 1),
            "curve_now_ch": P.raw_pct(P.curve_raw(s["curve"], P.minute_of_day())),
            "curve_peak": max(float(p["pct"]) for p in s["curve"]["points"]),
        })
    return {
        "lights": lights, "presets": STATE["presets"],
        "app_presets": {k: P.raw_to_pct(v) for k, v in P.APP_PRESETS.items()},
        "app_presets_raw": {k: list(v) for k, v in P.APP_PRESETS.items()}, "max_level": P.MAX_LEVEL,
        "power_weights": list(P.POWER_WEIGHTS), "max_power": P.MAX_POWER,
        "curve_templates": CURVE_TEMPLATES, "curve_profiles": STATE["curve_profiles"],
        "ramp_hours": P.RAMP_HOURS, "now": time.time(),
    }


@app.post("/api/timer")
async def set_timer(body: TimerReq):
    """Save the daily timer for the chosen lights and switch them to it.
    A light with an override running keeps it; the timer takes over when it ends."""
    macs = resolve(body.targets)
    timer = body.dict(exclude={"targets"})
    for m in macs:
        light_state(m)["timer"] = json.loads(json.dumps(timer))
        light_state(m)["mode"] = "timer"
    save_state()
    now = [m for m in macs if not light_state(m)["override"]]
    res = await send_each({m: normal_cmds(m) for m in now})
    waiting = [light_state(m)["name"] for m in macs if m not in now]
    if waiting:
        res["note"] = f"Saved. {', '.join(waiting)} keeps its temporary setting until it ends."
    return res


@app.post("/api/curve")
async def set_curve(body: Curve):
    """Save a smooth day curve for the chosen lights and switch them to it.
    LuminAqua then sends the interpolated level every minute while it changes."""
    macs = resolve(body.targets)
    curve = json.loads(json.dumps(body.dict(exclude={"targets"})))
    for m in macs:
        light_state(m)["curve"] = json.loads(json.dumps(curve))
        light_state(m)["mode"] = "curve"
    save_state()
    now = [m for m in macs if not light_state(m)["override"]]
    res = await send_each({m: normal_cmds(m) for m in now})
    waiting = [light_state(m)["name"] for m in macs if m not in now]
    if waiting:
        res["note"] = f"Saved. {', '.join(waiting)} keeps its temporary setting until it ends."
    return res


@app.post("/api/override")
async def override(body: Override):
    macs = resolve(body.targets)
    ch = body.ch if body.on else [0, 0, 0, 0]
    preset = body.preset if body.on else None
    res = await send_to(macs, P.force(ch, preset))
    until = time.time() + body.minutes * 60
    for m in macs:
        if res.get(m) == "ok":
            light_state(m)["override"] = {"until": until, "on": body.on, "ch": ch, "preset": preset}
    save_state()
    return res


@app.post("/api/normal")
async def back_to_normal(body: Targets):
    return await end_override(resolve(body.targets))


@app.post("/api/advanced")
async def set_advanced(body: Advanced):
    macs = resolve(body.targets)
    slots = [s.dict() for s in body.slots]
    res = await send_to(macs, [*P.time_sync(), *P.write_schedule(slots), P.mode_auto()])
    for m in macs:
        if res.get(m) == "ok":
            s = light_state(m)
            s.update(advanced=json.loads(json.dumps(slots)), mode="advanced", override=None)
    save_state()
    return res


@app.post("/api/curve_profiles")
def save_curve_profile(p: CurveProfile):
    STATE["curve_profiles"][p.name.strip()] = json.loads(json.dumps(p.curve.dict()))
    save_state()
    return STATE["curve_profiles"]


@app.delete("/api/curve_profiles/{name}")
def delete_curve_profile(name: str):
    STATE["curve_profiles"].pop(name, None)
    save_state()
    return STATE["curve_profiles"]


@app.post("/api/presets")
def save_preset(p: Preset):
    STATE["presets"][p.name] = p.ch
    save_state()
    return STATE["presets"]


@app.delete("/api/presets/{name}")
def delete_preset(name: str):
    STATE["presets"].pop(name, None)
    save_state()
    return STATE["presets"]


WEK_COMPANY_ID = 0x4657   # advert manufacturer id (raw bytes 57 46, "WF") of the lights this protocol drives
# Other Week Aqua product families, by advert id (raw bytes 57 xx). The official app drives each with a
# different screen and command set, so LuminAqua can't control them (yet).
OTHER_WEEK_IDS = {
    0x4557: "older HJ-series light", 0x4757: "4-series light", 0x4857: "5-series light",
    0x4957: "6-series light", 0x5057: "smart plug", 0x5157: "8-series device", 0x5257: "9-series light",
}


@app.get("/api/scan")
async def scan(seconds: float = 8):
    """Look for nearby lights. Compatible (WEK family) ones first; other Week Aqua products are listed
    as not supported. A light that is connected to a phone app does not show up until the app lets go."""
    seconds = max(3.0, min(20.0, seconds))
    for L in list(LIGHTS.values()):          # our own links stop lights advertising
        if not L.lock.locked():
            async with L.lock:
                await L._drop()
    async with radio_lock():
        found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    out = []
    for addr, (dev, adv) in found.items():
        ids = set((adv.manufacturer_data or {}).keys())
        kind = None
        if WEK_COMPANY_ID in ids:
            support = "supported"
        elif ids & OTHER_WEEK_IDS.keys():
            support, kind = "unsupported", OTHER_WEEK_IDS[min(ids & OTHER_WEEK_IDS.keys())]
        else:
            continue
        mac = addr.upper()
        if mac in LIGHTS:
            LIGHTS[mac].rssi, LIGHTS[mac].last_seen = adv.rssi, time.time()
        out.append({"mac": mac, "name": adv.local_name or dev.name or "Week Aqua light", "rssi": adv.rssi,
                    "support": support, "kind": kind, "added": mac in LIGHTS})
    out.sort(key=lambda d: (d["support"] != "supported", -(d["rssi"] or -999)))
    return {"devices": out}


MAC_RE = r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$"


class NewLight(BaseModel):
    mac: str = Field(..., regex=MAC_RE)
    name: str = Field(..., min_length=1, max_length=40)


@app.post("/api/lights")
async def add_light(body: NewLight):
    """Add a light. It starts on the default daily timer (sent now, so the light and LuminAqua agree)."""
    mac = body.mac.upper()
    if mac in STATE["lights"]:
        raise HTTPException(409, f"{STATE['lights'][mac]['name']} is already added")
    STATE["lights"][mac] = default_light(body.name.strip())
    LIGHTS[mac] = Light(mac)
    save_state()
    try:
        res = await send_each({mac: normal_cmds(mac)})
    except HTTPException as e:  # saved anyway; the user can retry from the page
        res = {mac: str(e.detail.get(mac) if isinstance(e.detail, dict) else e.detail),
               "note": f"Added {body.name}, but couldn't reach it yet - save a schedule once it's in range."}
    return res


@app.delete("/api/lights/{mac}")
async def remove_light(mac: str):
    """Forget a light (the light keeps running whatever it was last sent)."""
    mac = mac.upper()
    if mac not in STATE["lights"]:
        raise HTTPException(404)
    L = LIGHTS.pop(mac)
    STATE["lights"].pop(mac)
    save_state()
    with contextlib.suppress(Exception):
        await L._drop()
    return {"ok": True}


@app.post("/api/lights/{mac}/name")
def rename(mac: str, body: Rename):
    mac = mac.upper()
    if mac not in STATE["lights"]:
        raise HTTPException(404)
    STATE["lights"][mac]["name"] = body.name.strip()
    save_state()
    return {"ok": True}
