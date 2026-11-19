"""Offline API test: Bluetooth is replaced by a recorder. Run: python test_api.py"""
import json
import tempfile

import server
from fastapi.testclient import TestClient

L900, P6000 = "AA:BB:CC:00:00:01", "AA:BB:CC:00:00:02"   # made-up addresses
SENT = []  # list of {mac: [hex...]}


async def fake_send_each(per_light):
    SENT.append({m: [c.hex() for c in cmds] for m, cmds in per_light.items()})
    return {m: "ok" for m in per_light}

server.send_each = fake_send_each
c = TestClient(server.app)  # no `with`: lifespan (real Bluetooth) is not started


def strip_sync(cmds):
    return cmds[2:] if cmds and cmds[0].startswith("ff") else cmds


def last(mac):
    return strip_sync(SENT[-1][mac])


def light(mac):
    return next(l for l in c.get("/api/state").json()["lights"] if l["mac"] == mac)


GREEN_11_18 = ["fdf1555555555555", "fbf96ce373eb5555", "fef9110018000000"]
TIMER = {"start": "11:00", "end": "18:00", "ramp": 0, "preset": "Green", "ch": [46, 97, 49, 100]}

# --- migration from the single-timer state.json ------------------------------------
tmp = server.Path(tempfile.mkdtemp())
server.STATE_FILE = tmp / "state.json"
server.STATE_FILE.write_text(json.dumps({
    "lights": {L900: {"name": "Left"}, P6000: {"name": "P6000pro"}},
    "timer": {"start": "10:00", "end": "20:00", "ramp": 1, "preset": "Red", "ch": [71, 79, 49, 100]},
    "mode": "timer", "advanced": [], "presets": {"X": [1, 2, 3, 4]}, "override": None}))
st = server.load_state()
assert st["lights"][L900]["name"] == "Left" and st["lights"][L900]["timer"]["preset"] == "Red", st
assert st["lights"][P6000]["timer"]["start"] == "10:00" and st["presets"] == {"X": [1, 2, 3, 4]}
server.STATE_FILE = tmp / "fresh.json"
server.STATE = server.load_state()

# --- a fresh install has no lights; add them like the page does ------------------------------
assert c.get("/api/state").json()["lights"] == []
assert server.LIGHTS == {} or all(m not in server.STATE["lights"] for m in server.LIGHTS)
server.LIGHTS.clear()
for m, n in ((L900, "L900pro"), (P6000, "P6000pro")):
    r = c.post("/api/lights", json={"mac": m.lower(), "name": n})
    assert r.status_code == 200, r.text
    assert set(SENT[-1]) == {m} and strip_sync(SENT[-1][m]) == GREEN_11_18   # starts on the default timer
assert c.post("/api/lights", json={"mac": L900, "name": "again"}).status_code == 409
assert c.post("/api/lights", json={"mac": "not-a-mac", "name": "x"}).status_code == 422
assert [l["name"] for l in c.get("/api/state").json()["lights"]] == ["L900pro", "P6000pro"]
assert set(server.load_state()["lights"]) == {L900, P6000}            # saved
# rename / remove / re-add
assert c.post(f"/api/lights/{L900.lower()}/name", json={"name": " 40g Breeder "}).status_code == 200
assert light(L900)["name"] == "40g Breeder"
c.post(f"/api/lights/{L900}/name", json={"name": "L900pro"})
assert c.delete("/api/lights/" + P6000).status_code == 200 and P6000 not in server.LIGHTS
assert [l["mac"] for l in c.get("/api/state").json()["lights"]] == [L900]
assert c.delete("/api/lights/" + P6000).status_code == 404
c.post("/api/lights", json={"mac": P6000, "name": "P6000pro"})

# --- defaults ---------------------------------------------------------------------------
for m in (L900, P6000):
    l = light(m)
    assert l["timer"]["preset"] == "Green" and l["timer"]["start"] == "11:00" and l["mode"] == "timer"

# --- timer for both = exactly the official app's bytes ----------------------------------
assert c.post("/api/timer", json=TIMER).status_code == 200
assert set(SENT[-1]) == {L900, P6000} and last(L900) == GREEN_11_18 and last(P6000) == GREEN_11_18

# --- timer for ONE light only -------------------------------------------------------
r = c.post("/api/timer", json={**TIMER, "start": "09:30", "end": "24:00", "ramp": 2, "preset": None,
                               "ch": [100, 0, 50, 0], "targets": [P6000]})
assert r.status_code == 200, r.text
assert set(SENT[-1]) == {P6000}, SENT[-1]
assert last(P6000) == ["fdf1555555555555", "fbf9eb0076005555", "fef9093024000002"], last(P6000)
assert light(L900)["timer"]["start"] == "11:00" and light(P6000)["timer"]["start"] == "09:30"

# --- override one light; the other is untouched ------------------------------------------
c.post("/api/override", json={"on": True, "minutes": 30, "ch": [46, 97, 49, 100], "preset": "Green", "targets": [L900]})
assert set(SENT[-1]) == {L900} and last(L900) == ["fdf1555555555555", "fbf96ce373eb5555", "fef9000024000000"]
assert light(L900)["override"]["on"] is True and light(P6000)["override"] is None

# saving a timer for both while L900 has an override: only P6000 is sent to now
r = c.post("/api/timer", json=TIMER).json()
assert set(SENT[-1]) == {P6000} and "note" in r, (SENT[-1], r)

# back to normal for L900 restores ITS timer
c.post("/api/normal", json={"targets": [L900]})
assert set(SENT[-1]) == {L900} and last(L900) == GREEN_11_18 and light(L900)["override"] is None

# --- override expiry in the background loop restores only the expired light ---------
c.post("/api/override", json={"on": False, "minutes": 5, "targets": [P6000]})
assert last(P6000) == ["fdf1555555555555", "fbf9000000005555", "fef9000024000000"]
server.STATE["lights"][P6000]["override"]["until"] = 0
import asyncio
expired = [m for m, s in server.STATE["lights"].items() if s.get("override") and s["override"]["until"] <= 0]
asyncio.run(server.end_override(expired))
assert set(SENT[-1]) == {P6000} and last(P6000) == GREEN_11_18

# --- advanced on one light -----------------------------------------------------------
c.post("/api/advanced", json={"slots": [{"start": "10:00", "end": "12:00", "ch": [100, 100, 100, 100]}], "targets": [L900]})
assert set(SENT[-1]) == {L900} and SENT[-1][L900][-1] == "fdf2555555555555"
assert light(L900)["mode"] == "advanced" and light(P6000)["mode"] == "timer"
c.post("/api/timer", json={**TIMER, "targets": [L900]})
assert light(L900)["mode"] == "timer"

# --- validation ------------------------------------------------------------------------
assert c.post("/api/timer", json={**TIMER, "start": "25:00"}).status_code == 422
assert c.post("/api/timer", json={**TIMER, "ramp": 9}).status_code == 422
assert c.post("/api/override", json={"on": True, "minutes": 0}).status_code == 422
assert c.post("/api/timer", json={**TIMER, "targets": ["AA:BB"]}).status_code == 404

# --- smooth curve -----------------------------------------------------------------------
import datetime as dt
P = server.P
CURVE = {"preset": "Green", "ch": [46, 97, 49, 100], "points": [
    {"t": "11:00", "pct": 0}, {"t": "12:30", "pct": 35}, {"t": "14:00", "pct": 65},
    {"t": "17:00", "pct": 65}, {"t": "18:30", "pct": 25}, {"t": "19:30", "pct": 0}]}
pts = CURVE["points"]
assert P.curve_pct(pts, 10 * 60) == 0 and P.curve_pct(pts, 20 * 60) == 0      # outside the span
assert P.curve_pct(pts, 11 * 60 + 45) == 17.5                                  # halfway up the first ramp
assert P.curve_pct(pts, 15 * 60) == 65 and abs(P.curve_pct(pts, 19 * 60) - 12.5) < 1e-9
assert P.curve_raw(CURVE, 15 * 60) == tuple(round(v * .65) for v in P.APP_PRESETS["Green"])
# minute-to-minute steps are tiny (no visible jumps): max change per minute on this curve
steps = [max(abs(a - b) for a, b in zip(P.curve_raw(CURVE, m), P.curve_raw(CURVE, m + 1))) for m in range(660, 1170)]
assert max(steps) <= 2, max(steps)
noon = dt.datetime(2026, 11, 18, 11, 45)
cmds = [x.hex() for x in P.curve_start(CURVE, noon)]
g = P.curve_raw(CURVE, 11 * 60 + 45)
assert cmds[2:] == ["fdf1555555555555", P.raw_levels(g).hex(), "fef9110019300000"], cmds  # window 11:00-19:30

r = c.post("/api/curve", json={**CURVE, "targets": [L900]})
assert r.status_code == 200, r.text
assert set(SENT[-1]) == {L900} and SENT[-1][L900][4].startswith("fef91100193000")
assert light(L900)["mode"] == "curve" and light(P6000)["mode"] == "timer"
assert server.load_state()["lights"][L900]["curve"]["points"][-1]["t"] == "19:30"   # persisted

# the background tick sends only a level command, only when it changed, at most once a minute
ticks = []
FAIL = {"on": False}
async def rec_send(self, cmds):
    if FAIL["on"]:
        raise RuntimeError("simulated drop")
    ticks.append((self.mac, [x.hex() for x in cmds]))
ORIG_SEND = server.Light.send
server.Light.send = rec_send
L = server.LIGHTS[L900]
real_mod, real_time = P.minute_of_day, server.time.time
clock = {"t": real_time()}
server.time.time = lambda: clock["t"]
P.minute_of_day = lambda now=None: 12 * 60          # pretend it's noon
L.curve_sent = 0; L.window_sent = clock["t"]; L.level_sent = 0
asyncio.run(server.curve_tick())
assert len(ticks) == 1 and ticks[0][0] == L900 and len(ticks[0][1]) == 1 and ticks[0][1][0].startswith("fbf9")
asyncio.run(server.curve_tick())                     # within a minute: nothing
assert len(ticks) == 1
clock["t"] += 61
asyncio.run(server.curve_tick())                     # a minute later, same level: nothing
assert len(ticks) == 1
clock["t"] += server.CURVE_REFRESH
asyncio.run(server.curve_tick())                     # unchanged level is re-sent every 5 min
assert len(ticks) == 2 and ticks[-1][1] == ticks[0][1]
clock["t"] += server.WINDOW_REFRESH
asyncio.run(server.curve_tick())                     # failsafe window re-sent every 30 min
assert ticks[-1][1][-1].startswith("fef91100193000"), ticks[-1]

# a failed send (all retries) is tried again 15 s later, not a minute later
P.minute_of_day = lambda now=None: 12 * 60 + 30
FAIL["on"] = True; clock["t"] += 61; n = len(ticks)
asyncio.run(server.curve_tick())
assert len(ticks) == n
FAIL["on"] = False; clock["t"] += server.CURVE_RETRY + 1
asyncio.run(server.curve_tick())
assert len(ticks) == n + 1 and ticks[-1][1][0].startswith("fbf9")

# after the curve ends: one explicit "off" level (backup for the light's own window), then quiet
P.minute_of_day = lambda now=None: 21 * 60
clock["t"] += 61
asyncio.run(server.curve_tick())
assert ticks[-1][1] == ["fbf9000000005555"], ticks[-1]
n = len(ticks)
for _ in range(3):
    clock["t"] += server.WINDOW_REFRESH
    asyncio.run(server.curve_tick())
assert len(ticks) == n                               # nothing more overnight
P.minute_of_day, server.time.time = real_mod, real_time

# an override pauses the curve; ending it re-sends the curve start
c.post("/api/override", json={"on": True, "minutes": 5, "ch": [10, 10, 10, 10], "targets": [L900]})
assert not server.curve_active(L900)
asyncio.run(server.end_override([L900]))
assert SENT[-1][L900][2] == "fdf1555555555555" and SENT[-1][L900][4].startswith("fef91100193000")

# validation: order, range, count
bad = [{"t": "12:00", "pct": 10}, {"t": "11:00", "pct": 0}]
assert c.post("/api/curve", json={**CURVE, "points": bad}).status_code == 422
assert c.post("/api/curve", json={**CURVE, "points": [{"t": "11:00", "pct": 0}]}).status_code == 422
assert c.post("/api/curve", json={**CURVE, "points": [{"t": "11:00", "pct": 0}, {"t": "12:00", "pct": 120}]}).status_code == 422
c.post("/api/timer", json={**TIMER, "targets": [L900]})
assert light(L900)["mode"] == "timer"

# --- custom colour (single mode): effective = colour x brightness ---------------------------
CUSTOM = {"mode": "single", "preset": None, "ch": [80, 60, 40, 20], "points": [
    {"t": "10:00", "pct": 0}, {"t": "12:00", "pct": 50}, {"t": "14:00", "pct": 50}]}
assert P.curve_raw(CUSTOM, 13 * 60) == tuple(round(P.level(v) * .5) for v in [80, 60, 40, 20])

# --- per-point colour: each channel interpolated separately between effective anchors -------
WARM, DAY = [80, 40, 10, 0], [60, 80, 80, 30]
PER = {"mode": "per_point", "preset": None, "ch": DAY, "points": [
    {"t": "10:00", "pct": 0, "ch": WARM}, {"t": "11:00", "pct": 40, "ch": WARM},
    {"t": "13:00", "pct": 60, "ch": DAY}, {"t": "18:00", "pct": 0, "ch": WARM}]}
a = [P.level(v) * .4 for v in WARM]; b = [P.level(v) * .6 for v in DAY]
assert P.curve_raw(PER, 12 * 60) == tuple(round((x + y) / 2) for x, y in zip(a, b))   # halfway 11:00-13:00
assert P.curve_raw(PER, 11 * 60)[3] == 0                                              # warm sunrise: no UV
assert P.curve_raw(PER, 13 * 60)[3] == round(P.level(30) * .6)                        # UV at the peak
# a point without its own colour falls back to the curve colour; single mode ignores point colours
assert P.point_color({**PER, "points": []}, {"t": "12:00", "pct": 50}) == tuple(P.level(v) for v in DAY)
assert P.point_color({**PER, "mode": "single"}, PER["points"][0]) == tuple(P.level(v) for v in DAY)
# factory preset on a point stays byte-exact
assert P.point_color(PER, {"t": "1:00", "pct": 1, "preset": "Red", "ch": [0, 0, 0, 0]}) == P.APP_PRESETS["Red"]

r = c.post("/api/curve", json={**PER, "targets": [P6000]})
assert r.status_code == 200, r.text
l6 = light(P6000)
assert l6["mode"] == "curve" and l6["curve"]["mode"] == "per_point" and l6["curve"]["points"][1]["ch"] == WARM
assert l6["curve_peak"] == 60 and len(l6["curve_now_ch"]) == 4
assert light(L900)["mode"] == "timer"            # per-light independence
assert c.post("/api/curve", json={**PER, "mode": "rainbow"}).status_code == 422
assert c.post("/api/curve", json={**PER, "points": [{"t": "10:00", "pct": 0, "ch": [1, 2]}, {"t": "11:00", "pct": 1}]}).status_code == 422

# --- every built-in template is valid and keeps UV low -------------------------------------------
st = c.get("/api/state").json()
assert set(st["app_presets_raw"]) == set(P.APP_PRESETS) and st["curve_templates"]
for name, tpl in st["curve_templates"].items():
    assert c.post("/api/curve", json={**tpl, "targets": [P6000]}).status_code == 200, name
    if tpl["mode"] == "per_point":
        uv_peak = max(P.anchor_raw(tpl, p)[3] for p in tpl["points"]) / P.MAX_LEVEL * 100
        assert 12 <= uv_peak <= 21, (name, uv_peak)

# --- named profiles: save, survive a reload, delete ------------------------------------------
assert c.post("/api/curve_profiles", json={"name": "40g Breeder - Balanced", "curve": PER}).status_code == 200
assert "40g Breeder - Balanced" in server.load_state()["curve_profiles"]
assert c.get("/api/state").json()["curve_profiles"]["40g Breeder - Balanced"]["points"][2]["ch"] == DAY
c.delete("/api/curve_profiles/40g Breeder - Balanced")
assert c.get("/api/state").json()["curve_profiles"] == {}
assert c.post("/api/curve_profiles", json={"name": "", "curve": PER}).status_code == 422

# --- Light.send itself: weak signal -> up to 3 tries, reconnecting between them ---------------------
class FakeClient:
    is_connected = True
    def __init__(self): self.w = []
    async def write_gatt_char(self, ch, data, response=False): self.w.append(data.hex())
    async def disconnect(self): pass
fails = {"n": 2}
async def flaky_connect(self):
    if fails["n"]:
        fails["n"] -= 1
        raise RuntimeError("weak signal")
    if not self.client:
        self.client = FakeClient()
server.Light._connect = flaky_connect
server.RETRY_DELAY = 0; server.WRITE_GAP = 0
tl = server.Light(P6000)
asyncio.run(ORIG_SEND(tl, [P.raw_levels((1, 2, 3, 4))]))        # fails twice, third try works
assert tl.client.w == ["fbf9010203045555"] and tl.last_error is None
fails["n"] = 3
tl2 = server.Light(P6000)
try:
    asyncio.run(ORIG_SEND(tl2, [P.raw_levels((1, 2, 3, 4))]))   # three failures -> error reported
    raise AssertionError("should have failed")
except RuntimeError as e:
    assert "weak signal" in str(e) and tl2.last_error == "weak signal"

assert c.get("/").status_code == 200

# ---- power budget (official app: R .39, G .41, B .53, UV .11; custom colours max 100 %)
import protocol as PP
for name in PP.APP_PRESETS:                      # factory presets are sent byte-exact, untouched
    assert PP.preset_raw(name) == PP.color_cmd([0, 0, 0, 0], name)
    assert PP.power_pct(PP.raw_to_pct(PP.APP_PRESETS[name])) <= 102.5, name
full = PP.levels([100, 100, 100, 100])           # 144 % asked for -> scaled down, colour kept
assert PP.power_pct([b / 235 * 100 for b in full[2:6]]) <= PP.POWER_LIMIT
assert full[2] == full[3] == full[4] == full[5] and full[2] < 235
assert PP.levels([46, 97, 49, 80])[2:6] == bytes(PP.level(x) for x in [46, 97, 49, 80])   # in budget: unchanged
assert PP.levels([50, 50, 50, 50])[2:6] == bytes([118] * 4)
hot = {"mode": "single", "preset": None, "ch": [100, 100, 100, 100],
       "points": [{"t": "10:00", "pct": 100}, {"t": "12:00", "pct": 100}]}
assert PP.power_pct([b / 235 * 100 for b in PP.curve_raw(hot, 660)]) <= PP.POWER_LIMIT
assert PP.power_pct([b / 235 * 100 for b in PP.raw_levels((235, 235, 235, 235))[2:6]]) <= PP.POWER_LIMIT
st = c.get("/api/state").json()
assert st["power_weights"] == [0.39, 0.41, 0.53, 0.11] and st["max_power"] == 100
for name, tpl in st["curve_templates"].items():   # starter templates fit the budget at 100 %
    for pt in tpl["points"]:
        if pt.get("ch"):
            assert PP.power_pct(pt["ch"]) <= 100, (name, pt)
for icon in ("/favicon.png", "/apple-touch-icon.png", "/icon-192.png", "/favicon.ico"):
    r = c.get(icon); assert r.status_code == 200 and r.headers["content-type"] == "image/png", icon
assert c.get("/server.png").status_code == 404 and c.get("/state.png").status_code == 404
print("ALL OK -", len(SENT), "command batches checked")
