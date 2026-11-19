# Week Aqua BLE protocol — "WEK" family

Worked out for interoperability, from how the official WEEK AQUA app (v3.0.28) talks to the lights,
and verified on real hardware. Independent work; not affiliated with or endorsed by WEEK AQUA.

Tested on: **B3.0-L900pro** and **P6000pro** (both "WEK" family). Other WEK-family lights are expected
to work; other families (see below) use different commands and aren't supported.

## Transport
- Advertises service `FFF0`; manufacturer data company id `0x4657` (raw bytes `57 46`), which the app
  reads as family code `5746` = WEK.
- Other family codes, which the official app drives with different screens and commands: `5745` older HJ
  series (also the app's fallback for unknown codes), `5747`, `5748`, `5749`, `5751`, `5752` (newer light
  and device series), `5750` smart plug. LuminAqua lists these as *Not supported*.
- Write commands (write-without-response) to `0000fff2-…`. `0000fff1` is notify (silent so far).
- Every command is **8 bytes**; unused bytes are `0x55`.
- Do **not** touch service `F000FFC0` — that is the firmware-update (OAD) channel.

**Two things matter** (without either, nothing happens):
send the manual window (`FE F9 00 00 24 00 00 00`) with manual levels, and space writes ~500 ms apart.

## The two modes (important)
The light runs one of two modes, chosen by the last `FD F1` / `FD F2` it received:
- **Manual / "Mode setting"** (`FD F1`) — the app's main screen. One colour (`FB F9 …`) shown only
  inside a daily on-window (`FE F9 on-hh on-mm off-hh off-mm 00 ramp`), with a sunrise/sunset fade.
  Ramp index 0–5 = 0, 0.5, 1, 1.5, 2, 2.5 h.
- **Advanced setting** (`FD F2`) — up to 8 periods (`FE Fn` / `FB Fn`).
To force a light on/off right now in manual mode, widen the window to 00:00–24:00 and restore it after.
The light cannot report its settings back, so a controller must remember them itself.

## Encoding
- **Times** are written as decimal digits in hex (BCD-like): 19:44 → `19 44`. End of day = `24 00`.
- **Levels** are 0–235 (`0x00`–`0xEB`) = 0–100 %. `level = round(pct/100 * 235)`.
- Channel order is R, G, B, UV (as the app labels them).

## Commands
| Bytes | Meaning |
|---|---|
| `FF hh mm ss 55 55 55 55` then `F9 55 55 55 55 55 55 55` | Set clock (app sends both on every connect) |
| `FD F1 55…` | Switch to **manual** mode |
| `FD F2 55…` | Switch to **schedule (auto)** mode |
| `FB F9 R G B UV 55 55` | Live manual levels |
| `FE F9 sh sm eh em kk rr` | Manual on-window start/end; `kk` type 00/01, `rr` ramp index |
| `FC LL 55…` | Fan level 0–235 |
| `FE Fn sh sm eh em 55 55` | Schedule slot *n* (1–8) time window |
| `FB Fn R G B UV 55 55` | Schedule slot *n* levels |
| `FE Fn 00 00 00 00 55 55` + `FB Fn 00 00 00 00 55 55` | Clear slot *n* |

### What the app's power button sends
- **On (full):** `FDF1…`, `FBF9 79 DF 74 EB 5555`, `FEF9 0000 2400 0000`
- **Off:** `FBF9 00 00 00 00 5555`
- **Auto:** `FDF2…` (or re-sends the manual window if the manual tab was last used)

### Built-in presets (manual levels R G B UV)
`6C E3 73 EB`, `A7 B9 74 EB`, `79 DF 74 EB`, `C1 5E BC EB`, `EB 5E 94 EB`, `A3 EB 70 00`,
`77 EB 77 00`, `83 EB 85 00`, `5E EB 5E 00`, `59 EB 60 00`, `6E EB 65 00`, `7E EB 69 00`.

### Power budget
The app weights each channel by the power its LEDs draw and refuses any custom slider move that would
take the total over **100 %** (it shows a "maximum" message instead):

`power % = 0.39·R% + 0.41·G% + 0.53·B% + 0.11·UV%`  (all four at 100 % would be 144 %)

The built-in presets sit at 77–102 % on this scale. LuminAqua applies the same limit to custom colours
(sliders stop at 100 %) and, as a backstop, scales anything it sends above 102 % down proportionally.

## Smooth curves (LuminAqua)
The lights can't fade between arbitrary levels on their own. LuminAqua stays in manual mode, sets the
on-window to the curve's first/last point (so the light still switches itself off if the controller
dies), and writes `FB F9 R G B UV` once a minute as the curve changes. Live level writes change the
output smoothly with no blink.

## Thanks
[ad960009/WeekAquaProject](https://github.com/ad960009/WeekAquaProject), an independent Windows and
Home Assistant controller for WEEK AQUA lights, pointed us to the app's power limit and family codes;
the values above were then checked against the official app itself.
