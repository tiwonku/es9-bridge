"""Simulates a Windows machine's host API layout to exercise device selection
and the preflight shootout without hardware. Reproduces the reported layout:
ES-9 visible under MME/DirectSound/WASAPI/WDM-KS, ASIO present or absent."""

import sys
import io
import contextlib
import types
import es9_bridge as B


HARDWARE_RATE = 48000.0     # what the ES-9 actually runs at
SHARED_RATE = 44100.0       # Windows' "Default Format" for the device


class FakeStream:
    def __init__(self, **kw):
        self.device = kw["device"]
        self.blocksize = kw.get("blocksize", 256)
        d = FAKE_DEVICES[self.device]
        api = FAKE_APIS[d["hostapi"]]["name"]
        excl = kw.get("extra_settings") is not None
        req_rate = kw.get("samplerate")
        req_ch = kw.get("channels", 2)

        if d.get("busy"):
            raise RuntimeError("Error opening InputStream: Device unavailable [-9985]")

        shared = api in ("MME", "Windows DirectSound") or (api == "Windows WASAPI" and not excl)

        # shared WASAPI only offers the device's shared-mode channel config
        if api == "Windows WASAPI" and not excl and req_ch > 2:
            raise RuntimeError("Error opening InputStream: Invalid number of channels")

        if shared:
            # the audio engine resamples, so it accepts anything
            self.samplerate = SHARED_RATE
            self.latency = 0.029
        else:
            # exclusive paths accept ONLY the hardware rate
            if req_rate is not None and abs(req_rate - HARDWARE_RATE) > 1:
                if api == "Windows WDM-KS":
                    raise RuntimeError("Error opening InputStream: Unanticipated host error")
                raise RuntimeError("Error opening InputStream: Invalid sample rate [PaErrorCode -9997]")
            self.samplerate = HARDWARE_RATE
            self.latency = 0.006

    def start(self): pass
    def abort(self): pass
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass


FAKE_APIS = []
FAKE_DEVICES = []


def install(with_asio: bool, es9_busy_on: tuple = (), asio_enumerates: bool = True,
            asio_driver_name: str = "ES-9", default_sr: float = HARDWARE_RATE):
    """asio_enumerates=False models an ASIO driver held open by another app:
    the host API loads but lists no devices, which is what TouchDesigner does."""
    global FAKE_APIS, FAKE_DEVICES
    names = (["ASIO"] if with_asio else []) + [
        "MME", "Windows DirectSound", "Windows WASAPI", "Windows WDM-KS"]
    FAKE_APIS = [{"name": n} for n in names]
    FAKE_DEVICES = []
    for i, n in enumerate(names):
        if n == "ASIO" and not asio_enumerates:
            continue
        FAKE_DEVICES.append({
            "name": asio_driver_name if n == "ASIO" else "Line (ES-9)",
            "hostapi": i, "max_input_channels": 16,
            # shared-mode entries advertise Windows' Default Format
            "default_samplerate": default_sr if n in ("ASIO", "Windows WDM-KS")
                                  else SHARED_RATE,
            "busy": n in es9_busy_on,
        })
    fake = types.SimpleNamespace(
        query_hostapis=lambda i=None: FAKE_APIS[i] if i is not None else FAKE_APIS,
        query_devices=lambda i=None: FAKE_DEVICES[i] if i is not None else FAKE_DEVICES,
        InputStream=FakeStream,
        WasapiSettings=lambda exclusive=False: ("wasapi", exclusive),
    )
    B.sd = fake


results = []


def check(label, cond, detail=""):
    results.append((label, cond, detail))


cfg = B.load_config("es9.toml")

# --- 1. ASIO present: auto must pick it, and report the hardware rate
install(with_asio=True)
src = B.DeviceSource(cfg, B.Analyzer(cfg), None)
src.open()
check("ASIO present -> auto picks ASIO", src.active_api == "asio", src.active_api)
check("ASIO reports hardware rate 48k", src.info["samplerate"] == 48000.0,
      f"{src.info['samplerate']:g} Hz")
check("ASIO flagged bit-exact", src.info["bitexact"] is True)

# --- 2. ASIO absent (the reported case): must fall through to WASAPI exclusive,
#        NOT to MME, and must still report the true hardware rate
install(with_asio=False)
cfg2 = B.load_config("es9.toml")
src = B.DeviceSource(cfg2, B.Analyzer(cfg2), None)
src.open()
check("no ASIO -> falls back to WASAPI exclusive",
      src.active_api == "wasapi-exclusive", src.active_api)
check("does NOT silently fall back to MME", src.active_api != "mme", src.active_api)
check("exclusive reports 48k, not the 44.1k shared default",
      src.info["samplerate"] == 48000.0, f"{src.info['samplerate']:g} Hz")
check("flagged bit-exact", src.info["bitexact"] is True)

# --- 3. TouchDesigner holding the device on every exclusive path
install(with_asio=True, es9_busy_on=("ASIO", "Windows WASAPI", "Windows WDM-KS"))
cfg3 = B.load_config("es9.toml")
src = B.DeviceSource(cfg3, B.Analyzer(cfg3), None)
try:
    src.open()
    check("all exclusive paths busy -> refuses to open", False, "opened anyway!")
except RuntimeError as exc:
    check("all exclusive paths busy -> refuses to open",
          "could not open" in str(exc), "raised, did not fall back to MME")

# --- 4. forcing a shared API explicitly still works but is flagged non-bit-exact
install(with_asio=False)
cfg4 = B.load_config("es9.toml")
cfg4.hostapi = "mme"
src = B.DeviceSource(cfg4, B.Analyzer(cfg4), None)
src.open()
check("--api mme works but is flagged NOT bit-exact",
      src.active_api == "mme" and src.info["bitexact"] is False,
      f"{src.active_api}, bitexact={src.info['bitexact']}")

# --- 5. preflight on the reported machine reports a usable path and exits 0
install(with_asio=False)
cfg5 = B.load_config("es9.toml")

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = B.preflight(cfg5)
out = buf.getvalue()
check("preflight finds a bit-exact path", "bit-exact path" in out and "wasapi-exclusive" in out)
check("preflight flags the 44.1k/48k disagreement", "rate disagreement" in out,
      "warns that shared mode reports a different rate")
check("preflight passes", rc == 0, f"exit {rc}")

# --- 6. THE REPORTED CASE: ASIO loaded but enumerating nothing (TD holding it),
#        exclusive paths rejecting the shared-mode default rate of 44.1k.
#        Before the rate-probing fix this reported "no bit-exact path available".
install(with_asio=True, asio_enumerates=False)
cfg6 = B.load_config("es9.toml")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc6 = B.preflight(cfg6)
out6 = buf.getvalue()
check("reported case: finds WASAPI-exclusive by probing rates",
      "wasapi-exclusive" in out6 and "opens at 48000 Hz" in out6,
      "rate probing recovers the path that previously FAILED")
check("reported case: names TouchDesigner as the ASIO blocker",
      "TouchDesigner" in out6 and "ASIO enumerates nothing" in out6)
check("reported case: preflight now passes", rc6 == 0, f"exit {rc6}")

# and the bridge must actually open on that machine
src = B.DeviceSource(cfg6, B.Analyzer(cfg6), None)
src.open()
check("reported case: bridge opens bit-exact at 48k",
      src.active_api == "wasapi-exclusive" and src.info["samplerate"] == 48000.0,
      f"{src.active_api} @ {src.info['samplerate']:g} Hz")

# --- 7. ASIO driver listed under a name with no 'ES-9' in it
install(with_asio=True, asio_driver_name="Expert Sleepers USB Audio")
cfg7 = B.load_config("es9.toml")
src = B.DeviceSource(cfg7, B.Analyzer(cfg7), None)
src.open()
check("ASIO driver with a non-matching name is still found",
      src.active_api == "asio", f"{src.active_api} ({src.info['device']})")

# --- 8. shared WASAPI refusing 8 channels must not be mistaken for a usable path
install(with_asio=False)
cfg8 = B.load_config("es9.toml")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    B.preflight(cfg8)
out8 = buf.getvalue()
check("shared WASAPI channel refusal does not count as usable",
      "bit-exact path" in out8 and "wasapi-exclusive at 48000" in out8)

print(f"{'test':<58} {'result'}")
print("-" * 92)
fails = 0
for label, cond, detail in results:
    fails += 0 if cond else 1
    print(f"{label:<58} {'PASS' if cond else 'FAIL':<6} {detail}")
print("-" * 92)
print(f"{len(results)-fails}/{len(results)} passed")
sys.exit(1 if fails else 0)
