"""Week Aqua "WEK" family BLE protocol (see PROTOCOL.md).

Every command is 8 bytes written without response to FFF2; unused bytes are 0x55.
"""
from __future__ import annotations

import datetime as dt

SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb"
WRITE_CHAR = "0000fff2-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "0000fff1-0000-1000-8000-00805f9b34fb"
WEK_COMPANY_ID = 0x4657
PAD = 0x55
MAX_LEVEL = 235  # 0xEB = 100 %
SLOTS = 8


def _bcd(n: int) -> int:
    return int(f"{n:02d}", 16)


def _hm(t: str) -> tuple[int, int]:
    h, m = (int(x) for x in t.split(":"))
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m):
        raise ValueError(f"bad time {t!r}")
    return _bcd(h), _bcd(m)


# Power budget. The official app weights each channel by how much power its LEDs draw and won't let
# a custom colour's weighted total go over 100 %. LuminAqua lets you set any mix and, when it's over,
# dims all channels evenly so what's sent fits. Factory presets (77-102 %) are sent byte-exact.
POWER_WEIGHTS = (0.39, 0.41, 0.53, 0.11)   # R, G, B, UV
MAX_POWER = 100.0
POWER_LIMIT = MAX_POWER


def power_pct(ch) -> float:
    """Weighted power of channel percentages (0-100 each), as % of the light's budget."""
    return sum(w * float(c) for w, c in zip(POWER_WEIGHTS, ch))


def cap_pct(ch, limit: float = POWER_LIMIT) -> list[float]:
    """Scale all channels down by the same factor so the mix fits the power budget (colour kept)."""
    ch = [max(0.0, min(100.0, float(c))) for c in ch]
    p = power_pct(ch)
    if p <= limit:
        return ch
    f = limit / p * 0.999
    return [c * f for c in ch]


def cap_raw(raw, limit: float = POWER_LIMIT) -> tuple[int, ...]:
    pct = [r / MAX_LEVEL * 100 for r in raw]
    if power_pct(pct) <= limit:
        return tuple(int(r) for r in raw)
    return tuple(int(c / 100 * MAX_LEVEL) for c in cap_pct(pct, limit))   # round down: stay under


def level(pct: float) -> int:
    return round(max(0.0, min(100.0, float(pct))) / 100 * MAX_LEVEL)


def _cmd(*b: int) -> bytes:
    return bytes(b) + bytes([PAD] * (8 - len(b)))


def time_sync(now: dt.datetime | None = None) -> list[bytes]:
    now = now or dt.datetime.now()
    return [_cmd(0xFF, _bcd(now.hour), _bcd(now.minute), _bcd(now.second)), _cmd(0xF9)]


def mode_manual() -> bytes:
    return _cmd(0xFD, 0xF1)


def mode_auto() -> bytes:
    return _cmd(0xFD, 0xF2)


def levels(ch: list[float], slot: int = 9) -> bytes:
    """slot 9 = live manual levels; 1-8 = schedule slot."""
    r, g, b, uv = cap_raw([level(x) for x in ch])
    return _cmd(0xFB, 0xF0 | slot, r, g, b, uv)


RAMP_HOURS = [0, 0.5, 1, 1.5, 2, 2.5]  # "Sunrise Sunset Mode" options, sent as their index


def manual_window(start: str = "00:00", end: str = "24:00", ramp: int = 0) -> bytes:
    """Daily on-window for manual ("Mode setting") mode, with sunrise/sunset ramp index 0-5."""
    if not 0 <= ramp < len(RAMP_HOURS):
        raise ValueError("ramp index must be 0-5")
    return bytes([0xFE, 0xF9, *_hm(start), *_hm(end), 0x00, ramp])


def slot_time(slot: int, start: str, end: str) -> bytes:
    return _cmd(0xFE, 0xF0 | slot, *_hm(start), *_hm(end))


def slot_clear(slot: int) -> list[bytes]:
    return [_cmd(0xFE, 0xF0 | slot, 0, 0, 0, 0), _cmd(0xFB, 0xF0 | slot, 0, 0, 0, 0)]


def fan(pct: float) -> bytes:
    return _cmd(0xFC, level(pct))


# ---- high-level sequences ---------------------------------------------------
def color_cmd(ch: list[float], preset: str | None = None) -> bytes:
    """Factory presets are sent byte-exact; anything else from percentages."""
    return preset_raw(preset) if preset in APP_PRESETS else levels(ch)


def daily_timer(timer: dict) -> list[bytes]:
    """The official app's 'Mode setting': manual mode + colour + daily on-window."""
    return [*time_sync(), mode_manual(), color_cmd(timer["ch"], timer.get("preset")),
            manual_window(timer["start"], timer["end"], timer.get("ramp", 0))]


def force(ch: list[float], preset: str | None = None) -> list[bytes]:
    """On (or off, with zeros) right now regardless of the daily window."""
    return [mode_manual(), color_cmd(ch, preset), manual_window("00:00", "24:00", 0)]


def advanced_mode() -> list[bytes]:
    return [*time_sync(), mode_auto()]


def write_schedule(slots: list[dict]) -> list[bytes]:
    """slots: [{start:'HH:MM', end:'HH:MM', ch:[r,g,b,uv]}], up to 8. Unused slots are cleared."""
    out: list[bytes] = []
    for i in range(1, SLOTS + 1):
        s = slots[i - 1] if i <= len(slots) else None
        if s:
            out += [slot_time(i, s["start"], s["end"]), levels(s["ch"], slot=i)]
        else:
            out += slot_clear(i)
    return out


# The official app's 12 "Preset Modes" for WEK lights (R G B UV, raw 0-235), in app order.
# Names from the app's English strings; values from ModeSettingFragment / StringTools.getPercent.
APP_PRESETS = {
    "Green": (0x6C, 0xE3, 0x73, 0xEB),
    "Red": (0xA7, 0xB9, 0x74, 0xEB),
    "Mixed": (0x79, 0xDF, 0x74, 0xEB),  # also what the app's power button sends
    "Gorgeous": (0xC1, 0x5E, 0xBC, 0xEB),
    "Colorful": (0xEB, 0x5E, 0x94, 0xEB),
    "Algae": (0xA3, 0xEB, 0x70, 0x00),
    "Nature": (0x77, 0xEB, 0x77, 0x00),
    "Elegant": (0x83, 0xEB, 0x85, 0x00),
    "Brisk": (0x5E, 0xEB, 0x5E, 0x00),
    "Fresh": (0x59, 0xEB, 0x60, 0x00),
    "Enjoy": (0x6E, 0xEB, 0x65, 0x00),
    "White": (0x7E, 0xEB, 0x69, 0x00),
}


def preset_raw(name: str) -> bytes:
    """Send a factory preset byte-exact (no % rounding)."""
    r, g, b, uv = APP_PRESETS[name]
    return _cmd(0xFB, 0xF9, r, g, b, uv)


def raw_to_pct(raw: tuple[int, ...]) -> list[float]:
    return [round(v / MAX_LEVEL * 100) for v in raw]


# ---- smooth curve (LuminAqua drives it; the light only holds the current level) --------
def _mins(t: str) -> int:
    h, m = (int(x) for x in t.split(":"))
    return h * 60 + m


def _mins_to_t(n: int) -> str:
    return f"{n // 60:02d}:{n % 60:02d}"


def curve_pct(points: list[dict], minute: float) -> float:
    """Brightness % at a minute of the day: straight lines between anchor points, 0 outside them."""
    pts = [(_mins(p["t"]), float(p["pct"])) for p in points]
    if not pts or minute < pts[0][0] or minute > pts[-1][0]:
        return 0.0
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t0 <= minute <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (minute - t0) / (t1 - t0)
    return pts[-1][1]


def color_raw(preset: str | None, ch: list[float]) -> tuple[int, int, int, int]:
    """A colour as raw levels: factory presets byte-exact, anything else (custom/saved) from %."""
    return APP_PRESETS[preset] if preset in APP_PRESETS else tuple(level(x) for x in ch)


def point_color(curve: dict, p: dict) -> tuple[int, int, int, int]:
    """Per-point colour mode uses the point's own colour (if it has one), otherwise the curve's."""
    if curve.get("mode") == "per_point" and p.get("ch") is not None:
        return color_raw(p.get("preset"), p["ch"])
    return color_raw(curve.get("preset"), curve["ch"])


def anchor_raw(curve: dict, p: dict) -> list[float]:
    """Effective output at an anchor point: colour channel x point brightness / 100 (unrounded)."""
    k = float(p["pct"]) / 100
    return [v * k for v in point_color(curve, p)]


def curve_raw(curve: dict, minute: float) -> tuple[int, int, int, int]:
    """Output at this minute: R, G, B and UV each interpolated in a straight line between the
    effective values of the anchor points either side; 0 outside the curve."""
    pts = curve["points"]
    ms = [_mins(p["t"]) for p in pts]
    if not pts or minute < ms[0] or minute > ms[-1]:
        return (0, 0, 0, 0)
    for i in range(len(pts) - 1):
        if ms[i] <= minute <= ms[i + 1]:
            a, b = anchor_raw(curve, pts[i]), anchor_raw(curve, pts[i + 1])
            f = 0.0 if ms[i + 1] == ms[i] else (minute - ms[i]) / (ms[i + 1] - ms[i])
            return cap_raw([round(x + (y - x) * f) for x, y in zip(a, b)])
    return cap_raw([round(x) for x in anchor_raw(curve, pts[-1])])


def raw_pct(raw) -> list[float]:
    """Raw levels as % (one decimal), for display."""
    return [round(v / MAX_LEVEL * 100, 1) for v in raw]


def raw_levels(raw) -> bytes:
    return _cmd(0xFB, 0xF9, *cap_raw(raw))


def curve_window(curve: dict) -> tuple[str, str]:
    """The light's own on-window = the curve's span. If LuminAqua stops, the light keeps its
    last level and still switches itself off at the end of this window."""
    pts = curve["points"]
    return pts[0]["t"], pts[-1]["t"]


def minute_of_day(now: dt.datetime | None = None) -> float:
    now = now or dt.datetime.now()
    return now.hour * 60 + now.minute + now.second / 60


def curve_start(curve: dict, now: dt.datetime | None = None) -> list[bytes]:
    """Switch a light to curve mode: clock, manual mode, today's level now, safety window."""
    now = now or dt.datetime.now()
    start, end = curve_window(curve)
    return [*time_sync(now), mode_manual(), raw_levels(curve_raw(curve, minute_of_day(now))),
            manual_window(start, end, 0)]
