#!/usr/bin/env python3
"""
es9_bridge.py -- Expert Sleepers ES-9 -> OSC bridge with stall watchdog.

Owns the ES-9 audio stream in its own process, does audio-rate analysis
(mean / min / max / gate / trigger), and rebroadcasts to one or more OSC
destinations at a fixed control rate. A watchdog detects both dead streams
and the "still moving but stale" ring-buffer wraparound failure, and
reopens the device automatically.

    python es9_bridge.py --list-devices
    python es9_bridge.py --config es9.toml
    python es9_bridge.py --config es9.toml --simulate
    python es9_bridge.py --config es9.toml --simulate --simulate-fault 20

Status page: http://localhost:8080
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import signal
import sys
import threading
import time
import tomllib
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

# sounddevice ships TWO PortAudio DLLs on Windows -- one with ASIO, one without --
# and loads the non-ASIO one unless this is set BEFORE the import. Without it the
# ES-9's ASIO driver is invisible and you fall back to the Windows audio engine,
# which resamples and mixes: fatal for DC-coupled CV.
if os.environ.get("SD_DISABLE_ASIO") != "1":
    os.environ.setdefault("SD_ENABLE_ASIO", "1")

try:
    import sounddevice as sd
except OSError as exc:  # PortAudio missing -- simulate mode still works
    sd = None
    _SD_ERROR = exc
else:
    _SD_ERROR = None

# Host APIs that hand you the hardware's own samples. Anything else on Windows
# goes through the shared audio engine and is not trustworthy for CV.
BITEXACT_APIS = ("ASIO", "WDM-KS", "WASAPI-EXCLUSIVE")
API_PREFERENCE = ("asio", "wasapi-exclusive", "wdm-ks")

# Exclusive-mode APIs reject the shared-mode default rate, so never trust it:
# probe the rates a UAC2 interface actually runs at.
CANDIDATE_RATES = (48000.0, 44100.0, 96000.0, 88200.0, 192000.0)

from pythonosc.udp_client import SimpleUDPClient
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

from status_page import STATUS_PAGE_HTML

LOG_LOCK = threading.Lock()


def log(msg: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    with LOG_LOCK:
        print(f"[{stamp}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


MODE_PRESETS: dict[str, dict] = {
    "lfo": dict(
        mode="lfo", value="mean", events=True,
        in_range=(-5.0, 5.0), out_range=(0.0, 1.0), clamp=True, smooth=0.2,
        gate_threshold=1.0, gate_hysteresis=0.2, retrigger_lockout_ms=1.0,
        send_minmax=False,
    ),
    "clock": dict(
        mode="clock", value="mean", events=True,
        in_range=(0.0, 5.0), out_range=(0.0, 1.0), clamp=True, smooth=0.0,
        gate_threshold=1.5, gate_hysteresis=0.4, retrigger_lockout_ms=2.0,
        send_minmax=False,
    ),
    "env": dict(
        mode="env", value="mean", events=True,
        in_range=(0.0, 8.0), out_range=(0.0, 1.0), clamp=True, smooth=0.1,
        gate_threshold=1.0, gate_hysteresis=0.2, retrigger_lockout_ms=1.0,
        send_minmax=True,
    ),
    "audio": dict(
        mode="audio", value="rms", events=False,
        in_range=(0.0, 2.0), out_range=(0.0, 1.0), clamp=True, smooth=0.35,
        gate_threshold=1.0, gate_hysteresis=0.2, retrigger_lockout_ms=1.0,
        send_minmax=False,
    ),
}


def preset_public(name: str) -> dict:
    p = MODE_PRESETS[name]
    return {
        **p,
        "in_range": list(p["in_range"]),
        "out_range": list(p["out_range"]),
    }


@dataclass
class ChannelConfig:
    index: int
    name: str = ""
    mode: str = ""
    value: str = ""
    events: bool | None = None
    in_range: tuple[float, float] = (-5.0, 5.0)
    out_range: tuple[float, float] = (0.0, 1.0)
    clamp: bool = True
    smooth: float = 0.0
    gate_threshold: float = 1.0
    gate_hysteresis: float = 0.2
    retrigger_lockout_ms: float = 1.0
    send_minmax: bool = False
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"ch{self.index + 1}"
        if self.mode not in MODE_PRESETS:
            n = self.name.lower()
            if n.startswith("lfo"):
                self.mode = "lfo"
            elif "clock" in n or n in ("clk", "gate"):
                self.mode = "clock"
            elif "env" in n:
                self.mode = "env"
            elif "audio" in n or n == "rms":
                self.mode = "audio"
            else:
                self.mode = "lfo"
        if self.value not in ("mean", "rms"):
            self.value = "rms" if self.mode == "audio" else "mean"
        if self.events is None:
            self.events = self.mode != "audio"

    def as_public(self) -> dict:
        return {
            "n": self.index + 1,
            "name": self.name,
            "mode": self.mode,
            "value": self.value,
            "events": bool(self.events),
            "enabled": self.enabled,
            "in_range": list(self.in_range),
            "out_range": list(self.out_range),
            "clamp": self.clamp,
            "smooth": self.smooth,
            "gate_threshold": self.gate_threshold,
            "gate_hysteresis": self.gate_hysteresis,
            "retrigger_lockout_ms": self.retrigger_lockout_ms,
            "send_minmax": self.send_minmax,
            "threshold": self.gate_threshold,
        }


@dataclass
class Config:
    device: str | int | None = None
    hostapi: str | None = None
    samplerate: float | None = None          # None -> use device default
    blocksize: int = 256
    device_channels: list[int] = field(default_factory=lambda: list(range(8)))
    full_scale_volts: float = 10.0
    osc_rate: float = 60.0
    event_rate: float = 1000.0
    destinations: list[tuple[str, int]] = field(default_factory=lambda: [("127.0.0.1", 7000)])
    address_prefix: str = "/es9"
    control_port: int | None = 7001
    status_port: int | None = 8080
    # bind 127.0.0.1 by default: local-only, and Windows Firewall never prompts
    bind: str = "127.0.0.1"

    # watchdog
    watchdog_enabled: bool = True
    liveness_timeout: float = 0.25
    staleness_enabled: bool = True
    staleness_frozen_blocks: int = 40        # N identical blocks in a row -> frozen
    staleness_window: int = 30               # W-block window used for repeat search
    staleness_history: int = 500             # K blocks of signature history
    reopen_delay: float = 0.15

    channels: list[ChannelConfig] = field(default_factory=list)
    path: str | None = None
    mtime: float = 0.0

    @property
    def nch(self) -> int:
        return len(self.device_channels)


API_ALIASES = {
    "asio": "ASIO",
    "wasapi-exclusive": "Windows WASAPI",
    "wasapi": "Windows WASAPI",
    "wdm-ks": "Windows WDM-KS",
    "mme": "MME",
    "directsound": "Windows DirectSound",
}


def api_candidates(cfg: "Config") -> list[str]:
    """Which host APIs to try, in order."""
    want = (cfg.hostapi or "auto").lower()
    return list(API_PREFERENCE) if want == "auto" else [want]


def api_index(want_api: str) -> int | None:
    if sd is None:
        return None
    api_name = API_ALIASES.get(want_api, want_api)
    try:
        apis = sd.query_hostapis()
    except Exception:
        return None
    return next((i for i, h in enumerate(apis)
                 if api_name.lower() in h["name"].lower()), None)


def find_device(want_api: str, device: str | int | None,
                min_inputs: int = 1) -> tuple[int, dict] | None:
    """Locate the device under a specific host API. Returns (index, device_info).

    ASIO drivers are enumerated by DRIVER name, which often bears no resemblance
    to the Windows device name -- so if the name doesn't match we fall back to
    any input device under that host API with enough channels."""
    if sd is None:
        return None
    api_idx = api_index(want_api)
    if api_idx is None:
        return None

    here = [(i, d) for i, d in enumerate(sd.query_devices())
            if d["hostapi"] == api_idx and d["max_input_channels"] >= 1]
    if not here:
        return None

    if isinstance(device, int):
        return next(((i, d) for i, d in here if i == device), None)

    needle = str(device).lower() if device is not None else "es-9"
    exact = [(i, d) for i, d in here if needle in d["name"].lower()]
    if exact:
        return exact[0]

    # name miss -- fall back to anything on this API that could plausibly be it
    fallback = [(i, d) for i, d in here if d["max_input_channels"] >= min_inputs]
    return fallback[0] if fallback else None


def try_open(want_api: str, dev_index: int, channels: int, cfg: "Config",
             callback=None) -> tuple[object, float, list[str]]:
    """Open a stream, probing sample rates until one is accepted.

    Exclusive-mode APIs reject the shared-mode default, so a single attempt at
    the device default is not a real answer about whether the path works."""
    if cfg.samplerate:
        rates = [float(cfg.samplerate)]
    else:
        rates = []
        try:
            dflt = float(sd.query_devices(dev_index)["default_samplerate"])
            rates.append(dflt)
        except Exception:
            pass
        for r in CANDIDATE_RATES:
            if all(abs(r - x) > 1 for x in rates):
                rates.append(r)

    extra = extra_settings_for(want_api)
    errors: list[str] = []
    for rate in rates:
        kw = dict(device=dev_index, channels=channels, dtype="float32",
                  blocksize=cfg.blocksize, samplerate=rate, latency="low")
        if extra is not None:
            kw["extra_settings"] = extra
        if callback is not None:
            kw["callback"] = callback
        try:
            stream = sd.InputStream(**kw)
            return stream, rate, errors
        except Exception as exc:
            errors.append(f"{rate:g} Hz: {str(exc).strip().splitlines()[0]}")
    raise RuntimeError("; ".join(errors) if errors else "no rate accepted")


def extra_settings_for(want_api: str):
    """PortAudio host-API-specific settings, most importantly WASAPI exclusive."""
    if sd is None:
        return None
    if want_api == "wasapi-exclusive":
        try:
            return sd.WasapiSettings(exclusive=True)
        except Exception:
            return None
    return None


def is_bitexact(want_api: str) -> bool:
    return want_api.upper() in BITEXACT_APIS


def _pair(v, default):
    if v is None:
        return default
    return (float(v[0]), float(v[1]))


def load_config(path: str | None) -> Config:
    cfg = Config()
    raw: dict = {}
    if path:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        cfg.path = path
        cfg.mtime = os.path.getmtime(path)

    dev = raw.get("device", {})
    cfg.device = dev.get("name", cfg.device)
    cfg.hostapi = dev.get("hostapi", cfg.hostapi)
    cfg.samplerate = dev.get("samplerate", cfg.samplerate)
    cfg.blocksize = int(dev.get("blocksize", cfg.blocksize))
    cfg.device_channels = list(dev.get("channels", cfg.device_channels))
    cfg.full_scale_volts = float(dev.get("full_scale_volts", cfg.full_scale_volts))

    osc = raw.get("osc", {})
    cfg.osc_rate = float(osc.get("rate", cfg.osc_rate))
    cfg.event_rate = float(osc.get("event_rate", cfg.event_rate))
    cfg.address_prefix = osc.get("address_prefix", cfg.address_prefix)
    cfg.control_port = osc.get("control_port", cfg.control_port)

    dests = raw.get("destination", [])
    if dests:
        cfg.destinations = [(d["host"], int(d["port"])) for d in dests]

    status = raw.get("status", {})
    cfg.status_port = status.get("port", cfg.status_port)
    cfg.bind = raw.get("network", {}).get("bind", status.get("bind", cfg.bind))

    wd = raw.get("watchdog", {})
    cfg.watchdog_enabled = bool(wd.get("enabled", cfg.watchdog_enabled))
    cfg.liveness_timeout = float(wd.get("liveness_timeout", cfg.liveness_timeout))
    cfg.staleness_enabled = bool(wd.get("staleness_enabled", cfg.staleness_enabled))
    cfg.staleness_frozen_blocks = int(wd.get("frozen_blocks", cfg.staleness_frozen_blocks))
    cfg.staleness_window = int(wd.get("repeat_window", cfg.staleness_window))
    cfg.staleness_history = int(wd.get("history", cfg.staleness_history))
    cfg.reopen_delay = float(wd.get("reopen_delay", cfg.reopen_delay))

    ch_raw = raw.get("channel", {})
    cfg.channels = []
    for i, dev_idx in enumerate(cfg.device_channels):
        spec = ch_raw.get(str(i + 1), {}) or {}
        cfg.channels.append(
            ChannelConfig(
                index=i,
                name=spec.get("name", ""),
                mode=spec.get("mode", "") or "",
                value=spec.get("value", "") or "",
                events=spec["events"] if "events" in spec else None,
                in_range=_pair(spec.get("in_range"), (-5.0, 5.0)),
                out_range=_pair(spec.get("out_range"), (0.0, 1.0)),
                clamp=bool(spec.get("clamp", True)),
                smooth=float(spec.get("smooth", 0.0)),
                gate_threshold=float(spec.get("gate_threshold", 1.0)),
                gate_hysteresis=float(spec.get("gate_hysteresis", 0.2)),
                retrigger_lockout_ms=float(spec.get("retrigger_lockout_ms", 1.0)),
                send_minmax=bool(spec.get("send_minmax", False)),
                enabled=bool(spec.get("enabled", True)),
            )
        )
    return cfg


CHANNELS_TOML_HEADER = """\
# ---------------------------------------------------------------------------
# Channels. Keys are 1-8, matching the OSC addresses /es9/1 ... /es9/8.
# Edit here or on the status page (http://localhost:8080); both hot-reload.
#
#   mode            lfo | clock | env | audio   (preset; knobs below still win)
#   value           mean (DC / CV) or rms (audio amplitude)
#   events          emit /gate and /trig (off for audio so it doesn't chatter)
#   in_range        volts in  -> mapped from this
#   out_range       values out -> mapped to this
#   clamp           clip to out_range (true) or allow overshoot (false)
#   smooth          0.0 = none, 0.9 = heavy. One-pole, applied at the OSC rate.
#   gate_threshold  volts, evaluated at AUDIO rate so short triggers survive
#   gate_hysteresis volts of dead band around the threshold, stops slow-edge chatter
#   retrigger_lockout_ms
#                   minimum gap between events on this channel. Hysteresis alone
#                   cannot save a signal parked exactly on the threshold; this
#                   hard-bounds the event rate. 1 ms allows up to 1 kHz triggers.
#                   Raise it for noisy sources, lower it for audio-rate gates.
#   send_minmax     also emit /es9/N/minmax as [min, max] volts for the frame
# ---------------------------------------------------------------------------
"""


def _toml_num(x: float) -> str:
    s = f"{float(x):.6g}"
    return s if "." in s or "e" in s.lower() else s + ".0"


def _toml_pair(p: tuple[float, float]) -> str:
    return f"[{_toml_num(p[0])}, {_toml_num(p[1])}]"


def format_channel_toml(c: ChannelConfig) -> str:
    lines = [
        f"[channel.{c.index + 1}]",
        f'name = "{c.name.replace(chr(34), "")}"',
        f'mode = "{c.mode}"',
        f'in_range = {_toml_pair(c.in_range)}',
        f'out_range = {_toml_pair(c.out_range)}',
        f"smooth = {_toml_num(c.smooth)}",
        f"gate_threshold = {_toml_num(c.gate_threshold)}",
        f"gate_hysteresis = {_toml_num(c.gate_hysteresis)}",
        f"retrigger_lockout_ms = {_toml_num(c.retrigger_lockout_ms)}",
    ]
    if c.value != "mean":
        lines.append(f'value = "{c.value}"')
    if not c.events:
        lines.append("events = false")
    if c.send_minmax:
        lines.append("send_minmax = true")
    if not c.clamp:
        lines.append("clamp = false")
    if not c.enabled:
        lines.append("enabled = false")
    return "\n".join(lines)


def persist_channels(path: str, channels: list[ChannelConfig]) -> None:
    """Rewrite the [channel.N] tables, leaving the rest of the file intact."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    cut = len(text)
    for marker in ("\n# ---------------------------------------------------------------------------\n# Channels.",
                   "\n[channel."):
        i = text.find(marker)
        if i != -1:
            cut = min(cut, i)
    if text.startswith("[channel."):
        prefix = ""
    else:
        prefix = text[:cut].rstrip() + "\n\n"
    body = CHANNELS_TOML_HEADER + "\n" + "\n\n".join(
        format_channel_toml(c) for c in channels
    ) + "\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(prefix + body)
    os.replace(tmp, path)


def channel_from_spec(index: int, spec: dict, base: ChannelConfig | None = None) -> ChannelConfig:
    """Build a ChannelConfig from a status-page / API payload."""
    if spec.get("apply_preset") and spec.get("mode") in MODE_PRESETS:
        merged = dict(MODE_PRESETS[spec["mode"]])
        if base is not None:
            merged["name"] = base.name
            merged["enabled"] = base.enabled
        merged.update({k: spec[k] for k in spec if k not in ("apply_preset", "n", "threshold")})
        spec = merged
        src = ChannelConfig(index=index)
    elif base is not None:
        src = ChannelConfig(
            index=index,
            name=base.name,
            mode=base.mode,
            value=base.value,
            events=base.events,
            in_range=base.in_range,
            out_range=base.out_range,
            clamp=base.clamp,
            smooth=base.smooth,
            gate_threshold=base.gate_threshold,
            gate_hysteresis=base.gate_hysteresis,
            retrigger_lockout_ms=base.retrigger_lockout_ms,
            send_minmax=base.send_minmax,
            enabled=base.enabled,
        )
    else:
        src = ChannelConfig(index=index)

    if "name" in spec and spec["name"]:
        src.name = str(spec["name"]).strip()[:40]
    if "mode" in spec and spec["mode"] in MODE_PRESETS:
        src.mode = spec["mode"]
    if spec.get("value") in ("mean", "rms"):
        src.value = spec["value"]
    if "events" in spec:
        src.events = bool(spec["events"])
    if "enabled" in spec:
        src.enabled = bool(spec["enabled"])
    if "clamp" in spec:
        src.clamp = bool(spec["clamp"])
    if "send_minmax" in spec:
        src.send_minmax = bool(spec["send_minmax"])
    if "in_range" in spec:
        src.in_range = _pair(spec["in_range"], src.in_range)
    if "out_range" in spec:
        src.out_range = _pair(spec["out_range"], src.out_range)
    for key in ("smooth", "gate_threshold", "gate_hysteresis", "retrigger_lockout_ms"):
        if key in spec:
            setattr(src, key, float(spec[key]))
    src.__post_init__()
    return src


# ---------------------------------------------------------------------------
# Shared state written by the audio callback
# ---------------------------------------------------------------------------


class Analyzer:
    """Audio-rate analysis. All heavy lifting happens here, in the PortAudio
    callback thread. No allocation beyond small temporaries, no I/O, no logs."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        n = cfg.nch
        self.n = n
        self.lock = threading.Lock()

        # accumulators, protected by self.lock (held for ~microseconds)
        self.acc_sum = np.zeros(n, dtype=np.float64)
        self.acc_sumsq = np.zeros(n, dtype=np.float64)
        self.acc_n = 0
        self.acc_min = np.full(n, np.inf)
        self.acc_max = np.full(n, -np.inf)

        # gate state machine, callback-thread only
        self.gate_state = np.full(n, -1, dtype=np.int8)

        # events -> deque is atomic enough in CPython for append/popleft
        self.events: deque = deque(maxlen=8192)

        # health
        self.block_count = 0
        self.last_callback_ns = time.monotonic_ns()
        self.xrun_count = 0
        self.last_status = ""

        # staleness signatures
        self.signatures: deque = deque(maxlen=cfg.staleness_history)
        rng = np.random.default_rng(0xE59)
        self._sig_weights = rng.standard_normal(cfg.blocksize * n if cfg.blocksize else 4096)

        # thresholds in normalized ADC units
        fs = cfg.full_scale_volts
        self.thr_hi = np.array(
            [(c.gate_threshold + c.gate_hysteresis / 2.0) / fs for c in cfg.channels]
        )
        self.thr_lo = np.array(
            [(c.gate_threshold - c.gate_hysteresis / 2.0) / fs for c in cfg.channels]
        )
        # retrigger lockout: hysteresis alone cannot save a signal parked exactly
        # on the threshold, so bound the event rate per channel as well.
        self.lockout_ns = np.array(
            [int(c.retrigger_lockout_ms * 1e6) for c in cfg.channels], dtype=np.int64
        )
        self.last_event_ns = np.zeros(n, dtype=np.int64)
        self.suppressed = 0
        self.events_on = np.array(
            [bool(c.events) for c in cfg.channels], dtype=bool
        ) if cfg.channels else np.ones(n, dtype=bool)

    def apply_channel_settings(self, channels: list[ChannelConfig], fs: float) -> None:
        """Hot-swap thresholds / event masks after a config or UI edit."""
        self.cfg.channels = channels
        self.thr_hi = np.array(
            [(c.gate_threshold + c.gate_hysteresis / 2.0) / fs for c in channels]
        )
        self.thr_lo = np.array(
            [(c.gate_threshold - c.gate_hysteresis / 2.0) / fs for c in channels]
        )
        self.lockout_ns = np.array(
            [int(c.retrigger_lockout_ms * 1e6) for c in channels], dtype=np.int64
        )
        self.events_on = np.array([bool(c.events) for c in channels], dtype=bool)

    # -- signature ---------------------------------------------------------
    def _signature(self, block: np.ndarray) -> float:
        flat = block.reshape(-1)
        w = self._sig_weights
        if w.size < flat.size:
            rng = np.random.default_rng(0xE59)
            self._sig_weights = w = rng.standard_normal(flat.size)
        return float(np.dot(flat, w[: flat.size]))

    # -- the callback ------------------------------------------------------
    def process(self, block: np.ndarray, t_ns: int, status_text: str = "") -> None:
        """block: (frames, nch) float32/float64, already channel-selected."""
        frames = block.shape[0]
        self.block_count += 1
        self.last_callback_ns = t_ns
        if status_text:
            self.xrun_count += 1
            self.last_status = status_text

        # --- block statistics
        bmean = block.mean(axis=0)
        bmin = block.min(axis=0)
        bmax = block.max(axis=0)

        with self.lock:
            self.acc_sum += bmean * frames
            self.acc_sumsq += np.square(np.asarray(block, dtype=np.float64)).sum(axis=0)
            self.acc_n += frames
            np.minimum(self.acc_min, bmin, out=self.acc_min)
            np.maximum(self.acc_max, bmax, out=self.acc_max)

        # --- audio-rate gate / trigger detection with hysteresis
        lvl = np.empty((frames + 1, self.n), dtype=np.int8)
        lvl[0] = self.gate_state                      # always +-1, never 0
        above = block > self.thr_hi
        below = block < self.thr_lo
        np.copyto(lvl[1:], np.where(above, 1, np.where(below, -1, 0)).astype(np.int8))

        # forward-fill the zeros so every sample carries a definite state
        idx = np.where(lvl != 0, np.arange(frames + 1)[:, None], 0)
        np.maximum.accumulate(idx, axis=0, out=idx)
        filled = np.take_along_axis(lvl, idx, axis=0)

        prev = filled[:-1]
        curr = filled[1:]
        rise = (curr == 1) & (prev == -1)
        fall = (curr == -1) & (prev == 1)
        self.gate_state = filled[-1].copy()

        if rise.any() or fall.any():
            sr = self.cfg.samplerate or 48000.0
            edges = []
            for smp, ch in np.argwhere(rise):
                edges.append((t_ns + int(smp / sr * 1e9), int(ch), 1))
            for smp, ch in np.argwhere(fall):
                edges.append((t_ns + int(smp / sr * 1e9), int(ch), 0))
            edges.sort()
            for t_ev, ch, up in edges:
                if ch >= len(self.events_on) or not self.events_on[ch]:
                    continue
                if t_ev - self.last_event_ns[ch] < self.lockout_ns[ch]:
                    self.suppressed += 1
                    continue
                self.last_event_ns[ch] = t_ev
                if up:
                    self.events.append(("trig", ch, t_ev))
                self.events.append(("gate", ch, up))

        # --- staleness signature
        if self.cfg.staleness_enabled:
            self.signatures.append(self._signature(block))

    # -- consumed by the sender thread ------------------------------------
    def take_frame(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
        with self.lock:
            if self.acc_n == 0:
                return None
            mean = self.acc_sum / self.acc_n
            rms = np.sqrt(self.acc_sumsq / self.acc_n)
            mn = self.acc_min.copy()
            mx = self.acc_max.copy()
            self.acc_sum[:] = 0.0
            self.acc_sumsq[:] = 0.0
            self.acc_n = 0
            self.acc_min[:] = np.inf
            self.acc_max[:] = -np.inf
        return mean, mn, mx, rms


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


class Watchdog:
    """Two independent stall detectors.

    liveness  -- the callback stopped firing entirely.
    staleness -- the callback is still firing but delivering repeated data,
                 which is what ring-buffer wraparound looks like. Detected
                 either as N bit-identical blocks in a row (frozen buffer)
                 or as a W-block window that exactly repeats an earlier one
                 (looping buffer).
    """

    def __init__(self, cfg: Config, analyzer: Analyzer):
        self.cfg = cfg
        self.analyzer = analyzer

    def check(self) -> str | None:
        a = self.analyzer
        cfg = self.cfg

        age = (time.monotonic_ns() - a.last_callback_ns) / 1e9
        if age > cfg.liveness_timeout:
            return f"liveness: no callback for {age * 1000:.0f} ms"

        if not cfg.staleness_enabled:
            return None

        sigs = list(a.signatures)
        if len(sigs) < cfg.staleness_frozen_blocks:
            return None

        tail = sigs[-cfg.staleness_frozen_blocks:]
        if all(s == tail[0] for s in tail):
            return f"staleness: {len(tail)} bit-identical blocks (frozen buffer)"

        W = cfg.staleness_window
        if len(sigs) >= 2 * W + 1:
            arr = np.asarray(sigs)
            win = arr[-W:]
            # exact equality is correct here: identical PCM -> identical dot product
            for i in range(len(arr) - 2 * W + 1):
                if np.array_equal(arr[i:i + W], win):
                    lag = len(arr) - W - i
                    return f"staleness: {W}-block window repeats at lag {lag} (looping buffer)"
        return None


# ---------------------------------------------------------------------------
# OSC output
# ---------------------------------------------------------------------------


class OscOut:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.clients: list[tuple[str, SimpleUDPClient]] = []
        self.rebuild()
        self.sent = 0

    def rebuild(self) -> None:
        self.clients = [
            (f"{h}:{p}", SimpleUDPClient(h, p)) for h, p in self.cfg.destinations
        ]

    def send(self, address: str, value) -> None:
        for _, c in self.clients:
            try:
                c.send_message(address, value)
            except OSError:
                pass
        self.sent += 1


# ---------------------------------------------------------------------------
# Signal sources: real device, or simulator
# ---------------------------------------------------------------------------


class DeviceSource:
    """Wraps a PortAudio InputStream. Reopenable."""

    def __init__(self, cfg: Config, analyzer: Analyzer, bridge: "Bridge"):
        self.cfg = cfg
        self.analyzer = analyzer
        self.bridge = bridge
        self.stream: "sd.InputStream | None" = None
        self.max_ch = max(cfg.device_channels) + 1
        self.sel = np.asarray(cfg.device_channels, dtype=np.intp)
        self.info: dict = {}
        self.active_api: str | None = None

    def _callback(self, indata, frames, time_info, status):
        self.analyzer.process(
            indata[:, self.sel], time.monotonic_ns(), str(status) if status else ""
        )

    def open(self) -> None:
        if sd is None:
            raise RuntimeError(f"PortAudio unavailable: {_SD_ERROR}")

        errors = []
        for want_api in api_candidates(self.cfg):
            found = find_device(want_api, self.cfg.device, self.max_ch)
            if not found:
                errors.append(f"{want_api}: device not present under this host API")
                continue
            dev_index, d = found
            try:
                self._open_on(want_api, dev_index, d)
                return
            except Exception as exc:
                errors.append(f"{want_api}: {exc}")

        raise RuntimeError(
            "could not open the ES-9 on any suitable host API.\n    "
            + "\n    ".join(errors)
            + "\n  Run with --check for a full host API report."
        )

    def _open_on(self, want_api: str, dev_index: int, d: dict) -> None:
        stream, _rate, _tried = try_open(
            want_api, dev_index, self.max_ch, self.cfg, callback=self._callback
        )
        stream.start()
        self.stream = stream
        self.active_api = want_api

        # authoritative values, read back from the driver
        self.cfg.samplerate = float(stream.samplerate)
        ha = sd.query_hostapis(d["hostapi"])["name"]
        label = "WASAPI (exclusive)" if want_api == "wasapi-exclusive" else ha
        self.info = {
            "device": d["name"],
            "hostapi": label,
            "bitexact": is_bitexact(want_api),
            "samplerate": float(stream.samplerate),
            "blocksize": int(stream.blocksize or self.cfg.blocksize),
            "latency_ms": round(float(stream.latency) * 1000, 2),
            "device_channels": self.cfg.device_channels,
        }
        log(
            f"stream open: {d['name']} via {label} @ {self.cfg.samplerate:g} Hz, "
            f"block {self.info['blocksize']}, latency {self.info['latency_ms']} ms"
        )
        if not is_bitexact(want_api):
            log("WARNING: this host API goes through the Windows audio engine, which "
                "resamples and mixes. Values will NOT be bit-exact -- unusable for CV. "
                "Run --check to find a bit-exact path.")

    def close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.abort()
            except Exception:
                pass
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None


class SimulatedSource:
    """Synthetic 8-channel signal generator so the bridge (and your TD patch)
    can be developed and tested with no hardware attached.

    --simulate-fault N injects a wraparound-style stall after N seconds so you
    can watch the watchdog detect and recover from it."""

    def __init__(self, cfg: Config, analyzer: Analyzer, bridge: "Bridge", fault_after: float | None):
        self.cfg = cfg
        self.analyzer = analyzer
        self.bridge = bridge
        self.fault_after = fault_after
        self.thread: threading.Thread | None = None
        self.running = False
        self.t0 = 0.0
        self.phase = 0
        self.frozen_block: np.ndarray | None = None
        self.info: dict = {}
        cfg.samplerate = cfg.samplerate or 48000.0
        self.sr = cfg.samplerate
        self.bs = cfg.blocksize or 256

    def _generate(self, t: float) -> np.ndarray:
        n = self.cfg.nch
        idx = np.arange(self.bs) / self.sr + t
        out = np.empty((self.bs, n), dtype=np.float32)
        rng = np.random.default_rng()
        for c in range(n):
            if c % 4 == 0:      # slow sine LFO, +-5 V
                sig = 0.5 * np.sin(2 * math.pi * (0.2 + 0.05 * c) * idx)
            elif c % 4 == 1:    # triangle
                sig = 0.5 * (2 * np.abs(2 * ((idx * 0.35) % 1) - 1) - 1)
            elif c % 4 == 2:    # gate train at ~2 Hz, 0..+5 V
                sig = 0.5 * (((idx * 2.0) % 1) < 0.3).astype(np.float32)
            else:               # slow ramp with noise
                sig = 0.4 * ((idx * 0.13) % 1) - 0.2
            out[:, c] = sig
        out += rng.standard_normal(out.shape).astype(np.float32) * 1e-5  # ADC dither
        return out

    def _run(self) -> None:
        period = self.bs / self.sr
        next_t = time.monotonic()
        while self.running:
            now = time.monotonic()
            elapsed = now - self.t0
            faulting = self.fault_after is not None and elapsed > self.fault_after

            if faulting:
                if self.frozen_block is None:
                    # capture ~0.5 s of material and loop it: wraparound emulation
                    nblocks = max(4, int(0.5 / period))
                    self.frozen_block = np.stack(
                        [self._generate(elapsed + i * period) for i in range(nblocks)]
                    )
                    log(f"SIMULATED FAULT: looping {nblocks} blocks of stale audio")
                block = self.frozen_block[self.phase % len(self.frozen_block)]
                self.phase += 1
            else:
                block = self._generate(elapsed)

            self.analyzer.process(block, time.monotonic_ns())

            next_t += period
            sleep = next_t - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_t = time.monotonic()

    def open(self) -> None:
        self.t0 = time.monotonic()
        self.phase = 0
        self.frozen_block = None
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="simulate")
        self.thread.start()
        self.info = {
            "device": "SIMULATED (no hardware)",
            "hostapi": "simulate",
            "samplerate": self.sr,
            "blocksize": self.bs,
            "latency_ms": round(self.bs / self.sr * 1000, 2),
            "device_channels": self.cfg.device_channels,
        }
        log(f"simulator running @ {self.sr:g} Hz, block {self.bs}")

    def close(self) -> None:
        self.running = False
        if self.thread:
            self.thread.join(timeout=1.0)
            self.thread = None
        # a reopen after a simulated fault should behave like a real recovery
        self.fault_after = None


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class Bridge:
    def __init__(self, cfg: Config, simulate: bool = False, fault_after: float | None = None):
        self.cfg = cfg
        self.analyzer = Analyzer(cfg)
        self.watchdog = Watchdog(cfg, self.analyzer)
        self.osc = OscOut(cfg)
        self.simulate = simulate

        if simulate:
            self.source = SimulatedSource(cfg, self.analyzer, self, fault_after)
        else:
            self.source = DeviceSource(cfg, self.analyzer, self)

        self.running = False
        self.started_at = time.time()
        self.recoveries: deque = deque(maxlen=200)
        self.recovery_count = 0

        n = cfg.nch
        self.smoothed = np.zeros(n)
        self.last_out = np.zeros(n)
        self.last_volts = np.zeros(n)
        self.last_min = np.zeros(n)
        self.last_max = np.zeros(n)
        self.gates = np.zeros(n, dtype=int)
        self.trig_counts = np.zeros(n, dtype=int)
        self.frames_sent = 0

        # auto-calibration
        self.calibrating: set[int] = set()
        self.cal_min = np.full(n, np.inf)
        self.cal_max = np.full(n, -np.inf)

        self.threads: list[threading.Thread] = []
        self.control_server: ThreadingOSCUDPServer | None = None
        self.status_server: ThreadingHTTPServer | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.running = True
        _bump_timer_resolution()
        self.source.open()

        self._spawn(self._sender_loop, "sender")
        if self.cfg.watchdog_enabled:
            self._spawn(self._watchdog_loop, "watchdog")
        self._spawn(self._console_loop, "console")

        if self.cfg.control_port:
            self._start_control_server()
        if self.cfg.status_port:
            self._start_status_server()

    def _spawn(self, fn, name: str) -> None:
        t = threading.Thread(target=fn, name=name, daemon=True)
        t.start()
        self.threads.append(t)

    def stop(self) -> None:
        self.running = False
        self.source.close()
        if self.control_server:
            self.control_server.shutdown()
        if self.status_server:
            self.status_server.shutdown()

    # -- sender ------------------------------------------------------------
    def _sender_loop(self) -> None:
        cfg = self.cfg
        tick = 1.0 / cfg.event_rate
        frame_period = 1.0 / cfg.osc_rate
        next_frame = time.monotonic()
        next_tick = time.monotonic()

        while self.running:
            # events first: triggers and gate changes go out immediately,
            # off the 60 Hz grid, so nothing is ever quantized away.
            ev = self.analyzer.events
            while ev:
                try:
                    kind, ch, payload = ev.popleft()
                except IndexError:
                    break
                c = cfg.channels[ch]
                if not c.enabled:
                    continue
                if kind == "trig":
                    self.trig_counts[ch] += 1
                    self.osc.send(f"{cfg.address_prefix}/{ch + 1}/trig", 1)
                elif kind == "gate":
                    self.gates[ch] = payload
                    self.osc.send(f"{cfg.address_prefix}/{ch + 1}/gate", int(payload))

            now = time.monotonic()
            if now >= next_frame:
                self._send_values()
                next_frame += frame_period
                if next_frame < now:          # we fell behind; resync
                    next_frame = now + frame_period

            next_tick += tick
            sleep = next_tick - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_tick = time.monotonic()

    def _send_values(self) -> None:
        taken = self.analyzer.take_frame()
        if taken is None:
            return
        mean, mn, mx, rms = taken
        cfg = self.cfg
        fs = cfg.full_scale_volts

        mean_v = mean * fs
        rms_v = rms * fs
        self.last_min = mn * fs
        self.last_max = mx * fs

        if self.calibrating:
            for ch in self.calibrating:
                self.cal_min[ch] = min(self.cal_min[ch], self.last_min[ch])
                self.cal_max[ch] = max(self.cal_max[ch], self.last_max[ch])

        for c in cfg.channels:
            if not c.enabled:
                continue
            i = c.index
            volts = float(rms_v[i] if c.value == "rms" else mean_v[i])
            self.last_volts[i] = volts
            lo, hi = c.in_range
            span = (hi - lo) or 1e-9
            t = (volts - lo) / span
            if c.clamp:
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
            olo, ohi = c.out_range
            v = olo + t * (ohi - olo)

            a = 1.0 - min(max(c.smooth, 0.0), 0.999)
            self.smoothed[i] += (v - self.smoothed[i]) * a
            out = float(self.smoothed[i])
            self.last_out[i] = out

            self.osc.send(f"{cfg.address_prefix}/{i + 1}", out)
            if c.send_minmax:
                self.osc.send(
                    f"{cfg.address_prefix}/{i + 1}/minmax",
                    [float(self.last_min[i]), float(self.last_max[i])],
                )
        self.frames_sent += 1

    # -- watchdog ----------------------------------------------------------
    def _watchdog_loop(self) -> None:
        time.sleep(1.0)                       # let the stream settle
        while self.running:
            time.sleep(0.2)
            if not self.running:
                break
            try:
                reason = self.watchdog.check()
            except Exception as exc:
                log(f"watchdog check error: {exc}")
                continue
            if reason:
                self._recover(reason)
                time.sleep(1.5)               # settle before re-arming
            self._maybe_reload_config()

    def _recover(self, reason: str) -> None:
        self.recovery_count += 1
        log(f"STALL DETECTED -- {reason}")
        entry = {
            "n": self.recovery_count,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "uptime_s": round(time.time() - self.started_at, 1),
            "reason": reason,
            "ok": False,
        }
        t0 = time.monotonic()
        try:
            self.source.close()
            time.sleep(self.cfg.reopen_delay)
            self.analyzer.signatures.clear()
            self.analyzer.last_callback_ns = time.monotonic_ns()
            self.source.open()
            entry["ok"] = True
            entry["recover_ms"] = round((time.monotonic() - t0) * 1000, 1)
            log(f"recovered in {entry['recover_ms']} ms (recovery #{self.recovery_count})")
        except Exception as exc:
            entry["error"] = str(exc)
            log(f"REOPEN FAILED: {exc} -- retrying on next check")
        self.recoveries.appendleft(entry)

    def _maybe_reload_config(self) -> None:
        p = self.cfg.path
        if not p:
            return
        try:
            m = os.path.getmtime(p)
        except OSError:
            return
        if m <= self.cfg.mtime:
            return
        try:
            new = load_config(p)
        except Exception as exc:
            log(f"config reload failed: {exc}")
            self.cfg.mtime = m
            return
        # hot-swap only the things that are safe to change live
        self.cfg.mtime = m
        self.cfg.destinations = new.destinations
        self.cfg.full_scale_volts = new.full_scale_volts
        self.cfg.address_prefix = new.address_prefix
        self.osc.rebuild()
        self._apply_channels(new.channels)
        log(f"config reloaded from {p}")

    def _apply_channels(self, channels: list[ChannelConfig]) -> None:
        self.cfg.channels = channels
        self.analyzer.apply_channel_settings(channels, self.cfg.full_scale_volts)

    def update_channel(self, n: int, spec: dict) -> ChannelConfig:
        """Apply a status-page edit to channel n (1-based) and persist if possible."""
        if n < 1 or n > len(self.cfg.channels):
            raise ValueError(f"channel {n} out of range")
        idx = n - 1
        chans = list(self.cfg.channels)
        chans[idx] = channel_from_spec(idx, spec, chans[idx])
        self._apply_channels(chans)
        if self.cfg.path:
            persist_channels(self.cfg.path, chans)
            try:
                self.cfg.mtime = os.path.getmtime(self.cfg.path)
            except OSError:
                pass
            log(f"ch{n} saved ({chans[idx].mode} / {chans[idx].name})")
        else:
            log(f"ch{n} updated in memory (no config file to persist)")
        return chans[idx]

    # -- console -----------------------------------------------------------
    def _console_loop(self) -> None:
        while self.running:
            time.sleep(1.0)
            a = self.analyzer
            rate = a.block_count
            a.block_count = 0
            vals = " ".join(f"{v:5.2f}" for v in self.last_out[: self.cfg.nch])
            gates = "".join("#" if g else "." for g in self.gates)
            log(
                f"cb/s {rate:4d} | frames {self.frames_sent:6d} | "
                f"recover {self.recovery_count} | xrun {a.xrun_count} | "
                f"[{vals}] gates {gates}"
            )

    # -- control (OSC in) --------------------------------------------------
    def _start_control_server(self) -> None:
        disp = Dispatcher()

        def cal_start(addr, *args):
            ch = int(addr.rsplit("/", 1)[-1]) - 1
            self.calibrating.add(ch)
            self.cal_min[ch] = np.inf
            self.cal_max[ch] = -np.inf
            log(f"calibrating ch{ch + 1}: sweep the CV now, send /bridge/calibrate/{ch + 1}/stop")

        def cal_stop(addr, *args):
            ch = int(addr.split("/")[-2]) - 1
            self.calibrating.discard(ch)
            lo, hi = float(self.cal_min[ch]), float(self.cal_max[ch])
            if math.isfinite(lo) and math.isfinite(hi) and hi > lo:
                self.cfg.channels[ch].in_range = (lo, hi)
                if self.cfg.path:
                    persist_channels(self.cfg.path, self.cfg.channels)
                    try:
                        self.cfg.mtime = os.path.getmtime(self.cfg.path)
                    except OSError:
                        pass
                    log(f"ch{ch + 1} in_range = [{lo:.3f}, {hi:.3f}] V  (saved to config)")
                else:
                    log(f"ch{ch + 1} in_range = [{lo:.3f}, {hi:.3f}] V  (copy into your config to persist)")

        def do_reset(addr, *args):
            self._recover("manual reset via OSC")

        for i in range(self.cfg.nch):
            disp.map(f"/bridge/calibrate/{i + 1}", cal_start)
            disp.map(f"/bridge/calibrate/{i + 1}/stop", cal_stop)
        disp.map("/bridge/reset", do_reset)

        self.control_server = ThreadingOSCUDPServer((self.cfg.bind, self.cfg.control_port), disp)
        self._spawn(self.control_server.serve_forever, "control")
        log(f"OSC control listening on {self.cfg.bind}:{self.cfg.control_port}")

    # -- status page -------------------------------------------------------
    def snapshot(self) -> dict:
        cfg = self.cfg
        a = self.analyzer
        age_ms = (time.monotonic_ns() - a.last_callback_ns) / 1e6
        return {
            "uptime_s": round(time.time() - self.started_at, 1),
            "device": self.source.info,
            "simulate": self.simulate,
            "osc_rate": cfg.osc_rate,
            "destinations": [f"{h}:{p}" for h, p in cfg.destinations],
            "address_prefix": cfg.address_prefix,
            "full_scale_volts": cfg.full_scale_volts,
            "healthy": age_ms < cfg.liveness_timeout * 1000,
            "callback_age_ms": round(age_ms, 1),
            "xruns": a.xrun_count,
            "suppressed": a.suppressed,
            "last_status": a.last_status,
            "frames_sent": self.frames_sent,
            "messages_sent": self.osc.sent,
            "recovery_count": self.recovery_count,
            "recoveries": list(self.recoveries),
            "watchdog_enabled": cfg.watchdog_enabled,
            "staleness_enabled": cfg.staleness_enabled,
            "presets": {k: preset_public(k) for k in MODE_PRESETS},
            "channels": [
                {**c.as_public(), **self._channel_live(c)}
                for c in cfg.channels
            ],
        }

    def _channel_live(self, c: ChannelConfig) -> dict:
        i = c.index
        return {
            "volts": round(float(self.last_volts[i]), 4),
            "min_v": round(float(self.last_min[i]), 4),
            "max_v": round(float(self.last_max[i]), 4),
            "out": round(float(self.last_out[i]), 4),
            "gate": int(self.gates[i]),
            "trigs": int(self.trig_counts[i]),
            "calibrating": i in self.calibrating,
        }

    def snapshot_live(self) -> dict:
        """Cheap telemetry for the status-page meters. OSC is unaffected."""
        a = self.analyzer
        age_ms = (time.monotonic_ns() - a.last_callback_ns) / 1e6
        return {
            "healthy": age_ms < self.cfg.liveness_timeout * 1000,
            "callback_age_ms": round(age_ms, 1),
            "xruns": a.xrun_count,
            "suppressed": a.suppressed,
            "frames_sent": self.frames_sent,
            "messages_sent": self.osc.sent,
            "osc_rate": self.cfg.osc_rate,
            "last_status": a.last_status,
            "channels": [self._channel_live(c) | {"n": c.index + 1}
                         for c in self.cfg.channels],
        }

    def _start_status_server(self) -> None:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):    # silence per-request logging
                pass

            def _json(self, code: int, obj) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/api/live"):
                    self._json(200, bridge.snapshot_live())
                elif self.path.startswith("/api/state"):
                    self._json(200, bridge.snapshot())
                elif self.path.startswith("/api/reset"):
                    threading.Thread(
                        target=bridge._recover, args=("manual reset from status page",),
                        daemon=True,
                    ).start()
                    self.send_response(204)
                    self.end_headers()
                else:
                    body = STATUS_PAGE_HTML.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode() or "{}")
                except json.JSONDecodeError:
                    self._json(400, {"error": "invalid json"})
                    return
                if self.path.startswith("/api/channel/"):
                    tail = self.path.rsplit("/", 1)[-1]
                    try:
                        n = int(tail)
                        ch = bridge.update_channel(n, payload if isinstance(payload, dict) else {})
                    except ValueError as exc:
                        self._json(400, {"error": str(exc)})
                        return
                    except Exception as exc:
                        self._json(500, {"error": str(exc)})
                        return
                    self._json(200, ch.as_public())
                else:
                    self.send_response(404)
                    self.end_headers()

        self.status_server = ThreadingHTTPServer((self.cfg.bind, self.cfg.status_port), Handler)
        self.status_server.daemon_threads = True
        self._spawn(self.status_server.serve_forever, "status")
        log(f"status page: http://localhost:{self.cfg.status_port}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _bump_timer_resolution() -> None:
    """Windows default timer granularity is ~15.6 ms, which wrecks a 1 kHz
    event loop. Ask for 1 ms."""
    if platform.system() == "Windows":
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
            log("timer resolution set to 1 ms")
        except Exception as exc:
            log(f"could not set timer resolution: {exc}")


def list_devices() -> None:
    """Every input device, grouped by host API. Grouping matters because the same
    hardware appears once per API, and ASIO names it after the DRIVER."""
    if sd is None:
        print(f"PortAudio unavailable: {_SD_ERROR}")
        return
    apis = sd.query_hostapis()
    devs = list(enumerate(sd.query_devices()))
    print()
    for ai, api in enumerate(apis):
        here = [(i, d) for i, d in devs
                if d["hostapi"] == ai and d["max_input_channels"] >= 1]
        exact = "bit-exact" if any(k in api["name"].upper() for k in ("ASIO", "WDM-KS")) else \
                "exclusive mode only" if "WASAPI" in api["name"].upper() else "shared -- not for CV"
        print(f"  {api['name']}  [{exact}]")
        if not here:
            print("      (no input devices enumerated)")
            if "ASIO" in api["name"].upper():
                print("      ASIO drivers cannot be enumerated while another app holds "
                      "them -- close TouchDesigner and try again.")
        for i, d in here:
            star = "*" if any(k in d["name"].lower() for k in ("es-9", "es9", "expert")) else " "
            print(f"    {star} [{i:>3}] {d['max_input_channels']:>3} in  "
                  f"{d['default_samplerate']:>7,.0f} Hz   {d['name']}")
        print()
    print("  * = looks like your ES-9.")
    print("  Under ASIO the name is the DRIVER's, not Windows' device name -- if it")
    print("  differs, set device.name in es9.toml to match it.")
    print("  Sample rate is probed at open time; you do not need to set it.\n")


def preflight(cfg: Config) -> int:
    """Check every prerequisite and say plainly what is wrong. Exit 0 if ready."""
    import socket

    ok = "  OK  "
    bad = " FAIL "
    warn = " WARN "
    problems = 0
    notes: list[str] = []

    def row(state: str, label: str, detail: str = "") -> None:
        print(f"[{state}] {label:<34} {detail}")

    print("\n=== es9_bridge preflight ===\n")

    # --- Python
    v = sys.version_info
    if (v.major, v.minor) >= (3, 11):
        row(ok, "Python version", f"{v.major}.{v.minor}.{v.micro} ({sys.executable})")
    else:
        problems += 1
        row(bad, "Python version", f"{v.major}.{v.minor} -- need 3.11 or newer (tomllib)")
        notes.append("Install Python 3.13 from python.org, tick 'Add python.exe to PATH'.")

    # --- packages
    for mod, pipname in (("numpy", "numpy"), ("sounddevice", "sounddevice"),
                         ("pythonosc", "python-osc")):
        try:
            m = __import__(mod)
            row(ok, f"package: {pipname}", getattr(m, "__version__", "installed"))
        except ImportError:
            problems += 1
            row(bad, f"package: {pipname}", "not installed")
            notes.append(f"Run:  py -m pip install {pipname}")
        except OSError as exc:
            problems += 1
            row(bad, f"package: {pipname}", str(exc))

    # --- PortAudio + host APIs
    if sd is None:
        problems += 1
        row(bad, "PortAudio", str(_SD_ERROR))
    else:
        try:
            apis = [h["name"] for h in sd.query_hostapis()]
            row(ok, "PortAudio host APIs", ", ".join(apis))
            if platform.system() == "Windows" and not any("ASIO" in a for a in apis):
                row(warn, "ASIO host API", "not present in this PortAudio build")
                notes.append(
                    "ASIO is missing. sounddevice ships two PortAudio DLLs and loads the "
                    "non-ASIO one unless SD_ENABLE_ASIO=1 is set BEFORE importing it. "
                    "This script sets it automatically, so if ASIO is still absent your "
                    "sounddevice version may not bundle the ASIO DLL -- WASAPI exclusive "
                    "below is an equally good path."
                )
        except Exception as exc:
            problems += 1
            row(bad, "PortAudio host APIs", str(exc))

    # --- host API shootout: probe every path to the device and open each one.
    #     This is what finds a bit-exact route and catches exclusivity conflicts.
    usable: list[tuple[str, float, float]] = []
    all_rates: dict[str, float] = {}
    if sd is not None:
        print()
        print("  probing every host API for the ES-9 (bit-exact = safe for CV):")
        print(f"    {'host api':<22} {'bit-exact':<10} {'result'}")
        print("    " + "-" * 76)
        need = max(cfg.device_channels) + 1
        any_found = False
        asio_api_present = api_index("asio") is not None
        asio_device_found = False
        detail_lines: list[str] = []

        for want_api in ("asio", "wasapi-exclusive", "wdm-ks", "wasapi", "directsound", "mme"):
            exact = "yes" if is_bitexact(want_api) else "NO"
            if api_index(want_api) is None:
                print(f"    {want_api:<22} {exact:<10} host API not in this PortAudio build")
                continue
            found = find_device(want_api, cfg.device, need)
            if not found:
                print(f"    {want_api:<22} {exact:<10} host API present, but no input "
                      f"device enumerated under it")
                continue
            any_found = True
            if want_api == "asio":
                asio_device_found = True
            dev_index, d = found

            # try the full channel count, then fall back to what the device offers
            for chans in dict.fromkeys([need, d["max_input_channels"]]):
                try:
                    stream, rate, tried = try_open(want_api, dev_index, chans, cfg)
                    with stream as s:
                        sr, lat = float(s.samplerate), float(s.latency) * 1000
                    note = "" if chans == need else f"  (only {chans} ch available)"
                    print(f"    {want_api:<22} {exact:<10} opens at {sr:g} Hz, "
                          f"{lat:.1f} ms, {d['max_input_channels']} in{note}")
                    if tried:
                        detail_lines.append(
                            f"      {want_api}: rejected {len(tried)} rate(s) first -- "
                            + tried[0])
                    all_rates[want_api] = sr
                    if is_bitexact(want_api) and chans >= need:
                        usable.append((want_api, sr, lat))
                    break
                except Exception as exc:
                    msg = str(exc).strip().replace("\n", " ")
                    if chans == list(dict.fromkeys([need, d["max_input_channels"]]))[-1]:
                        print(f"    {want_api:<22} {exact:<10} FAILED -- {msg[:60]}")
                        detail_lines.append(f"      {want_api}: {msg[:150]}")
        if detail_lines:
            print()
            for ln in detail_lines:
                print(ln)
        print()

        if asio_api_present and not asio_device_found:
            row(warn, "ASIO enumerates nothing",
                "host API loaded, no device under it")
            notes.append(
                "ASIO is loaded but no device appears under it. An ASIO driver cannot "
                "be enumerated while another application holds it -- so the overwhelming "
                "likelihood is that TouchDesigner (or a DAW, or VCV Rack) is open right "
                "now with the ES-9. Close it completely and re-run this check. If ASIO "
                "still shows nothing with everything closed, run --list-devices: the "
                "Expert Sleepers ASIO driver may be listed under a name that does not "
                "contain 'ES-9', in which case set device.name to match it."
            )

        if not any_found:
            problems += 1
            row(bad, "device found", f"nothing matching '{cfg.device or 'ES-9'}'")
            notes.append("Run  py es9_bridge.py --list-devices  and set device.name.")
        elif not usable:
            problems += 1
            row(bad, "bit-exact path", "none available")
            notes.append(
                "The ES-9 is visible but only through shared-mode host APIs (MME, "
                "DirectSound, shared WASAPI). Those route through the Windows audio "
                "engine, which resamples and mixes -- your CV would be altered before "
                "you ever saw it. If ASIO and WASAPI-exclusive both FAILED above, "
                "something else holds the device: close TouchDesigner and re-check."
            )
        else:
            best, sr, lat = usable[0]
            row(ok, "bit-exact path", f"{best} at {sr:g} Hz, {lat:.1f} ms")
            row(ok, "sample rate", f"auto-detected {sr:g} Hz from the driver")
            shared = {round(r) for a, r in all_rates.items() if not is_bitexact(a)}
            if shared and any(abs(r - sr) > 1 for r in shared):
                row(warn, "rate disagreement",
                    f"hardware {sr:g} Hz, Windows shared mode "
                    f"{'/'.join(str(r) for r in sorted(shared))} Hz")
                notes.append(
                    f"The hardware runs at {sr:g} Hz but Windows' shared-mode 'Default "
                    f"Format' for this device is {'/'.join(str(r) for r in sorted(shared))} Hz. "
                    "The bridge uses the hardware rate, so it is unaffected -- but check "
                    "that TouchDesigner's Audio Device In CHOP Rate parameter also says "
                    f"{sr:g}. A mismatch there causes exactly the slow queue drift and "
                    "eventual wraparound you have been chasing."
                )

        # channel count sanity, from whichever entry we found
        try:
            need = max(cfg.device_channels) + 1
            f = next((find_device(a, cfg.device) for a in
                      ("asio", "wasapi-exclusive", "wdm-ks", "mme")
                      if find_device(a, cfg.device)), None)
            if f and f[1]["max_input_channels"] < need:
                problems += 1
                row(bad, "channel count",
                    f"config wants {need}, device offers {f[1]['max_input_channels']}")
            elif f:
                row(ok, "channel count",
                    f"{f[1]['max_input_channels']} inputs available, config uses {need}")
        except Exception:
            pass

    # --- ports
    for label, port, proto in (("status page", cfg.status_port, "tcp"),
                               ("OSC control in", cfg.control_port, "udp")):
        if not port:
            continue
        fam = socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM
        s = socket.socket(socket.AF_INET, fam)
        try:
            s.bind((cfg.bind, port))
            row(ok, f"port {port} free", f"{label} ({proto})")
        except OSError as exc:
            problems += 1
            row(bad, f"port {port} busy", f"{label} -- {exc}")
            notes.append(f"Something already uses port {port}. Change it in es9.toml.")
        finally:
            s.close()

    # --- destinations are informational only (UDP send needs nothing bound)
    row(ok, "OSC destinations", ", ".join(f"{h}:{p}" for h, p in cfg.destinations))
    row(ok, "bind address", f"{cfg.bind} (local only -- no firewall prompt)"
        if cfg.bind.startswith("127.") else f"{cfg.bind} (Windows may prompt to allow)")

    print()
    if notes:
        print("What to do:")
        for i, n in enumerate(dict.fromkeys(notes), 1):
            print(f"  {i}. {n}")
        print()
    if problems == 0:
        print("Ready. Start it with:  py es9_bridge.py --config es9.toml\n")
    else:
        print(f"{problems} problem(s) found. Fix the FAIL rows above, then re-run --check.\n")
    return 0 if problems == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="ES-9 -> OSC bridge with stall watchdog")
    ap.add_argument("--config", "-c", help="path to TOML config")
    ap.add_argument("--list-devices", action="store_true", help="list input devices and exit")
    ap.add_argument("--check", action="store_true",
                    help="check every prerequisite and report what is wrong, then exit")
    ap.add_argument("--simulate", action="store_true", help="synthetic signals, no hardware")
    ap.add_argument("--simulate-fault", type=float, metavar="SEC",
                    help="in simulate mode, inject a wraparound stall after SEC seconds")
    ap.add_argument("--device", help="override device name or index")
    ap.add_argument("--samplerate", type=float, help="override sample rate (normally auto)")
    ap.add_argument("--status-port", type=int, help="override status page port")
    ap.add_argument("--no-watchdog", action="store_true", help="disable the watchdog")
    ap.add_argument("--api", choices=["auto", "asio", "wasapi-exclusive", "wdm-ks",
                                      "wasapi", "mme", "directsound"],
                    help="force a host API (default: auto = asio, then wasapi-exclusive, "
                         "then wdm-ks). Use this to take the ASIO driver out of the "
                         "picture as a diagnostic.")
    args = ap.parse_args()

    if args.list_devices:
        list_devices()
        return 0

    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        print(f"config not found: {args.config}")
        return 1
    except Exception as exc:
        print(f"could not parse {args.config}: {exc}")
        return 1

    if args.device is not None:
        cfg.device = int(args.device) if args.device.isdigit() else args.device
    if args.samplerate:
        cfg.samplerate = args.samplerate
    if args.status_port:
        cfg.status_port = args.status_port
    if args.no_watchdog:
        cfg.watchdog_enabled = False
    if args.api:
        cfg.hostapi = args.api

    if args.check:
        return preflight(cfg)

    bridge = Bridge(cfg, simulate=args.simulate, fault_after=args.simulate_fault)

    stopping = threading.Event()

    def handle_sig(signum, frame):
        stopping.set()

    signal.signal(signal.SIGINT, handle_sig)
    try:
        signal.signal(signal.SIGTERM, handle_sig)
    except (AttributeError, ValueError):
        pass

    try:
        bridge.start()
    except Exception as exc:
        log(f"failed to start: {exc}")
        if not args.simulate:
            log("try --list-devices to check the device name, or --simulate to test without hardware")
        return 1

    log("running -- ctrl-C to stop")
    try:
        while not stopping.is_set():
            stopping.wait(0.25)
    finally:
        log("stopping")
        bridge.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
