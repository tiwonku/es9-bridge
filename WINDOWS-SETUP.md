# Windows setup

If you just want to get going: unzip, double-click **1-setup.bat**, then
**2-check.bat**, then **3-run.bat**. The rest of this document explains what
each step needs and what to do when something goes wrong.

---

## Why your `pip` command failed

TouchDesigner does ship a Python — 3.11, embedded inside the TouchDesigner
install folder. But it is deliberately not on your system PATH, it doesn't
expose `pip` in the normal way, and Derivative's own documentation tells you to
install a *parallel* Python rather than putting packages into TD's copy.

So: yes, you have a Python, but not one your terminal can see or that you should
install into. You need a standalone install.

**Good news:** this bridge runs as its own process and never imports anything
into TouchDesigner. So it does **not** need to match TD's Python version. You
can install whatever version you like and nothing about your TD setup changes.

---

## Prerequisites

### 1. Python 3.11 or newer — required

Get it from **https://www.python.org/downloads/windows/**

**Recommended: Python 3.13.x.** It's mature, every package here has prebuilt
wheels for it, and it has years of support left. Python 3.14 also works.

Anything from 3.11 up is fine — 3.11 is the floor because the bridge reads its
config with `tomllib`, which landed in 3.11. If you happen to pick 3.11 you get
a minor bonus: it matches TouchDesigner's version, so you *could* later point
TD's "Python Module Path" preference at the same site-packages folder. You don't
need that for this project.

**During installation, the one thing that matters:**

> ☑ **Add python.exe to PATH** — tick this on the first installer screen.

It is unticked by default and it is the single most common reason `pip` doesn't
work afterward. If you miss it, re-run the installer, choose *Modify*, and
enable it.

Also leave **pip** and **py launcher** checked (both are on by default).

**After installing, close every open terminal window and open a new one.** PATH
changes don't reach windows that were already open.

### 2. The three Python packages — installed by `1-setup.bat`

| Package | What it does |
|---|---|
| `sounddevice` | PortAudio binding — this is what talks to the ES-9. Bundles PortAudio, so there is nothing else to install. See 3a about ASIO. |
| `numpy` | the audio-rate math in the callback |
| `python-osc` | OSC send and receive |

All three ship prebuilt wheels. **You do not need Visual Studio or C++ build
tools.** If you see an error mentioning "Microsoft Visual C++ 14.0 required",
you're on a Python version that doesn't have wheels yet — drop back to 3.13.

### 3. Expert Sleepers Windows driver — you already have this

The USB Audio 2.0 driver from
**https://www.expert-sleepers.co.uk/downloads.html** (v5.70.0 for Windows
10/11).

### 3a. If the preflight says "ASIO host API not present"

This is **not** a missing driver, and an earlier version of this document got it
wrong. `sounddevice` ships **two** PortAudio DLLs on Windows — one built with
ASIO support, one without — and it loads the **non-ASIO one by default**. ASIO
only appears if the environment variable `SD_ENABLE_ASIO=1` is set *before*
`sounddevice` is imported.

`es9_bridge.py` now sets that automatically at the top of the file, so ASIO
should appear. If it still doesn't, your `sounddevice` build may not bundle the
ASIO DLL at all — which is fine, because **WASAPI exclusive mode is an equally
good path** and the bridge falls back to it automatically.

### 4. A bit-exact host API — the thing that actually matters

The ES-9 shows up under several Windows host APIs, and they are **not
interchangeable for CV**:

| Host API | Bit-exact? | Use for CV? |
|---|---|---|
| ASIO | yes | ✅ best |
| WASAPI **exclusive** | yes | ✅ equally good |
| WDM-KS | yes | ✅ works |
| WASAPI shared | no | ❌ |
| DirectSound | no | ❌ |
| MME | no | ❌ |

The bottom three route through the **Windows audio engine**, which resamples and
mixes. That's harmless for music and destructive for DC-coupled control voltage
— your CV would be altered before you ever saw it, and the reported sample rate
is Windows' "Default Format" setting rather than the hardware's.

`hostapi = "auto"` in `es9.toml` tries ASIO, then WASAPI exclusive, then WDM-KS,
and **refuses to start** rather than silently falling back to a shared-mode API.
`2-check.bat` probes every one of them and shows you which open, at what rate,
and which are safe.

### 5. TouchDesigner must release the ES-9 — important

**ASIO gives one application exclusive access to a device.** While
TouchDesigner has the ES-9 open through an Audio Device In CHOP, the bridge
cannot open it, and vice versa.

Before running the bridge for the first time, do one of:

- delete or bypass your Audio Device In CHOP, or
- point its Device parameter at something other than the ES-9, or
- just close TouchDesigner entirely for the first test

`2-check.bat` actually tries to open the device, so it will tell you plainly if
something else is holding it. This is the most likely thing to trip you up, and
it is not a bug in either program.

### 6. Ports 7000, 7001 and 8080 — free

- **7000/UDP** — OSC out to TouchDesigner
- **7001/UDP** — OSC control in (calibration, manual reset)
- **8080/TCP** — the status page

8080 in particular is popular with other software. The preflight check tests all
three; change them in `es9.toml` if there's a clash.

### 7. Firewall — nothing to do

By default the bridge binds to `127.0.0.1`, so everything stays on your machine
and **Windows Firewall will not prompt you.** You only need to think about this
if you set `bind = "0.0.0.0"` in `es9.toml` to reach the status page from
another machine — then Windows prompts once and you should tick *Private
networks* only.

Sending OSC *out* to another machine's IP works either way; the bind setting
only controls what can reach in.

---

## Step by step

**1. Unzip somewhere simple** — `C:\es9bridge` is ideal.

Avoid putting it inside OneDrive-synced folders (Documents, Desktop on many
setups). OneDrive can lock files mid-write and produces confusing errors.

**2. Install Python** — see above. Tick *Add python.exe to PATH*.

**3. Open a fresh terminal** and confirm it worked:

```
py --version
```

You should see `Python 3.13.x`. If you see anything else, jump to
Troubleshooting.

**4. Double-click `1-setup.bat`** — installs the three packages.

**5. Double-click `2-check.bat`** — the preflight. It verifies your Python
version and packages, then **probes every host API in turn** — opening the ES-9
through each one and reporting the rate, latency and whether that path is
bit-exact. It also checks the three ports and flags any sample-rate disagreement
between the hardware and Windows' shared-mode default. Fix anything marked FAIL
and run it again.

**6. Double-click `3-run.bat`** — the bridge starts. Open
**http://localhost:8080**.

**7. In TouchDesigner:** add an **OSC In CHOP**, Network Port `7000`, Protocol
`Messages`. Eight channels appear. Delete your Audio Device In CHOP.

---

## Use `py`, not `python`

Windows installs a launcher called `py` that lives in a fixed location and
always works. `python` depends on PATH and can be hijacked by Windows' Microsoft
Store aliases. Every batch file here uses `py`.

If you're typing commands yourself, prefer:

```
py -m pip install sounddevice python-osc numpy
py es9_bridge.py --config es9.toml --check
```

over `pip install ...` and `python es9_bridge.py ...`.

---

## Troubleshooting

**Typing `python` opens the Microsoft Store.**
Windows ships placeholder aliases that hijack the command. Turn them off:
*Settings → Apps → Advanced app settings → App execution aliases* → toggle OFF
both **python.exe** and **python3.exe**. Or just use `py` instead.

**`'py' is not recognized` / `'pip' is not recognized`.**
Python isn't installed, or *Add python.exe to PATH* wasn't ticked. Re-run the
python.org installer, choose *Modify*, enable it. Then open a **new** terminal —
existing windows keep the old PATH.

**`pip` runs but installs into the wrong Python.**
If you have several Pythons, pin the one you want:
`py -3.13 -m pip install sounddevice python-osc numpy`.
`py --list` shows everything installed.

**"Microsoft Visual C++ 14.0 or greater is required".**
You're on a Python version too new for prebuilt wheels. Install 3.13 and use
`py -3.13 -m pip install ...`.

**Preflight says the device won't open, with an "unanticipated host error" or
"device unavailable".**
Almost always ASIO exclusivity — TouchDesigner (or a DAW, or VCV Rack) still has
the ES-9. Close it and re-check.

**The ES-9 shows up more than once in `--list-devices`.**
It appears separately under each host API — MME, WASAPI, WDM-KS, ASIO. Prefer
the ASIO entry. Set `device.name` to the exact name, or use the numeric index
from the listing.

**Preflight says no ASIO host API is present.**
Not a driver problem — see section 3a. `sounddevice` loads a non-ASIO PortAudio
build unless `SD_ENABLE_ASIO=1` is set before import. The bridge sets it for you.
If ASIO still doesn't appear, WASAPI exclusive is an equally good path and the
bridge uses it automatically.

**Preflight says ASIO "host API present, but no input device enumerated under it".**
This is the big one. **An ASIO driver cannot be enumerated while another
application holds it.** If TouchDesigner is open with the ES-9 on an Audio Device
In CHOP, ASIO will list nothing at all — the host API loads, but the driver is
locked. Close TouchDesigner completely (not just the project — the whole app) and
run `2-check.bat` again.

If ASIO still lists nothing with everything closed, run `2b-list-devices.bat`.
Under ASIO, PortAudio names devices after the **driver**, which may not contain
"ES-9" at all — it could be "Expert Sleepers USB Audio" or similar. Set
`device.name` in `es9.toml` to whatever you see. (The bridge already falls back
to any input device under ASIO with enough channels, so this is rarely needed.)

**Preflight says "Invalid sample rate" on wasapi-exclusive or wdm-ks.**
Fixed as of this version. Exclusive-mode APIs accept *only* the hardware's actual
rate and reject Windows' shared-mode default, so a single attempt at the device
default fails misleadingly. The bridge now probes 48k, 44.1k, 96k, 88.2k and
192k in turn and reports which was accepted.

**Preflight says "Invalid number of channels" on shared wasapi.**
Expected and harmless. Shared-mode WASAPI only offers the device's shared channel
config, usually stereo. You don't want shared mode anyway.

**Preflight says "bit-exact path: none available".**
The ES-9 is visible but only through shared-mode APIs. Either something holds the
exclusive paths (close TouchDesigner) or the driver didn't register. Never run a
set on a shared-mode API — the Windows mixer will alter your CV.

**Preflight warns about a rate disagreement.**
Your hardware runs at one rate and Windows' shared-mode "Default Format" for the
device says another. The bridge uses the hardware rate so it's unaffected — but
check TouchDesigner's Audio Device In CHOP Rate parameter matches the hardware
rate. A mismatch there is a prime suspect for the original drift problem.

**Nothing arrives in TouchDesigner.**
Check the status page first. If the meters are moving there, the bridge is fine
and the problem is on the TD side: confirm the OSC In CHOP port is `7000` and
that TD isn't also trying to hold the audio device. If the meters are *not*
moving, check the channel `in_range` values in `es9.toml` — a signal outside the
configured range clamps to the ends.

**Values look wrong / stuck at 0 or 1.**
Range mismatch, not a bug. The Volts column on the status page shows the real
input; set `in_range` to bracket it. Or send `/bridge/calibrate/N` to port 7001,
sweep the CV, then `/bridge/calibrate/N/stop`.

**Everything works but you want to be sure the watchdog does too.**
Run `run-simulate.bat`. It generates synthetic signals with no hardware and
injects a fake wraparound stall after 30 seconds so you can watch the recovery
log fill in.

---

## Quick reference

| Command | What it does |
|---|---|
| `py es9_bridge.py --check -c es9.toml` | preflight — run this whenever something's off |
| `py es9_bridge.py --list-devices` | list every input device and host API |
| `py es9_bridge.py -c es9.toml` | run it |
| `py es9_bridge.py -c es9.toml --simulate` | run on synthetic signals |
| `py es9_bridge.py -c es9.toml --api wasapi-exclusive` | force a host API — takes the ASIO driver out of the picture |
| `py es9_bridge.py -c es9.toml --no-watchdog` | disable auto-recovery, for A/B testing |
| `py test_watchdog.py` | run the detector test suite |
| `py test_hostapi.py` | run the host API selection test suite |
