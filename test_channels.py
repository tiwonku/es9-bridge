"""Channel modes, RMS, event masking, toml persist. Run: python test_channels.py"""
import os
import tempfile
import time
import numpy as np

from es9_bridge import (
    Analyzer, ChannelConfig, channel_from_spec, load_config, persist_channels,
)

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


cfg = load_config("es9.toml")
cfg.samplerate = 48000.0
check("toml infers clock mode", cfg.channels[2].mode == "clock", cfg.channels[2].mode)
check("toml infers env mode", cfg.channels[3].mode == "env", cfg.channels[3].mode)
check("lfo uses mean", cfg.channels[0].value == "mean")
check("clock events on", cfg.channels[2].events is True)

# RMS of a full-scale sine ≈ 1/sqrt(2)
a = Analyzer(cfg)
sr, n = 48000, 4800
t = np.arange(n) / sr
sig = np.stack([np.sin(2 * np.pi * 440 * t)] * 8, axis=1).astype(np.float32)
for i in range(0, n, 256):
    a.process(sig[i:i + 256], time.monotonic_ns())
mean, mn, mx, rms = a.take_frame()
check("sine mean ~ 0", abs(float(mean[0])) < 0.02, f"{mean[0]:.4f}")
check("sine rms ~ 0.707", abs(float(rms[0]) - 0.7071) < 0.01, f"{rms[0]:.4f}")

# audio mode must not emit trigs on a loud AC signal
cfg.channels[0].events = False
cfg.channels[0].value = "rms"
cfg.channels[0].mode = "audio"
a = Analyzer(cfg)
a.apply_channel_settings(cfg.channels, cfg.full_scale_volts)
a.process(np.full((256, 8), -0.5, np.float32), time.monotonic_ns())
a.events.clear()
b = np.full((256, 8), -0.5, np.float32)
b[100:] = 0.5
a.process(b, time.monotonic_ns())
ntrig = sum(1 for e in a.events if e[0] == "trig" and e[1] == 0)
check("events=false suppresses trigs", ntrig == 0, f"{ntrig} trigs")

# other channels still fire
ntrig1 = sum(1 for e in a.events if e[0] == "trig" and e[1] == 1)
check("sibling channel still trigs", ntrig1 == 1, f"{ntrig1} trigs")

ch = channel_from_spec(0, {"apply_preset": True, "mode": "audio"}, cfg.channels[0])
check("audio preset uses rms", ch.value == "rms" and ch.events is False, f"{ch.value}/{ch.events}")
check("audio preset keeps name", ch.name == cfg.channels[0].name, ch.name)

ch2 = channel_from_spec(0, {"smooth": 0.5}, cfg.channels[0])
check("partial edit keeps mode", ch2.mode == cfg.channels[0].mode and ch2.smooth == 0.5)

# persist round-trip (use a fresh load so earlier mutations don't leak)
fresh = load_config("es9.toml")
with tempfile.TemporaryDirectory() as td:
    dst = os.path.join(td, "es9.toml")
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write("[device]\nname = \"ES-9\"\n\n")
    persist_channels(dst, fresh.channels)
    loaded = load_config(dst)
    check("persist keeps device section", "ES-9" in open(dst, encoding="utf-8").read())
    check("persist round-trip names",
          [c.name for c in loaded.channels] == [c.name for c in fresh.channels])
    check("persist round-trip modes",
          [c.mode for c in loaded.channels] == [c.mode for c in fresh.channels])
    check("ch4 still send_minmax", loaded.channels[3].send_minmax is True)

print(f"{'test':<52} {'result':<7} detail")
print("-" * 100)
fails = 0
for name, ok, detail in results:
    fails += 0 if ok else 1
    print(f"{name:<52} {'PASS' if ok else 'FAIL':<7} {detail}")
print("-" * 100)
print(f"{len(results) - fails}/{len(results)} passed")
raise SystemExit(1 if fails else 0)
