"""Getting *exactly* the frames a mask was drawn on, as JPEGs.

This is the load-bearing part of the whole plugin, and the reason it is its own
file. A mask is `(object, frame, payload)`; the frame number means "this
camera's own frame N", and every one of these recordings is variable-rate. Ask
ffmpeg for frame N by index, or by `N / fps`, and on `cam1_20260612_021454.mp4`
you land on a different picture — SAM then segments whoever is standing there
instead, and the result is a mask that looks perfectly plausible and is wrong.
Nothing on screen admits it.

So frames are addressed by **presentation timestamp**, which the core already
stores per stream (`streams.timestamps`, int64 µs, one per frame) and which is
the same table `sync_id_ui/prepare.py` built the sync map from. Stream frame N
is `timestamps[N]`, and that is the only conversion in here.

Two extraction paths, and both are checked rather than trusted:

    one(path, t)        a single frame — `-ss` before `-i`, ~150 ms on a
                        600 s video, the same trick `quorum.probe.still` uses
    many(path, ts)      a run of frames — one decode pass with a `select`
                        filter, because seeking separately for each of 60
                        frames costs a minute and this costs a second

`many` refuses rather than guesses when ffmpeg hands back the wrong number of
frames: an off-by-one there would silently shift every propagated mask by one
frame, which is exactly the failure this file exists to prevent.
"""
from __future__ import annotations

import glob
import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from quorum import probe

# How close a decoded frame's timestamp must be to the one asked for, in
# seconds. Frame intervals here are 33–66 ms; a µs-rounded timestamp is out by
# at most 1 µs. 2 ms is far inside one and far outside the other.
TOL = 0.002


class NoFrame(RuntimeError):
    pass


# ------------------------------------------------------------------- lookup
def times_of(stamps: np.ndarray | None, frames) -> list[float]:
    """Stream frame numbers -> seconds. Out-of-range is an error, not a clamp:
    a clamped frame is a wrong picture that looks like a right one."""
    if stamps is None or not len(stamps):
        raise NoFrame(
            "this stream has no frame timestamps, so a frame number cannot be turned "
            "into a picture — re-probe it on the capture's Data panel")
    out = []
    for f in frames:
        f = int(f)
        if f < 0 or f >= len(stamps):
            raise NoFrame(f"frame {f} is outside this stream's {len(stamps)} frames")
        out.append(int(stamps[f]) / 1e6)
    return out


def stamps_for(db, stream_id: int) -> np.ndarray | None:
    r = db.one("SELECT timestamps FROM streams WHERE id=?", int(stream_id))
    return probe.unpack(r["timestamps"]) if r else None


# --------------------------------------------------------------- extraction
def one(path: str | Path, t: float, roi=None, quality: int = 2) -> bytes:
    """The frame presented at `t` seconds, as JPEG bytes, cropped to `roi`.

    `-ss` before `-i` seeks by keyframe and decodes forward, so this is fast;
    it also lands on the first frame at or after `t`, which is the frame we
    asked for as long as `t` is that frame's own timestamp. It is.
    """
    vf = _crop(roi) or "null"
    with tempfile.TemporaryDirectory() as work:
        out = Path(work) / "f.jpg"
        _ffmpeg(["-nostdin", "-v", "error", "-y",
                 "-ss", f"{max(0.0, float(t)):.6f}", "-i", str(path),
                 "-frames:v", "1", "-vf", vf, "-q:v", str(quality), str(out)], timeout=180)
        if not out.exists():
            raise NoFrame(f"no frame at {t:.3f}s in {Path(path).name}")
        return out.read_bytes()


def many(path: str | Path, times, roi=None, quality: int = 2,
         check=None) -> list[bytes]:
    """The frames presented at each of `times`, in order, as JPEG bytes.

    One decode pass. `-copyts` keeps the source timestamps after the input
    seek, so the `select` expression still refers to the same numbers we were
    given; `-to` matters as much as `-ss`, because without it ffmpeg decodes on
    to EOF after the last frame we wanted, which on a ten-minute video costs
    more than everything else here put together.
    """
    times = [float(t) for t in times]
    if not times:
        return []
    if len(times) == 1:
        return [one(path, times[0], roi, quality)]
    lo, hi = min(times), max(times)
    sel = "+".join(f"lt(abs(t-{t:.6f})\\,{TOL})" for t in times)
    vf = sel and f"select='{sel}'"
    crop = _crop(roi)
    if crop:
        vf += f",{crop}"
    with tempfile.TemporaryDirectory() as work:
        _ffmpeg(["-nostdin", "-v", "error", "-y", "-copyts",
                 "-ss", f"{max(0.0, lo - 2.0):.6f}", "-to", f"{hi + 0.5:.6f}", "-i", str(path),
                 "-vf", vf, "-fps_mode", "passthrough", "-q:v", str(quality),
                 "-f", "image2", os.path.join(work, "f%05d.jpg")], timeout=1800, check=check)
        files = sorted(glob.glob(os.path.join(work, "f*.jpg")))
        if len(files) != len(times):
            raise NoFrame(
                f"asked {Path(path).name} for {len(times)} frames and ffmpeg produced "
                f"{len(files)} — refusing rather than guessing which is which, because "
                f"a mask on the wrong frame looks right")
        return [Path(f).read_bytes() for f in files]


def _crop(roi) -> str:
    if not roi:
        return ""
    x, y, w, h = (int(v) for v in roi)
    return f"crop={w}:{h}:{x}:{y}"


def _ffmpeg(args: list[str], timeout: int, check=None) -> None:
    exe = probe._need("ffmpeg")                       # raises NoFFmpeg, readably
    p = subprocess.Popen([exe, *args], stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE, text=True)
    try:
        while p.poll() is None:
            if check:
                try:
                    check()                            # cooperative cancellation
                except Exception:
                    p.terminate()
                    raise
            try:
                p.wait(timeout=0.4)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if p.poll() is None:
            p.terminate()
    if p.returncode != 0:
        err = (p.stderr.read() if p.stderr else "").strip().splitlines()
        raise NoFrame(f"ffmpeg failed: {err[-1] if err else p.returncode}")


# --------------------------------------------------------------------- roi
def roi_for(points, box, width: int, height: int, pad: float = 1.6,
            min_side: int = 224, max_fraction: float = 0.45):
    """A crop that makes a small object big enough for SAM to see.

    SAM rescales whatever it is given to a fixed square, so a person 130 px
    wide in a 1920×1080 frame arrives about 70 px tall, while the same person
    inside a 400 px crop arrives nearly full size. That is the entire reason
    this exists, and it is why the ROI is computed from the *prompt*, not from
    the frame.

    Returns `[x, y, w, h]`, or None when the prompt already covers enough of
    the frame that cropping would only lose context. Even sides, because
    yuv420p wants them.
    """
    xs, ys = [], []
    for p in points or ():
        xs.append(float(p[0])); ys.append(float(p[1]))
    if box:
        xs += [float(box[0]), float(box[2])]
        ys += [float(box[1]), float(box[3])]
    if not xs:
        return None
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w = max((x1 - x0) * (1 + pad), min_side)
    h = max((y1 - y0) * (1 + pad), min_side)
    side = max(w, h)                                   # square: SAM squashes anyway
    if side * side >= max_fraction * width * height:
        return None
    x = int(round(cx - side / 2)); y = int(round(cy - side / 2))
    w = h = int(round(side))
    x = max(0, min(x, width - 1)); y = max(0, min(y, height - 1))
    w = min(w, width - x); h = min(h, height - y)
    w -= w % 2; h -= h % 2
    return [x, y, w, h] if w >= 32 and h >= 32 else None
