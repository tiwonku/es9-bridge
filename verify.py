"""Verification harness: receives the bridge's OSC and checks rates + trigger timing."""
import subprocess, sys, threading, time, statistics, json, urllib.request
from collections import defaultdict
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

recv = defaultdict(list)
lock = threading.Lock()

def handler(addr, *args):
    with lock:
        recv[addr].append((time.monotonic(), args))

d = Dispatcher()
d.set_default_handler(handler)
srv = ThreadingOSCUDPServer(("127.0.0.1", 7000), d)
threading.Thread(target=srv.serve_forever, daemon=True).start()

proc = subprocess.Popen(
    [sys.executable, "-u", "es9_bridge.py", "--config", "es9.toml",
     "--simulate", "--simulate-fault", "9", "--status-port", "8080"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

time.sleep(2.0)
with lock:
    recv.clear()
t0 = time.monotonic()
time.sleep(6.0)
t1 = time.monotonic()
with lock:
    snap = {k: list(v) for k, v in recv.items()}

print(f"=== OSC received over {t1-t0:.1f}s ===")
vals = sorted(k for k in snap if k.count("/") == 2)
for a in vals:
    ts = [t for t, _ in snap[a]]
    rate = len(ts) / (t1 - t0)
    gaps = [b - a2 for a2, b in zip(ts, ts[1:])]
    jit = statistics.stdev(gaps) * 1000 if len(gaps) > 2 else 0
    print(f"  {a:<12} {len(ts):5d} msgs  {rate:6.1f} Hz  gap jitter {jit:.2f} ms")

print("\n=== events ===")
for a in sorted(k for k in snap if k.count("/") == 3):
    print(f"  {a:<22} {len(snap[a]):5d}")

# trigger timing: /es9/3 is the 2 Hz gate train in the simulator
trigs = sorted(t for t, _ in snap.get("/es9/3/trig", []))
if len(trigs) > 3:
    gaps = [b - a2 for a2, b in zip(trigs, trigs[1:])]
    print(f"\n  /es9/3/trig interval: mean {statistics.mean(gaps)*1000:.1f} ms "
          f"(expect 500), stdev {statistics.stdev(gaps)*1000:.2f} ms")
    print(f"  -> {'PASS' if statistics.stdev(gaps)*1000 < 16.7 else 'FAIL'}: "
          f"jitter is {'below' if statistics.stdev(gaps)*1000 < 16.7 else 'above'} "
          f"one 60Hz frame (16.7ms), i.e. triggers are sub-frame accurate")

# gate messages should be on-change only, not per frame
g = snap.get("/es9/3/gate", [])
print(f"\n  /es9/3/gate: {len(g)} msgs in {t1-t0:.0f}s "
      f"-> {'PASS (on-change only)' if len(g) < 60 else 'FAIL (sending every frame)'}")

# status API
try:
    st = json.load(urllib.request.urlopen("http://127.0.0.1:8080/api/state", timeout=2))
    print(f"\n=== status api ===\n  healthy={st['healthy']} sr={st['device']['samplerate']} "
          f"recoveries={st['recovery_count']} channels={len(st['channels'])}")
except Exception as e:
    print(f"  status api FAIL: {e}")

time.sleep(6)
try:
    st = json.load(urllib.request.urlopen("http://127.0.0.1:8080/api/state", timeout=2))
    print(f"\n=== after injected fault ===\n  recoveries={st['recovery_count']}")
    for r in st["recoveries"]:
        print(f"    #{r['n']} {r['reason']} -> recovered in {r.get('recover_ms')} ms")
except Exception as e:
    print(f"  FAIL: {e}")

proc.terminate()
proc.wait(timeout=5)
