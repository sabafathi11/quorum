"""Read-only diagnostic: how does *this* camera lose data, and what will
main.py do about it?

Recorders lose time in two ways, and they need opposite detectors:

  * REPEATED frames -- the stream re-sends the last picture. Detected on
    pixels. main.py defaults to byte-identical, which is the safe choice: a
    live sensor's read noise stops a merely static scene from looking frozen.
    The one case that defeats it is a burnt-in ticking clock, where a stalled
    frame still differs by the digits -- this script tells you if you are in it.
  * DROPPED frames -- the recorder writes nothing at all. No pixel evidence
    exists; only the presentation timestamps show the hole. This is the more
    dangerous case, because the surviving frames look adjacent and the tracker
    will carry identities straight across the missing seconds.

    python3 freeze_probe.py --video /records/cam1_....mp4 [--max_frames 0]

Reports both, and how many tracking segments main.py would cut the video into.
"""
import argparse
import os
import sys
import types

import numpy as np
import cv2

# Reuse main.py's real detector so this reports what main.py will actually do.
# torch/sam3 are imported by main.py but unused by the detectors.
if "torch" not in sys.modules:
    _t = types.ModuleType("torch")
    _t.Tensor = type("Tensor", (), {})
    _t.nn = types.SimpleNamespace(Module=type("Module", (), {}))
    sys.modules["torch"] = _t
if "sam3" not in sys.modules:
    _s, _b = types.ModuleType("sam3"), types.ModuleType("sam3.model_builder")
    _b.build_sam3_multiplex_video_predictor = lambda **kw: None
    _s.model_builder = _b
    sys.modules["sam3"], sys.modules["sam3.model_builder"] = _s, _b
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from main import _GapWatch, _is_duplicate_frame  # noqa: E402


def runs_of(flags, min_len):
    """Inclusive frame ranges where flags is True and the run is long enough.

    flags[i] describes the pair (i, i+1), so True at i means frame i+1 repeats.
    """
    out, start = [], None
    for i, f in enumerate(list(flags) + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            if i - start >= min_len:
                out.append((start + 1, i))
            start = None
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--max_frames", type=int, default=0,
                   help="Stop after N frames (0 = whole file, the default).")
    p.add_argument("--min_frames", type=int, default=10, help="= --freeze_min_frames")
    p.add_argument("--gap_ms", type=float, default=400.0, help="= --freeze_gap_ms")
    p.add_argument("--gap_mult", type=float, default=5.0, help="= --freeze_gap_mult")
    p.add_argument("--pixel_thresh", type=int, default=8,
                   help="Per-pixel grey change for the 'near duplicate' probe")
    p.add_argument("--ratio", type=float, default=0.001,
                   help="Changed-pixel fraction for the 'near duplicate' probe")
    args = p.parse_args()

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        raise SystemExit(f"cannot open {args.video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0

    gapwatch = _GapWatch(args.gap_ms, args.gap_mult)
    prev, prev_ts, n = None, None, 0
    exact, near, gaps = [], [], []
    changed_px, acc, deltas = [], None, []

    while True:
        ts = cap.get(cv2.CAP_PROP_POS_MSEC)
        ret, frame = cap.read()
        if not ret or (args.max_frames and n >= args.max_frames):
            break
        if prev_ts is not None:
            deltas.append(ts - prev_ts)
        prev_ts = ts
        hole = gapwatch.update(ts)
        if hole and n > 0:
            gaps.append((n - 1, hole))

        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if prev is not None:
            d = cv2.absdiff(g, prev)
            exact.append(cv2.countNonZero(d) == 0)
            _, t = cv2.threshold(d, args.pixel_thresh, 255, cv2.THRESH_BINARY)
            c = cv2.countNonZero(t)
            near.append(c <= args.ratio * d.size)
            if near[-1] and c > 0:
                changed_px.append(c)
                acc = t if acc is None else cv2.bitwise_or(acc, t)
        prev = g
        n += 1
        if n % 5000 == 0:
            print(f"  ...{n} frames", flush=True)
    cap.release()

    h, w = prev.shape if prev is not None else (0, 0)
    med = float(np.median(deltas)) if deltas else 0.0
    print(f"\nvideo   : {os.path.basename(args.video)}")
    print(f"scanned : {n} frames, container says {fps:.2f} fps, {w}x{h}")
    print(f"timing  : median inter-frame {med:.1f}ms "
          f"(= {1000.0 / med if med else 0:.1f} fps while actually recording)")

    e_runs = runs_of(exact, args.min_frames)
    n_runs = runs_of(near, args.min_frames)
    e_frames = set(f for a, b in e_runs for f in range(a, b + 1))
    only_near = [(a, b) for a, b in n_runs if not set(range(a, b + 1)) <= e_frames]

    e_tot = sum(b - a + 1 for a, b in e_runs)
    print(f"\nREPEATED-frame freezes (caught by default): {len(e_runs)} run(s), "
          f"{e_tot} frames ({e_tot / fps if fps else 0:.1f}s)")
    for a, b in e_runs[:10]:
        print(f"    frames {a}..{b} ({b - a + 1} frames)")
    if len(e_runs) > 10:
        print(f"    ... and {len(e_runs) - 10} more")

    lost = sum(ms for _, ms in gaps) / 1000.0
    print(f"\nDROPPED-frame holes (caught by default): {len(gaps)} hole(s), "
          f"{lost:.1f}s of wall-clock missing")
    for i, ms in gaps[:10]:
        print(f"    {ms / 1000.0:.2f}s between frames {i} and {i + 1}")
    if len(gaps) > 10:
        print(f"    ... and {len(gaps) - 10} more")

    segments = len(e_runs) + len(gaps) + 1
    print(f"\n=> main.py would cut this video into {segments} tracking segment(s). "
          f"Track ids never carry across a cut.")
    if n and segments > 1:
        print(f"   Average segment: {n / segments:.0f} frames "
              f"({n / segments / (1000.0 / med) if med else 0:.1f}s of footage).")

    print("\n--- repeated-frame threshold check ---")
    if not only_near:
        print("Nothing extra would be caught by loosening the pixel thresholds."
              "\nThe strict defaults are correct for this camera.")
    else:
        tot = sum(b - a + 1 for a, b in only_near)
        print(f"{len(only_near)} near-duplicate run(s) ({tot} frames) are NOT "
              f"byte-identical.")
        if acc is not None and cv2.countNonZero(acc):
            ys, xs = np.where(acc > 0)
            x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
            area = ((x1 - x0 + 1) * (y1 - y0 + 1)) / float(w * h)
            print(f"Their change sits in box x={x0}..{x1} y={y0}..{y1} "
                  f"({area * 100:.2f}% of frame), median "
                  f"{int(np.median(changed_px)) if changed_px else 0} px/frame.")
            if area < 0.05:
                print("A small fixed region -- that is a burnt-in clock, so these ARE"
                      f"\nreal freezes being missed. Add:"
                      f"\n    --freeze_pixel_thresh {args.pixel_thresh} "
                      f"--freeze_ratio {args.ratio}")
            else:
                print("Change is spread across the frame, so this is low-motion"
                      "\nfootage, NOT a stall. Keep the strict defaults -- loosening"
                      "\nthem here would throw away real data.")


if __name__ == "__main__":
    main()
