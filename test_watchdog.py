"""Detector unit tests. Run: python test_watchdog.py"""
import time
import numpy as np
from es9_bridge import Analyzer, Watchdog, load_config

cfg = load_config("es9.toml")
cfg.samplerate = 48000.0
rng = np.random.default_rng(1)


def blk():
    return (rng.standard_normal((256, 8)) * 0.1).astype(np.float32)


def fresh():
    a = Analyzer(cfg)
    return a, Watchdog(cfg, a)


results = []

# 1. healthy stream -> no trip
a, w = fresh()
for _ in range(120):
    a.process(blk(), time.monotonic_ns())
r = w.check()
results.append(("healthy stream, no trip", r is None, r or "OK"))

# 2. frozen buffer: identical block repeated
a, w = fresh()
for _ in range(60):
    a.process(blk(), time.monotonic_ns())
frozen = blk()
for _ in range(45):
    a.process(frozen, time.monotonic_ns())
r = w.check()
results.append(("frozen buffer detected", r is not None and "frozen" in r, r))

# 3. looping buffer: a 20-block loop replayed (your actual symptom)
a, w = fresh()
for _ in range(60):
    a.process(blk(), time.monotonic_ns())
loop = [blk() for _ in range(20)]
for i in range(120):
    a.process(loop[i % 20], time.monotonic_ns())
r = w.check()
results.append(("looping buffer detected", r is not None and "looping" in r, r))

# 4. dead stream
a, w = fresh()
for _ in range(120):
    a.process(blk(), time.monotonic_ns())
a.last_callback_ns = time.monotonic_ns() - int(0.5e9)
r = w.check()
results.append(("dead stream detected", r is not None and "liveness" in r, r))

# 5. false-positive guard: a perfectly periodic LFO with realistic ADC dither
a, w = fresh()
t = 0.0
for _ in range(400):
    idx = np.arange(256) / 48000.0 + t
    sig = np.stack([0.5 * np.sin(2 * np.pi * 2.0 * idx)] * 8, axis=1).astype(np.float32)
    sig += rng.standard_normal(sig.shape).astype(np.float32) * 1e-6
    a.process(sig, time.monotonic_ns())
    t += 256 / 48000.0
r = w.check()
results.append(("periodic LFO -> no false positive", r is None, r or "OK"))

# 6. trigger timestamp accuracy: one clean edge at sample 100 of the block
a, _ = fresh()
a.process(np.full((256, 8), -0.5, np.float32), time.monotonic_ns())
a.events.clear()
b = np.full((256, 8), -0.5, np.float32)
b[100:] = 0.5
t0 = time.monotonic_ns()
a.process(b, t0)
ev = [e for e in a.events if e[0] == "trig"]
off = (ev[0][2] - t0) / 1000 if ev else -1
expect = 100 / 48000 * 1e6
results.append((
    f"mid-block edge timestamp ({off:.0f} us, expect {expect:.0f} us)",
    len(ev) == 8 and abs(off - expect) < 30,
    f"{len(ev)} triggers",
))

# 7. worst case for chatter: signal parked exactly on the threshold with noise
#    at half the hysteresis band. Hysteresis cannot fix this; the retrigger
#    lockout must bound the event rate to 1 / lockout.
a, _ = fresh()
thr = cfg.channels[0].gate_threshold / cfg.full_scale_volts
lockout_s = cfg.channels[0].retrigger_lockout_ms / 1000.0
a.process(np.full((256, 8), -0.5, np.float32), time.monotonic_ns())
a.events.clear()
nblocks, sr = 50, 48000.0
t = time.monotonic_ns()
for _ in range(nblocks):
    noisy = (np.full((256, 8), thr, np.float32)
             + rng.standard_normal((256, 8)).astype(np.float32) * (thr * 0.05))
    a.process(noisy, t)
    t += int(256 / sr * 1e9)
ntrig = sum(1 for e in a.events if e[0] == "trig" and e[1] == 0)
duration = nblocks * 256 / sr
ceiling = int(duration / lockout_s) + 2
results.append((
    f"lockout bounds chatter ({ntrig} trigs in {duration*1000:.0f} ms, ceiling {ceiling})",
    ntrig <= ceiling,
    f"{a.suppressed} edges suppressed",
))

# 8. lockout must not eat legitimate fast triggers (200 Hz trigger train)
a, _ = fresh()
sr = 48000.0
period = int(sr / 200)          # 240 samples -> 5 ms apart, well over the 1 ms lockout
sig = np.full((sr_n := 4800, 8), -0.5, np.float32)
for k in range(0, sr_n, period):
    sig[k:k + period // 4] = 0.5
a.process(np.full((256, 8), -0.5, np.float32), time.monotonic_ns())
a.events.clear()
t = time.monotonic_ns()
for i in range(0, sr_n, 256):
    b = sig[i:i + 256]
    if len(b) < 256:
        break
    a.process(b, t)
    t += int(256 / sr * 1e9)
ntrig = sum(1 for e in a.events if e[0] == "trig" and e[1] == 0)
expect = (sr_n // 256 * 256) // period
results.append((
    f"200 Hz trigger train survives lockout ({ntrig} of ~{expect})",
    abs(ntrig - expect) <= 1,
    f"{ntrig} triggers",
))

print(f"{'test':<52} {'result':<7} detail")
print("-" * 100)
fails = 0
for name, ok, detail in results:
    fails += 0 if ok else 1
    print(f"{name:<52} {'PASS' if ok else 'FAIL':<7} {detail}")
print("-" * 100)
print(f"{len(results) - fails}/{len(results)} passed")
raise SystemExit(1 if fails else 0)
