"""Week Aqua BLE light control - protocol decoded from the official Android app (v3.0.28).

Covers the "WEK" family (advert manufacturer id bytes 57 46), e.g. B3.0-L900pro.
Every command is 8 bytes written (no response) to characteristic 0000fff2; unused bytes are 0x55.

Usage:
  python extras/weekaqua-cli.py scan
  python extras/weekaqua-cli.py sync   <mac>                 # set the light's clock to this PC's time
  python extras/weekaqua-cli.py manual <mac> R G B UV        # live levels in percent (0-100)
  python extras/weekaqua-cli.py off    <mac>
  python extras/weekaqua-cli.py auto   <mac>                 # back to the light's stored schedule
  python extras/weekaqua-cli.py fan    <mac> PCT
  python extras/weekaqua-cli.py slot   <mac> N HH:MM HH:MM R G B UV   # schedule slot N (1-8)
  python extras/weekaqua-cli.py clear  <mac> N
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys

from bleak import BleakClient, BleakScanner

WRITE_CHAR = "0000fff2-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "0000fff1-0000-1000-8000-00805f9b34fb"
FAMILIES = {  # manufacturer-data company id (as the app reads it) -> app's name
    0x4557: "HJ (5745)", 0x4657: "WEK (5746)", 0x4757: "FOUR (5747)", 0x4857: "FIVE (5748)",
    0x4957: "SIX (5749)", 0x5057: "SEVEN (5750)", 0x5157: "EIGHT (5751)", 0x5257: "NINE (5752)",
}
PAD = 0x55
MAX_LEVEL = 235  # 0xEB = 100 %


def _bcd(n: int) -> int:
    """The app writes decimal digits straight into hex, e.g. 23 -> 0x23."""
    return int(f"{n:02d}", 16)


def _level(pct: float) -> int:
    pct = max(0.0, min(100.0, pct))
    return round(pct / 100 * MAX_LEVEL)


def _cmd(*b: int) -> bytes:
    return bytes(b) + bytes([PAD] * (8 - len(b)))


# ---- command builders -------------------------------------------------------
def cmd_time_sync(now: dt.datetime | None = None) -> list[bytes]:
    now = now or dt.datetime.now()
    return [_cmd(0xFF, _bcd(now.hour), _bcd(now.minute), _bcd(now.second)), _cmd(0xF9)]


def cmd_mode_manual() -> bytes:
    return _cmd(0xFD, 0xF1)


def cmd_mode_auto() -> bytes:
    return _cmd(0xFD, 0xF2)


def cmd_levels(r: float, g: float, b: float, uv: float, slot: int = 9) -> bytes:
    """slot 9 = live manual levels; 1-8 = schedule slot levels."""
    return _cmd(0xFB, 0xF0 | slot, _level(r), _level(g), _level(b), _level(uv))


def cmd_manual_window(start: str = "00:00", end: str = "24:00", ramp: int = 0, kind: int = 0) -> bytes:
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    return bytes([0xFE, 0xF9, _bcd(sh), _bcd(sm), _bcd(eh), _bcd(em), kind, ramp])


def cmd_slot_time(slot: int, start: str, end: str) -> bytes:
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    return _cmd(0xFE, 0xF0 | slot, _bcd(sh), _bcd(sm), _bcd(eh), _bcd(em))


def cmd_slot_clear(slot: int) -> list[bytes]:
    return [_cmd(0xFE, 0xF0 | slot, 0, 0, 0, 0), _cmd(0xFB, 0xF0 | slot, 0, 0, 0, 0)]


def cmd_fan(pct: float) -> bytes:
    return _cmd(0xFC, _level(pct))


def cmd_on_full() -> list[bytes]:
    """Exactly what the official app's power button sends for 'on'."""
    return [cmd_mode_manual(), bytes.fromhex("FBF979DF74EB5555"), bytes.fromhex("FEF9000024000000")]


def cmd_off() -> bytes:
    return _cmd(0xFB, 0xF9, 0, 0, 0, 0)


# ---- transport --------------------------------------------------------------
class Light:
    def __init__(self, mac: str, delay: float = 0.5):  # app spaces writes 500 ms apart
        self.mac = mac.upper()
        self.delay = delay
        self.client: BleakClient | None = None

    async def __aenter__(self):
        dev = await BleakScanner.find_device_by_address(self.mac, timeout=15)
        if dev is None:
            raise RuntimeError(f"{self.mac} not found - is the phone app holding it?")
        self.client = BleakClient(dev, timeout=20)
        await self.client.connect()
        try:
            await self.client.start_notify(NOTIFY_CHAR, lambda _, d: print(f"  <- {d.hex(' ')}"))
        except Exception:  # noqa: BLE001
            pass
        return self

    async def __aexit__(self, *exc):
        if self.client:
            await self.client.disconnect()

    async def send(self, *cmds: bytes | list[bytes]):
        flat: list[bytes] = []
        for c in cmds:
            flat.extend(c if isinstance(c, list) else [c])
        for c in flat:
            print(f"  -> {c.hex(' ')}")
            await self.client.write_gatt_char(WRITE_CHAR, c, response=False)
            await asyncio.sleep(self.delay)


async def scan():
    found = await BleakScanner.discover(timeout=10, return_adv=True)
    for addr, (dev, adv) in found.items():
        if "0000fff0-0000-1000-8000-00805f9b34fb" not in adv.service_uuids:
            continue
        fam = ", ".join(FAMILIES.get(cid, f"unknown 0x{cid:04x}") for cid in adv.manufacturer_data) or "?"
        print(f"{addr}  {adv.local_name!r:24} rssi={adv.rssi:4}  family={fam}")


async def main(argv: list[str]):
    if not argv or argv[0] == "scan":
        return await scan()
    op, mac, *rest = argv
    async with Light(mac) as L:
        if op == "sync":
            await L.send(cmd_time_sync())
        elif op == "manual":
            r, g, b, uv = map(float, rest)
            await L.send(cmd_mode_manual(), cmd_levels(r, g, b, uv), cmd_manual_window())
        elif op == "on":
            await L.send(cmd_on_full())
        elif op == "off":
            await L.send(cmd_off())
        elif op == "auto":
            await L.send(cmd_time_sync(), cmd_mode_auto())
        elif op == "fan":
            await L.send(cmd_fan(float(rest[0])))
        elif op == "slot":
            n, start, end, *lv = rest
            await L.send(cmd_slot_time(int(n), start, end), cmd_levels(*map(float, lv), slot=int(n)))
        elif op == "clear":
            await L.send(cmd_slot_clear(int(rest[0])))
        elif op == "demo":
            await L.send(cmd_time_sync(), cmd_on_full())
            await asyncio.sleep(4)
            await L.send(cmd_levels(50, 50, 50, 50))
            await asyncio.sleep(4)
            await L.send(cmd_levels(0, 0, 100, 0))
            await asyncio.sleep(3)
            await L.send(cmd_mode_auto())
        else:
            print(__doc__)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
