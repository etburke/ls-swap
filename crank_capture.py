#!/usr/bin/env python3
"""Command-line front end to the crank harness (the Streamlit app is app.py).

  python crank_capture.py capture --sim ref_sag          # simulated scope
  python crank_capture.py capture --seconds 6            # real Rigol scope; arm, then crank
  python crank_capture.py analyze crank_capture.npz      # re-run analysis on a saved capture
"""
import argparse
import sys
import time

from crank.analysis import analyze_capture
from crank.capture import ACTIVE, CaptureJob
from crank.model import CaptureData, CaptureSettings, Thresholds
from crank.report import text_report
from crank.scope import RigolScope, ScopeError, SimScope
from crank.sim import SCENARIOS


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--sim", choices=SCENARIOS, help="use the simulator instead of a real scope")
    c.add_argument("--resource", help="VISA resource string (default: auto-detect)")
    c.add_argument("--seconds", type=float, default=6.0)
    c.add_argument("--depth", default="10M")
    c.add_argument("--out", default="crank_capture.npz")
    c.add_argument("--poll", type=float, default=0.2, help=argparse.SUPPRESS)
    a = sub.add_parser("analyze")
    a.add_argument("file")
    for sp in (c, a):
        sp.add_argument("--teeth", type=int, default=58, help="rising edges per crank revolution")
    args = p.parse_args(argv)
    th = Thresholds(teeth=args.teeth)

    if args.mode == "analyze":
        print(text_report(analyze_capture(CaptureData.load(args.file), th)))
        return 0

    try:
        scope = SimScope(args.sim) if args.sim else RigolScope(args.resource)
    except ScopeError as e:
        print(e, file=sys.stderr)
        return 1
    settings = CaptureSettings(seconds=args.seconds, depth=args.depth)
    job = CaptureJob(scope, settings, th, poll_s=args.poll,
                     save=lambda d: (d.save(args.out), args.out)[1])
    job.start()
    last = None
    while job.running or job.state == "idle":
        if job.state != last:
            last = job.state
            print(f"[{job.state}] {job.message}".rstrip())
        time.sleep(0.05)
    job.join()
    for line in scope.log:
        print("scope:", line)
    if job.state != "done":
        print(f"{job.state}: {job.message}", file=sys.stderr)
        return 1
    print(f"Saved {args.out}\n")
    print(text_report(job.analysis))
    return 0


if __name__ == "__main__":
    sys.exit(main())
