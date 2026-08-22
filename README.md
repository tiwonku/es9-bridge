# ES-9 → OSC bridge

A small standalone process that owns the ES-9 audio stream, does audio-rate
analysis, and rebroadcasts to OSC. TouchDesigner never touches the audio device
again — it just receives OSC.

It exists to solve two problems at once:

1. **Stability.** A watchdog detects both a dead stream *and* the "still moving
   but not responding" ring-buffer wraparound case, and reopens the device in
   ~150 ms without you touching anything. Every recovery is timestamped so you
   can characterise the underlying fault instead of guessing at it.
2. **Trigger fidelity.** Gates and triggers are detected at audio rate inside
   the callback, on the exact sample where the edge occurs, and sent
   immediately rather than on the 60 Hz grid. A 2 ms trigger cannot be lost or
   smeared. You cannot get this from a 60 fps Audio Device In CHOP at all.

## Install

**On Windows, read `WINDOWS-SETUP.md`** — it covers the full prerequisite list,
why TouchDesigner's embedded Python doesn't count, and the batch files. Short
version: double-click `1-setup.bat`, `2-check.bat`, `3-run.bat`.

Otherwise:

```
pip install sounddevice python-osc numpy
```

Python 3.11 or newer (uses `tomllib` from the standard library).

**Two Windows notes that matter:**

- `sounddevice` bundles two PortAudio DLLs and loads the **non-ASIO one** unless
  `SD_ENABLE_ASIO=1` is set before import. The bridge sets it automatically.
- Only **ASIO, WASAPI-exclusive and WDM-KS** are bit-exact. MME, DirectSound and
  shared WASAPI route through the Windows audio engine, which resamples and
  mixes — fine for music, destructive for DC-coupled CV. `hostapi = "auto"`
  picks a bit-exact path and refuses to start rather than falling back to a
  shared one.

ASIO and WASAPI-exclusive both give one application exclusive access, so
TouchDesigner must not have the ES-9 open when the bridge starts.

## Run

```
python es9_bridge.py --check -c es9.toml     # preflight: checks everything
python es9_bridge.py --list-devices          # find your ES-9
python es9_bridge.py --config es9.toml       # go
```

`--check` verifies your Python version and packages, then probes **every host
API** — opening the ES-9 through each and reporting rate, latency and whether
that path is bit-exact. It catches exclusivity conflicts, finds a usable path,
and flags any disagreement between the hardware rate and Windows' shared-mode
default. Run it first, and again any time something is off.

Then open **http://localhost:8080** for the status page.

You do **not** need to know your ES-9's sample rate. The bridge reads it back
from the driver at open time and shows it on the status page — which also means
it can never be wrong the way a hardcoded value in TD can.

### Useful flags

| Flag | What it does |
|---|---|
| `--list-devices` | lists input devices; marks anything that looks like an ES-9 |
| `--simulate` | synthetic 8-channel signals, no hardware needed |
| `--simulate-fault 20` | in simulate mode, inject a wraparound stall after 20 s so you can watch the watchdog catch it |
| `--device "ES-9"` | override the config's device |
| `--api wasapi-exclusive` | force a host API — the fastest way to take the ASIO driver out of the picture |
| `--no-watchdog` | disable auto-recovery (for A/B testing) |
| `--status-port 8090` | move the status page |

Build your TouchDesigner patch against `--simulate` with the rack powered down;
the OSC surface is identical.

## The TouchDesigner side

One operator. Add an **OSC In CHOP**:

- **Network Port**: `7000`
- **Protocol**: `Messages`

Eight channels appear as `es9/1` … `es9/8`, already mapped to 0–1 and smoothed.
Rename them with a Rename CHOP if you want the friendly names from the config.
Set each jack's role (LFO, clock, envelope, audio RMS) on the status page or in
`es9.toml` — OSC addresses stay numeric so the TD patch does not move.

For triggers, add a second **OSC In CHOP** (or use the same one) and look at the
`es9/N/trig` channels — each message is a discrete event, so pair it with a
Trigger CHOP or a Count CHOP rather than sampling the value.

That's the whole integration. Delete your Audio Device In CHOP.

## Channel modes

Each input is one pipeline with a **mode** that fills sensible defaults. Change
it on the status page (edits save back to `es9.toml`) or edit the file directly.

| Mode | Value sent at 60 Hz | Gates / trigs | Typical `in_range` |
|---|---|---|---|
| `lfo` | block mean, bipolar CV | on | −5 … 5 V |
| `clock` | block mean, unsmoothed | on, stricter threshold | 0 … 5 V |
| `env` | block mean, light smooth, minmax | on | 0 … 8 V |
| `audio` | **RMS amplitude** | off (so it doesn't chatter) | 0 … 2 V RMS |

OSC addresses stay `/es9/N`. Names are labels only.

The knobs under each row (ranges, smooth, threshold, lockout) still apply after
you pick a mode — mode is a preset, not a lock.

## OSC surface

| Address | Payload | Rate |
|---|---|---|
| `/es9/1` … `/es9/8` | float — mapped, smoothed | 60 Hz |
| `/es9/N/minmax` | `[min_v, max_v]` for the frame | 60 Hz, opt-in per channel |
| `/es9/N/gate` | `0` / `1` | **on change only** |
| `/es9/N/trig` | `1` | event, sub-frame timing |

Gates are sent on change rather than every frame, so they are cheap and trivial
to act on in TD.

### Control input (port 7001)

| Address | Effect |
|---|---|
| `/bridge/calibrate/N` | start auto-ranging channel N — sweep the CV by hand |
| `/bridge/calibrate/N/stop` | finish; the observed min/max is logged for you to paste into the config |
| `/bridge/reset` | force a stream reopen |

## Multiple destinations

Add another `[[destination]]` block to `es9.toml`. The sender duplicates every
message to every destination; no code change, and it hot-reloads, so you can add
a machine while the bridge is running.

```toml
[[destination]]
host = "127.0.0.1"
port = 7000

[[destination]]
host = "192.168.1.42"
port = 7000
```

## How the watchdog decides something is wrong

Two independent detectors, because the failure you're chasing never raises an
error:

- **Liveness** — the callback stamps a timestamp on every invocation. No new
  stamp for 250 ms means the stream is dead.
- **Staleness** — the callback keeps a cheap signature of each raw block.
  A stall trips if either *(a)* 40 blocks in a row are bit-identical (frozen
  buffer), or *(b)* the last 30 blocks exactly repeat an earlier 30-block window
  (looping buffer — your symptom).

Check (b) is safe on real analog inputs: there is always LSB dither on an ADC,
so bit-identical blocks across all eight channels simultaneously essentially
cannot happen naturally. `test_watchdog.py` includes a false-positive case with
a perfectly periodic LFO to prove it. If you ever run with nothing patched,
set `staleness_enabled = false`.

## Calibrating volts

`full_scale_volts = 10.0` is a starting assumption for the ES-9's DC-coupled
inputs, not a measured fact. To pin it down: patch a known voltage — a quantizer
set to a known note, or a VCO's tuned CV — and adjust `full_scale_volts` until
the status page's Volts column agrees. Everything downstream (ranges,
thresholds) is expressed in volts, so this is worth five minutes.

If you only care about normalized 0–1 values, skip it and use
`/bridge/calibrate/N` to auto-range each channel instead.

## Tests

```
python test_watchdog.py      # detector logic, trigger timestamps, lockout behaviour
python test_channels.py      # modes, RMS amplitude, toml persist
python test_hostapi.py       # host API selection against a simulated Windows layout
python verify.py             # end-to-end: receives the OSC, checks rates and jitter
```

`verify.py` measured 60.0 Hz exactly on all eight value channels with 0.5–0.7 ms
gap jitter, trigger interval jitter of 2.45 ms (well under one 60 Hz frame), and
recovery from an injected wraparound stall in 151 ms.

## Known limitations

- When it runs on ASIO, PortAudio drives the same Expert Sleepers driver
  TouchDesigner does, so if the driver itself is at fault the bridge inherits it
  — it just recovers automatically instead of stranding you. To take the ASIO
  layer out of the picture entirely as a diagnostic, run with
  `--api wasapi-exclusive`. That is also a perfectly good way to run it
  permanently: exclusive mode is bit-exact and comparably low latency.
- One extra frame of latency versus reading the device directly in TD.
- Python's GIL is a fair concern for real-time audio. At 48 kHz with 256-sample
  blocks that's ~188 callbacks/sec doing vectorized numpy on an 8×256 array —
  comfortable. If it proves marginal on your machine, the architecture ports to
  a small Rust binary without changing the OSC surface or your TD patch.
- Device settings (name, sample rate, block size, channel list) need a restart.
  Everything else hot-reloads on save.
