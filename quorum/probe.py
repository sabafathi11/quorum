"""What the core needs to know about a media file, and nothing more.

Three questions, all of them about *time and pixels*, none of them about
cameras:

    facts(path)       how big, how long, what codec, can a browser play it
    timestamps(path)  the presentation time of every frame, in order
    still(path, t)    one frame, as a JPEG

This is core rather than a plugin because the core already serves these files
byte by byte and already owns `frame_map`. A frame map you cannot recompute is
a frame map you cannot correct, and recomputing one needs the timestamps — so
a core feature (a clock offset) would otherwise be waiting on a plugin to be
installed before it worked, which is a lie about where the boundary is.

`ffprobe`/`ffmpeg` are a soft dependency: every function here raises
`NoFFmpeg` with a readable message rather than returning a plausible guess.
Reading the packet timestamps of a 140 MB HEVC camera video takes ~0.2 s and
never decodes a frame.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np

# Codecs a browser will decode without help. HEVC is deliberately *not* here:
# Chrome plays it only where the platform has a hardware decoder, and these
# captures are all HEVC, so guessing optimistically would show black rectangles
# and blame the network. See `media.prepare` for what to do about it.
WEB_CODECS = {"h264", "avc1", "vp8", "vp9", "av1", "theora"}
WEB_CONTAINERS = {".mp4", ".m4v", ".webm", ".ogv", ".mov"}


class NoFFmpeg(RuntimeError):
    pass


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffprobe") and shutil.which("ffmpeg"))


def _need(tool: str) -> str:
    p = shutil.which(tool)
    if not p:
        raise NoFFmpeg(
            f"{tool} is not on PATH — install ffmpeg to probe media, correct a clock "
            f"offset, or read a full-resolution still")
    return p


def _run(args: list[str], timeout: int = 600) -> str:
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip().splitlines()[-1:][0]
                           if r.stderr else f"{args[0]} failed ({r.returncode})")
    return r.stdout


# ------------------------------------------------------------------- facts
def facts(path: str | Path) -> dict:
    """Everything a stream row wants about its file, in one ffprobe call."""
    path = Path(path)
    out = _run([_need("ffprobe"), "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,codec_name,nb_frames,duration,"
                                 "r_frame_rate,avg_frame_rate",
                "-show_entries", "format=duration,format_name",
                "-of", "json", str(path)])
    d = json.loads(out or "{}")
    st = (d.get("streams") or [{}])[0]
    fmt = d.get("format") or {}
    dur = _f(st.get("duration")) or _f(fmt.get("duration")) or 0.0
    codec = (st.get("codec_name") or "").lower()
    return {
        "path": str(path),
        "kind": "video/mp4" if path.suffix.lower() in (".mp4", ".m4v", ".mov") else "video",
        "codec": codec,
        "width": int(st.get("width") or 0),
        "height": int(st.get("height") or 0),
        "n_frames": int(_f(st.get("nb_frames")) or 0),
        "duration": dur,
        "fps": _rate(st.get("avg_frame_rate")) or _rate(st.get("r_frame_rate")) or 0.0,
        "playable": codec in WEB_CODECS and path.suffix.lower() in WEB_CONTAINERS,
        "present": path.exists(),
        "size": path.stat().st_size if path.exists() else 0,
    }


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rate(v) -> float:
    if not v or "/" not in str(v):
        return _f(v) or 0.0
    a, _, b = str(v).partition("/")
    fa, fb = _f(a) or 0.0, _f(b) or 0.0
    return fa / fb if fb else 0.0


# -------------------------------------------------------------- timestamps
def timestamps(path: str | Path) -> np.ndarray:
    """Presentation time of every video frame, ascending, as int64 microseconds.

    From *packets*, so nothing is decoded. Packets come out in decode order and
    carry the presentation timestamp, so they are sorted — which is what
    `sync_id_ui/prepare.py` does with PyAV, and this tree's venv has no PyAV.
    """
    out = _run([_need("ffprobe"), "-v", "error", "-select_streams", "v:0",
                "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path)])
    vals = []
    for line in out.splitlines():
        line = line.strip().rstrip(",")
        if not line or line == "N/A":
            continue
        try:
            vals.append(int(round(float(line) * 1e6)))
        except ValueError:
            continue
    if not vals:
        raise RuntimeError(f"{Path(path).name} has no readable frame timestamps")
    a = np.asarray(sorted(vals), dtype="<i8")
    return a


def pack(a: np.ndarray) -> bytes:
    return np.ascontiguousarray(a, dtype="<i8").tobytes()


def unpack(blob: bytes | None) -> np.ndarray | None:
    if not blob:
        return None
    return np.frombuffer(blob, dtype="<i8")


# ------------------------------------------------------------------ stills
def still(path: str | Path, t: float, width: int = 0, out: Path | None = None,
          quality: int = 4) -> Path:
    """One frame at time `t` (seconds), as a JPEG, at `width` pixels across.

    `-ss` before `-i` seeks by keyframe and then decodes forward to the exact
    time, which is what makes this ~150 ms on a 600 s video instead of minutes.
    """
    out = Path(out or Path(str(path) + f".{int(t * 1000)}.jpg"))
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = f"scale={int(width)}:-2" if width else "null"
    _run([_need("ffmpeg"), "-v", "error", "-y",
          "-ss", f"{max(0.0, float(t)):.4f}", "-i", str(path),
          "-frames:v", "1", "-vf", vf, "-q:v", str(quality), str(out)], timeout=120)
    if not out.exists():
        raise RuntimeError(f"no frame at {t:.3f}s in {Path(path).name}")
    return out


def cache_name(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


# ---------------------------------------------------------------- the rule
def frame_map(stamps: np.ndarray, n_frames: int, fps: float, offset: float = 0.0) -> np.ndarray:
    """Capture frame -> this stream's own frame number.

    One rule, in one place, and the same one `sync_id_ui/prepare.py` used: the
    capture's frame `f` happens at `f / fps`; this stream shows whichever of its
    own frames was most recently presented at that moment, after its clock error
    is taken out. A camera running `offset` seconds *ahead* is read at
    `t - offset`, which delays its content into step with everyone else.
    """
    if stamps is None or not len(stamps) or n_frames <= 0 or fps <= 0:
        return np.zeros(max(0, int(n_frames)), dtype="<i4")
    # Rounded to whole microseconds before the search, because the timestamps
    # are. Without it, `3 / 5.0 - 0.2` is 0.39999999999999997 and lands on the
    # frame *before* the one presented at 0.4 s — a mask one frame out, which
    # looks perfectly fine and is wrong.
    t = np.rint((np.arange(int(n_frames), dtype=np.float64) / float(fps)
                 - float(offset)) * 1e6).astype("int64")
    idx = np.searchsorted(stamps, t, side="right") - 1
    np.clip(idx, 0, len(stamps) - 1, out=idx)
    return np.ascontiguousarray(idx, dtype="<i4")
