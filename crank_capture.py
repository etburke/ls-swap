#!/usr/bin/env python3
"""
crank_capture.py - long single-shot capture + crank-signal dropout analysis
for a Rigol DHO800 (4-ch) over USB/LAN. Aimed at Gen IV LS (58x crank) 0-RPM debugging.

Default wiring (edit CHANNELS below to suit):
  CH1  crank signal at the PCM pin
  CH2  crank signal at the sensor connector
  CH3  5 V reference at the sensor connector
  CH4  PCM low reference vs battery negative
All probe grounds go to BATTERY NEGATIVE (scope channel grounds are common).

Usage:
  python crank_capture.py capture --seconds 6        # arm, you crank, download, analyze
  python crank_capture.py analyze crank_capture.npz  # re-run analysis on a saved file

Setup:  pip install pyvisa pyvisa-py pyusb numpy matplotlib
        (macOS: brew install libusb)
Close any other program/tool holding the scope's USB connection first.
NOTE: SCPI commands are from memory of the DHO800 programming guide. Every command is
followed by an error-queue check that prints anything the scope rejected.
"""
import argparse, json, sys, time
import numpy as np

CHANNELS = {  # scale in V/div, offset in V (negative offset moves a positive signal toward center)
    1: dict(label="Crank @ PCM",           scale=2.0, offset=0.0),
    2: dict(label="Crank @ sensor",        scale=2.0, offset=0.0),
    3: dict(label="5V ref @ sensor",       scale=0.5, offset=-5.0),
    4: dict(label="PCM low ref vs batt -", scale=0.2, offset=0.0),
}

# ----------------------------------------------------------------- scope I/O
class Scope:
    def __init__(self, resource=None):
        import pyvisa
        rm = pyvisa.ResourceManager("@py")
        if not resource:
            found = [r for r in rm.list_resources() if r.startswith(("USB", "TCPIP"))]
            rig = [r for r in found if "0X1AB1" in r.upper()] or found
            if not rig:
                sys.exit("No scope found. Pass --resource (e.g. TCPIP0::192.168.1.50::INSTR).")
            resource = rig[0]
        self.i = rm.open_resource(resource)
        self.i.timeout = 30000
        self.i.chunk_size = 1 << 20
        print("Connected:", self.q("*IDN?"))

    def w(self, cmd):
        self.i.write(cmd)
        self.check(cmd)

    def q(self, cmd):
        return self.i.query(cmd).strip()

    def check(self, ctx=""):
        for _ in range(8):
            e = self.q(":SYSTem:ERRor:NEXT?")
            if e.startswith("0,") or e == "0":
                return
            print(f"  ! scope rejected/complained after '{ctx}': {e}")


def configure(s, a):
    scale = a.seconds / 10.0                       # 10 horizontal divisions
    s.w(":STOP")
    s.w(":TIMebase:MODE MAIN")
    s.w(f":TIMebase:MAIN:SCALe {scale}")
    # put the trigger point at `pretrig` of the screen from the left (verify sign on screen)
    s.w(f":TIMebase:MAIN:OFFSet {(0.5 - a.pretrig) * 10 * scale}")
    s.w(":ACQuire:TYPE NORMal")                    # no averaging / hi-res smoothing of runts
    s.w(f":ACQuire:MDEPth {a.depth}")
    got = s.q(":ACQuire:MDEPth?")
    print(f"Memory depth requested {a.depth}, scope reports {got}")
    for n, c in CHANNELS.items():
        s.w(f":CHANnel{n}:DISPlay ON")
        s.w(f":CHANnel{n}:COUPling DC")
        s.w(f":CHANnel{n}:PROBe 1")
        s.w(f":CHANnel{n}:BWLimit OFF")
        s.w(f":CHANnel{n}:SCALe {c['scale']}")
        s.w(f":CHANnel{n}:OFFSet {c['offset']}")
    s.w(":TRIGger:MODE EDGE")
    s.w(":TRIGger:EDGE:SOURce CHANnel1")
    s.w(":TRIGger:EDGE:SLOPe POSitive")
    s.w(f":TRIGger:EDGE:LEVel {a.trig_level}")
    s.w(":TRIGger:SWEep SINGle")
    print(f"Configured: {a.seconds}s window, depth {a.depth}, "
          f"sample rate now {s.q(':ACQuire:SRATe?')}")


def arm_and_wait(s, wait):
    s.w(":SINGle")
    print("\n>>> ARMED. Crank the engine now. <<<")
    t0, seen_armed = time.time(), False
    while True:
        st = s.q(":TRIGger:STATus?")
        if st != "STOP":
            seen_armed = True
        if st == "STOP" and seen_armed:
            return
        if st == "STOP" and time.time() - t0 > 3:
            sys.exit("Scope never armed after :SINGle (status stayed STOP); check the SCPI error output above.")
        if time.time() - t0 > wait:
            sys.exit("Timed out waiting for trigger (is CH1 crossing the trigger level?).")
        time.sleep(0.2)


def fetch(s, ch, chunk=250_000):
    s.w(f":WAVeform:SOURce CHANnel{ch}")
    s.w(":WAVeform:MODE RAW")
    s.w(":WAVeform:FORMat BYTE")
    pre = [float(x) for x in s.q(":WAVeform:PREamble?").split(",")]
    n, xinc, xorg, xref, yinc, yorg, yref = int(pre[2]), pre[4], pre[5], pre[6], pre[7], pre[8], pre[9]
    if n < 100_000:
        print(f"  ! CH{ch}: preamble reports only {n} points; scope is likely not returning full "
              f"RAW memory (check depth / :WAVeform:MODE RAW)")
    raw = np.empty(n, dtype=np.uint8)
    for start in range(1, n + 1, chunk):
        stop = min(start + chunk - 1, n)
        s.w(f":WAVeform:STARt {start}")
        s.w(f":WAVeform:STOP {stop}")
        d = s.i.query_binary_values(":WAVeform:DATA?", datatype="B", container=np.array)
        raw[start - 1:stop] = d[: stop - start + 1]
    v = (raw.astype(np.float32) - yorg - yref) * yinc
    t = (np.arange(n) - xref) * xinc + xorg
    print(f"  CH{ch}: {n} points, dt={xinc*1e6:.2f} us, {v.min():.2f}..{v.max():.2f} V")
    return t, v


def capture(a):
    s = Scope(a.resource)
    configure(s, a)
    arm_and_wait(s, a.wait)
    print("Downloading...")
    data, t = {}, None
    for n in CHANNELS:
        t, data[f"ch{n}"] = fetch(s, n)
    np.savez_compressed(a.out, t=t, meta=json.dumps({str(n): c["label"] for n, c in CHANNELS.items()}),
                        **data)
    print("Saved", a.out)
    return np.load(a.out)

# ----------------------------------------------------------------- analysis
def schmitt(v, lo, hi):
    above, below = v > hi, v < lo
    idx = np.flatnonzero(above | below)
    if idx.size == 0:
        return np.zeros(v.size, bool)
    st = above[idx]
    pos = np.searchsorted(idx, np.arange(v.size), side="right") - 1
    return np.where(pos >= 0, st[np.clip(pos, 0, None)], st[0])


def crank_report(name, t, v, a):
    print(f"\n=== {name} ===")
    s = schmitt(v, a.lo, a.hi)
    rise = np.flatnonzero(~s[:-1] & s[1:]) + 1
    res = dict(name=name, tr=t[rise], events=[])
    if len(rise) < 12:
        print(f"  only {len(rise)} rising edges: no usable crank signal in this capture")
        return res
    tr = t[rise]
    per = np.diff(tr)
    ratio = np.empty_like(per)
    for i in range(len(per)):
        nb = np.delete(per[max(0, i - 4):i + 5], i - max(0, i - 4))
        ratio[i] = per[i] / np.median(nb)
    gaps = np.flatnonzero(ratio > 2.5)
    drops = np.flatnonzero((ratio > 1.6) & (ratio <= 2.5))
    extra = np.flatnonzero(ratio < 0.6)

    # runt / weak pulses: cross the midpoint but never reach the full levels
    mid = (a.hi + a.lo) / 2
    m = v > mid
    starts = np.concatenate(([0], np.flatnonzero(m[1:] != m[:-1]) + 1))
    vmax, vmin, state = np.maximum.reduceat(v, starts), np.minimum.reduceat(v, starts), m[starts]
    inner = lambda ix: ix[(ix > 0) & (ix < len(starts) - 1)]
    runt_hi = inner(np.flatnonzero(state & (vmax < a.runt_hi)))
    runt_lo = inner(np.flatnonzero(~state & (vmin > a.runt_lo)))

    print(f"  rising edges: {len(rise)}   active window: {tr[0]:.3f}s .. {tr[-1]:.3f}s")
    print(f"  sync gaps: {len(gaps)}   missing-edge suspects (~2x period): {len(drops)}   "
          f"extra/short edges: {len(extra)}")
    print(f"  runt highs (peak < {a.runt_hi} V): {len(runt_hi)}   weak lows (floor > {a.runt_lo} V): {len(runt_lo)}")
    print(f"  signal high level: median peak {np.median(vmax[state]):.2f} V, "
          f"lowest peak {vmax[state].min():.2f} V")

    if len(gaps) >= 2:
        counts = np.diff(gaps)
        revt = tr[gaps[1:] + 1] - tr[gaps[:-1] + 1]
        rpm = 60.0 / revt
        bad = np.flatnonzero(counts != a.teeth)
        print(f"  revolutions between gaps: {len(counts)}; edges/rev expected {a.teeth}: "
              f"{len(counts) - len(bad)} good, {len(bad)} wrong")
        print(f"  RPM during capture: median {np.median(rpm):.0f}, range {rpm.min():.0f}-{rpm.max():.0f}")
        for j in bad[:10]:
            print(f"    rev at t={tr[gaps[j]+1]:.3f}s had {counts[j]} edges")
    else:
        print("  fewer than 2 sync gaps found: cannot count edges per revolution")

    ev = [(tr[i + 1], "missing edge?") for i in drops] + [(tr[i + 1], "extra edge") for i in extra]
    ev += [(t[starts[i]], "runt high") for i in runt_hi] + [(t[starts[i]], "weak low") for i in runt_lo]
    res["events"] = sorted(ev)
    return res


def correlate(t, chans, events, labels):
    if not events:
        return
    dt = float(np.median(np.diff(t[:1000])))
    w = max(1, int(0.0005 / dt))
    print("\n=== Supply/reference behaviour at the first events (+/-0.5 ms window) ===")
    for te, kind in events[:12]:
        i = int(np.searchsorted(t, te))
        parts = []
        for n in (3, 4):
            if n in chans:
                seg = chans[n][max(0, i - w):i + w + 1]
                base = np.median(chans[n])
                dev = seg[np.argmax(np.abs(seg - base))] - base
                parts.append(f"{labels[str(n)]}: {dev*1000:+.0f} mV vs median")
        print(f"  t={te:8.4f}s  {kind:14s} " + "   ".join(parts))


def analyze(d, a):
    t = d["t"]
    labels = json.loads(str(d["meta"]))
    chans = {n: d[f"ch{n}"] for n in (1, 2, 3, 4) if f"ch{n}" in d.files}
    reports = {n: crank_report(labels[str(n)], t, chans[n], a) for n in (1, 2) if n in chans}

    for n in (3, 4):
        if n in chans:
            v = chans[n]
            tr1 = reports.get(1, {}).get("tr", [])
            if len(tr1):
                v = v[(t >= tr1[0]) & (t <= tr1[-1])]
            print(f"\n{labels[str(n)]} during cranking: min {v.min():.3f}  median {np.median(v):.3f}  "
                  f"max {v.max():.3f}  p-p {v.max()-v.min():.3f} V")

    if 1 in reports and 2 in reports and len(reports[1]["tr"]) and len(reports[2]["tr"]):
        a1, a2 = reports[1]["tr"], reports[2]["tr"]
        j = np.clip(np.searchsorted(a1, a2), 1, len(a1) - 1)
        near = np.where(np.abs(a1[j] - a2) < np.abs(a1[j - 1] - a2), a1[j], a1[j - 1])
        dly = near - a2
        print(f"\nPCM vs sensor-connector edges: {len(a1)} vs {len(a2)}; "
              f"delay median {np.median(dly)*1e6:.1f} us, worst {np.abs(dly).max()*1e6:.1f} us")

    events = sorted(sum((r["events"] for r in reports.values()), []))
    correlate(t, chans, events, labels)
    if not a.no_plot:
        plot(t, chans, events, labels, a.out.replace(".npz", ""))


def plot(t, chans, events, labels, stem):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    def draw(x0, x1, path):
        fig, ax = plt.subplots(len(chans), 1, sharex=True, figsize=(14, 2.3 * len(chans)))
        ax = np.atleast_1d(ax)
        msk = (t >= x0) & (t <= x1)
        for k, (n, v) in enumerate(sorted(chans.items())):
            ax[k].plot(t[msk], v[msk], lw=0.6)
            ax[k].set_ylabel(labels[str(n)], fontsize=8)
            for te, _ in events:
                if x0 <= te <= x1:
                    ax[k].axvline(te, color="r", alpha=0.3, lw=0.8)
        ax[-1].set_xlabel("s from trigger")
        fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)
        print("Saved", path)
    draw(t[0], t[-1], stem + "_full.png")
    if events:
        te = events[0][0]
        draw(te - 0.03, te + 0.03, stem + "_zoom_first_event.png")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["capture", "analyze"])
    p.add_argument("file", nargs="?", help="npz file (analyze mode)")
    p.add_argument("--resource"); p.add_argument("--seconds", type=float, default=6.0)
    p.add_argument("--pretrig", type=float, default=0.05)
    p.add_argument("--depth", default="10M"); p.add_argument("--trig-level", type=float, default=2.5)
    p.add_argument("--wait", type=float, default=120)
    p.add_argument("--out", default="crank_capture.npz")
    p.add_argument("--teeth", type=int, default=58, help="rising edges per crank rev")
    p.add_argument("--hi", type=float, default=3.5); p.add_argument("--lo", type=float, default=1.5)
    p.add_argument("--runt-hi", type=float, default=4.0); p.add_argument("--runt-lo", type=float, default=1.0)
    p.add_argument("--no-plot", action="store_true")
    a = p.parse_args()
    if a.mode == "analyze":
        if not a.file: sys.exit("analyze needs a .npz file")
        a.out = a.file
        analyze(np.load(a.file), a)
    else:
        analyze(capture(a), a)

if __name__ == "__main__":
    main()
