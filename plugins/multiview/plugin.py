"""multiview — a capture built from videos somebody uploaded. Nothing else.

This is the standalone path. `grid_capture` reads `sync_id_ui/data/<stamp>/`,
which is another program's output directory and the exact dependency this
project exists to remove; this plugin reads *video files* — dragged into the
browser, or already sitting on the server — and works out the rest itself:

  · every view's real frame timestamps, from packet headers (~0.2 s each);
  · a capture timeline: constant rate, long enough to cover the longest view;
  · a frame map per view, derived — so a clock offset is a slider, not a re-run
    of a preprocessing script;
  · a grid layout, which is a *hint to the viewport* and not a video file.

There is no mosaic here and there is nothing to stack. The cells are composed
in the browser (see docs/DESIGN-uploads-time-display.md §3), so adding a camera,
re-ordering the grid or correcting a clock costs nothing and loses no pixels.

Views are matched to files by name: `cam3_2026…mp4` is view `cam3`. Anything
unrecognised keeps a key made from its filename, and every key can be renamed
afterwards, because a guess you cannot correct is worse than no guess.

Two providers, one build:

  videos     from files uploaded into a draft capture — your laptop's files.
  disk       from files that are *already on this server*, picked by path.

`disk` exists because on the deployment that matters they always are: the
cameras write to a 4 TB read-only share the GPU box has mounted, and the only
thing standing between it and a capture used to be a download and a re-upload
of the same bytes to the same machine. It copies nothing, moves nothing and
writes nothing into the tree — the stream's media path is the recording, where
it lies. Everything after that point is identical, which is why the two share
one function: a capture built either way is the same capture.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

import numpy as np

from quorum import probe
from quorum.sdk import AssetKind, Plugin

PLUGIN = Plugin(
    id="multiview",
    name="Multi-view capture",
    description="Build a capture from uploaded videos, one per view. No preprocessing step.",
)

PLUGIN.asset_kind(AssetKind(
    id="video",
    title="View video",
    description="One video file per camera. Anything ffprobe can read; a browser-friendly "
                "rendition is made later if the codec needs it.",
    accept=[".mp4", ".mkv", ".mov", ".avi", ".ts", ".webm", ".m4v"],
    multiple=True,
    on_ready="probe_asset",
    icon="🎞"))

KEY_RE = re.compile(r"(cam\d+|camera[-_ ]?\d+|view[-_ ]?\d+|[a-z]+\d+)", re.I)


def view_key(name: str) -> str:
    """`cam3_20260612_021454.mp4` -> `cam3`; anything else -> a tidy stem."""
    m = KEY_RE.search(Path(name).stem)
    if m:
        return re.sub(r"[-_ ]", "", m.group(1)).lower()
    stem = re.sub(r"[^A-Za-z0-9]+", "_", Path(name).stem).strip("_").lower()
    return stem[:32] or "view"


def _sort_key(k: str):
    m = re.search(r"(\d+)$", k)
    return (0, int(m.group(1)), k) if m else (1, 0, k)


# ------------------------------------------------------------------- probing
@PLUGIN.job("probe_asset", title="Read an uploaded video",
            params={"asset_id": {"type": "integer", "required": True}},
            description="Dimensions, codec, duration and every frame's timestamp.")
def probe_asset(ctx):
    """Run for us by the core the moment an upload of our kind finishes. It
    caches the expensive part (the timestamp table) on the asset, so building
    the capture afterwards is instant however many times you rebuild."""
    aid = int(ctx.params["asset_id"])
    row = ctx.db.one("SELECT * FROM assets WHERE id=?", aid)
    if row is None:
        raise ValueError(f"no asset {aid}")
    path = Path(row["path"])
    ctx.progress(0.1, f"probing {row['name']}")
    facts = probe.facts(path)
    ctx.progress(0.4, "reading frame timestamps")
    stamps = probe.timestamps(path)
    blob = ctx.blob_path(f"pts-{aid}.npy")
    np.save(blob, stamps)
    meta = json.loads(row["meta"] or "{}")
    meta.update({k: facts[k] for k in ("codec", "width", "height", "duration", "fps", "playable")})
    meta.update({"n_frames": int(len(stamps)), "pts": str(blob),
                 "view": meta.get("view") or view_key(row["name"])})
    ctx.db.run("UPDATE assets SET meta=? WHERE id=?", json.dumps(meta), aid)
    ctx.host.publish(row["capture_id"], {"t": "assets", "capture_id": row["capture_id"]})
    return {"view": meta["view"], "frames": len(stamps), "codec": facts["codec"],
            "playable": facts["playable"],
            "message": f"{meta['view']}: {facts['width']}×{facts['height']} {facts['codec']}, "
                       f"{len(stamps)} frames, {facts['duration']:.1f}s"}


def _stamps_for(ctx, asset: dict) -> np.ndarray:
    meta = json.loads(asset["meta"] or "{}")
    p = meta.get("pts")
    if p and Path(p).exists():
        return np.load(p).astype("<i8")
    return probe.timestamps(asset["path"])


# ------------------------------------------------------------------ provider
@PLUGIN.provider(
    "videos", title="Build from uploaded videos",
    params={"capture_id": {"type": "capture"},
            "fps": {"type": "number", "default": 0,
                    "label": "Capture frame rate",
                    "description": "0 picks the fastest view's rate, so no view is "
                                   "sampled below its own."},
            "cols": {"type": "integer", "default": 0,
                     "label": "Grid columns (0 = square-ish)"},
            "cell_w": {"type": "integer", "default": 0,
                       "label": "Cell width in scene pixels (0 = the widest view)"},
            "duration": {"type": "number", "default": 0,
                         "label": "Seconds to cover (0 = the longest view)"}},
    description="Reads this capture's uploaded videos. Idempotent: build again after any change.")
def build(ctx):
    """The whole provider. Idempotent by construction: it upserts streams onto
    the capture it was given, so building after an edit updates rather than
    duplicating."""
    cid = ctx.params.get("capture_id")
    if not cid:
        raise ValueError("build this from a capture — create one first, then upload into it")
    cid = int(cid)
    cap = ctx.db.one("SELECT * FROM captures WHERE id=?", cid)
    if cap is None:
        raise ValueError(f"no capture {cid}")

    assets = ctx.assets(cid, "multiview.video")
    if not assets:
        raise ValueError("no view videos uploaded yet — drop some files on the Data panel, "
                         "filed as “View video”")

    ctx.progress(0.05, f"reading {len(assets)} view(s)")
    views = []
    for i, a in enumerate(assets):
        ctx.check()
        meta = json.loads(a["meta"] or "{}")
        if not meta.get("width"):                     # never probed: do it now
            meta.update({k: v for k, v in probe.facts(a["path"]).items()
                         if k in ("codec", "width", "height", "duration", "fps", "playable")})
        stamps = _stamps_for(ctx, a)
        views.append({"name": a["name"], "path": a["path"], "meta": meta, "stamps": stamps,
                      "key": meta.get("view") or view_key(a["name"]),
                      "meta_out": {"asset_id": a["id"], "source_name": a["name"]}})
        ctx.progress(0.05 + 0.3 * (i + 1) / len(assets), f"{views[-1]['key']}: {len(stamps)} frames")

    # Two views that resolve to the same key would silently overwrite one
    # another. Say which files clashed instead — the fix is to rename an asset,
    # which takes a second and cannot be guessed at from here.
    _refuse_duplicate_keys(views, "Rename one on the Data panel.")
    views.sort(key=lambda v: _sort_key(v["key"]))
    return _finish(ctx, cid, cap, views, "videos")


def _finish(ctx, cid: int, cap: dict, views: list[dict], provider_kind: str) -> dict:
    """Views (a key, a metadata dict, a timestamp array and a media path) to a
    built capture. The half that does not care where the files came from — and
    keeping it that way is what makes `disk` twenty lines rather than a second
    copy of the provider that drifts from this one."""
    # -- the capture's own timeline ---------------------------------------
    # Constant rate, because every annotation format in this tree counts
    # frames. Fast enough that no view is sampled below its own rate, long
    # enough to cover the longest one.
    fps = float(ctx.params.get("fps") or 0) or max(
        (float(v["meta"].get("fps") or 0) for v in views), default=15.0) or 15.0
    fps = round(fps, 4)
    span = float(ctx.params.get("duration") or 0) or max(
        (float(v["stamps"][-1]) / 1e6 if len(v["stamps"]) else 0.0) for v in views)
    n_frames = max(1, int(round(span * fps)) + 1)

    # -- the layout: a hint for the viewport, never a file -----------------
    cell_w = int(ctx.params.get("cell_w") or 0) or max(
        int(v["meta"].get("width") or 0) for v in views) or 1920
    aspect = max((int(v["meta"].get("height") or 0) / max(1, int(v["meta"].get("width") or 1))
                  for v in views), default=9 / 16)
    cell_h = max(1, int(round(cell_w * aspect)))
    cols = int(ctx.params.get("cols") or 0) or max(1, int(round(len(views) ** 0.5)))
    layout = _grid(views, cols, cell_w, cell_h)

    cfg = {**(json.loads(cap["config"] or "{}")),
           "fps": fps, "cols": cols, "cell_w": cell_w, "duration": span}
    ctx.write.capture(key=cap["key"], name=cap["name"], n_frames=n_frames, fps=fps,
                      layout=layout, config=cfg, capture_id=cid,
                      provider_kind=provider_kind, state="ready")

    for i, v in enumerate(views):
        ctx.check()
        m = v["meta"]
        ctx.write.stream(
            cid, key=v["key"], idx=i, name=v["key"],
            width=int(m.get("width") or 0), height=int(m.get("height") or 0),
            n_frames=int(len(v["stamps"])), duration=float(m.get("duration") or 0),
            media={"path": v["path"], "kind": "video/mp4",
                   "codec": m.get("codec", ""), "playable": bool(m.get("playable")),
                   "width": int(m.get("width") or 0), "height": int(m.get("height") or 0),
                   "duration": float(m.get("duration") or 0), "present": True,
                   "renditions": _existing_renditions(ctx, cid, v["key"])},
            meta=v.get("meta_out") or {},
            timestamps=v["stamps"])
        ctx.progress(0.4 + 0.5 * (i + 1) / len(views), f"{v['key']} mapped")

    # Offsets are ops; re-applying them rebuilds every frame map against the
    # timeline we just chose, so a rebuild never quietly drops a correction.
    ctx.host.apply_stream_offsets(cid)
    unplayable = [v["key"] for v in views if not v["meta"].get("playable")]
    msg = f"{len(views)} view(s), {n_frames} frames at {fps:g} fps"
    if unplayable:
        msg += f" — {', '.join(unplayable)} need a browser rendition (run media.prepare)"
    ctx.progress(1.0, msg)
    return {"capture_id": cid, "streams": len(views), "fps": fps, "n_frames": n_frames,
            "needs_rendition": unplayable, "message": msg}


def _existing_renditions(ctx, cid: int, key: str) -> dict:
    """A rebuild must not throw away renditions somebody waited ten minutes for."""
    r = ctx.db.one("SELECT media FROM streams WHERE capture_id=? AND key=?", cid, key)
    return (json.loads(r["media"] or "{}").get("renditions") or {}) if r else {}


def _grid(views, cols: int, cell_w: int, cell_h: int) -> dict:
    cells = []
    for i, v in enumerate(views):
        cells.append({"stream": v["key"], "x": (i % cols) * cell_w,
                      "y": (i // cols) * cell_h, "w": cell_w, "h": cell_h})
    rows = (len(views) + cols - 1) // cols
    return {"kind": "grid", "cols": cols, "cells": cells,
            "scene": {"w": cols * cell_w, "h": rows * cell_h}}


# --------------------------------------------------------------- re-layout
@PLUGIN.job("layout", title="Re-lay out the grid",
            params={"capture_id": {"type": "capture", "required": True},
                    "cols": {"type": "integer", "default": 0, "label": "Columns (0 = square-ish)"},
                    "cell_w": {"type": "integer", "default": 0,
                               "label": "Cell width (0 = keep)"}},
            description="Change the shape of the mosaic. Instant: it is a layout, not a video.")
def relayout(ctx):
    """The measure of whether §3 worked. In the tools this replaces, changing
    the grid meant `make_grid.sh` and a re-encode; here it is a row of numbers,
    and the pixels have not moved."""
    cid = int(ctx.params["capture_id"])
    cap = ctx.db.one("SELECT * FROM captures WHERE id=?", cid)
    cfg = json.loads(cap["config"] or "{}")
    old = json.loads(cap["layout"] or "{}")
    streams = [dict(r) for r in ctx.db.all(
        "SELECT * FROM streams WHERE capture_id=? AND enabled=1 ORDER BY idx", cid)]
    if not streams:
        raise ValueError("no enabled streams to lay out")
    cols = int(ctx.params.get("cols") or 0) or int(cfg.get("cols") or 0) or \
        max(1, int(round(len(streams) ** 0.5)))
    prev = {c["stream"]: c for c in (old.get("cells") or [])}
    cell_w = int(ctx.params.get("cell_w") or 0) or int(cfg.get("cell_w") or 0) or \
        max((c.get("w", 0) for c in prev.values()), default=0) or \
        max(s["width"] for s in streams) or 1920
    aspect = max((s["height"] / max(1, s["width"]) for s in streams), default=9 / 16)
    cell_h = max(1, int(round(cell_w * aspect)))
    layout = _grid([{"key": s["key"]} for s in streams], cols, cell_w, cell_h)
    ctx.db.run("UPDATE captures SET layout=?, config=? WHERE id=?",
               json.dumps(layout), json.dumps({**cfg, "cols": cols, "cell_w": cell_w}), cid)
    ctx.host.publish(cid, {"t": "capture", "capture_id": cid})
    return {"cols": cols, "cells": len(layout["cells"]),
            "message": f"{len(layout['cells'])} cells, {cols} columns, {cell_w}×{cell_h} each"}


# ------------------------------------------------- from the server's own disk
STAMP_RE = re.compile(r"(\d{8}_\d{6})")
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".avi", ".ts", ".webm", ".m4v")


def _refuse_duplicate_keys(views: list[dict], fix: str) -> None:
    """Two views resolving to one key would silently overwrite one another.
    Say which files clashed instead — the fix takes a second and cannot be
    guessed at from here."""
    seen: dict[str, str] = {}
    for v in views:
        if v["key"] in seen:
            raise ValueError(f"{v['name']} and {seen[v['key']]} both look like view "
                             f"{v['key']!r}. {fix}")
        seen[v["key"]] = v["name"]


def _disk_stamps(ctx, path: Path) -> np.ndarray:
    """Frame timestamps for a file on the server, cached against its identity.

    Reading the packet headers of a ten-minute camera costs a second or two and
    six of them is the whole wait, so a rebuild — which is how every change to
    the grid or the frame rate takes effect — must not pay it again. The cache
    key is path, size and mtime together: a recording that was replaced under
    the same name is a different file and gets read again.
    """
    st = path.stat()
    tag = hashlib.sha1(f"{path}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]
    blob = ctx.blob_path(f"pts-disk-{tag}.npy")
    if blob.exists():
        try:
            return np.load(blob).astype("<i8")
        except Exception:
            pass                       # a truncated cache is not worth a failed build
    stamps = probe.timestamps(path)
    try:
        np.save(blob, stamps)
    except OSError:
        pass                           # read-only blob dir: slower, not broken
    return stamps


@PLUGIN.provider(
    "disk", title="Import from the server",
    params={"paths": {"type": "paths", "label": "Videos on the server",
                      "required": True, "accept": list(VIDEO_EXT),
                      "description": "Pick one file per camera. They are read where they "
                                     "lie — nothing is uploaded, copied or moved."},
            "name": {"type": "string", "label": "Capture name",
                     "description": "Blank takes the timestamp the filenames share."},
            "replace": {"type": "boolean", "default": False,
                        "label": "Replace a capture with the same key"},
            "fps": {"type": "number", "default": 0, "label": "Capture frame rate",
                    "description": "0 picks the fastest view's rate, so no view is "
                                   "sampled below its own."},
            "cols": {"type": "integer", "default": 0,
                     "label": "Grid columns (0 = square-ish)"},
            "cell_w": {"type": "integer", "default": 0,
                       "label": "Cell width in scene pixels (0 = the widest view)"},
            "duration": {"type": "number", "default": 0,
                         "label": "Seconds to cover (0 = the longest view)"}},
    description="Build a capture from video files already on this server — the recordings "
                "share, a scratch disk, anywhere inside the media roots. No upload.")
def from_disk(ctx):
    """The same build as `videos`, from paths instead of uploads.

    Two rules, and they are the two that make this safe to expose to a browser
    at all. Every path is checked against `media_roots` with the core's own
    `is_media_allowed` — the same function that decides whether the file may be
    served — so this endpoint cannot reach anything the video endpoint could
    not. And nothing is written to any of it: the stream's media path *is* the
    recording, read-only, where the camera left it.
    """
    raw = ctx.params.get("paths") or []
    if not raw and ctx.params.get("capture_id"):
        # Rebuild: the core hands a provider its capture, and this one's files
        # are named in the capture's own config rather than in an upload.
        row = ctx.db.one("SELECT config FROM captures WHERE id=?", int(ctx.params["capture_id"]))
        raw = json.loads((row["config"] if row else "") or "{}").get("paths") or []
    if isinstance(raw, str):
        raw = [x for x in re.split(r"[\n,]", raw) if x.strip()]
    paths = [Path(str(x).strip()) for x in raw if str(x).strip()]
    if not paths:
        raise ValueError("pick at least one video — press Browse and choose the files on "
                         "the server, one per camera")

    for p in paths:
        if not ctx.cfg.is_media_allowed(p):
            raise ValueError(f"{p} is outside the media roots this server will read. "
                             f"Roots: {', '.join(str(r) for r in ctx.cfg.media_roots)}")
        if not p.is_file():
            raise ValueError(f"{p} is not a file on this server")
        if not os.access(p, os.R_OK):
            raise ValueError(f"{p} exists but this server cannot read it — check the bind "
                             f"mount and who the container runs as")

    # The key is what makes a rebuild find the same capture, so it comes from
    # the files rather than from the clock: these recorders name every camera
    # of one session with the same stamp, and re-importing that session must
    # update it rather than make a second one.
    stamps_in_name = {m.group(1) for p in paths if (m := STAMP_RE.search(p.name))}
    stamp = sorted(stamps_in_name)[0] if len(stamps_in_name) == 1 else ""
    name = str(ctx.params.get("name") or "").strip()
    key = stamp or re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_").lower() or \
        f"disk-{int(time.time())}"

    ctx.progress(0.05, f"reading {len(paths)} file(s)")
    views = []
    for i, path in enumerate(paths):
        ctx.check()
        facts = probe.facts(path)
        pts = _disk_stamps(ctx, path)
        views.append({"name": path.name, "path": str(path), "key": view_key(path.name),
                      "meta": {k: facts[k] for k in
                               ("codec", "width", "height", "duration", "fps", "playable")},
                      "stamps": pts,
                      "meta_out": {"source_path": str(path), "source_name": path.name,
                                   "linked": True}})
        ctx.progress(0.05 + 0.3 * (i + 1) / len(paths),
                     f"{views[-1]['key']}: {len(pts)} frames")

    _refuse_duplicate_keys(views, "Import them as separate captures, or rename a stream "
                                  "on the Capture panel afterwards.")
    views.sort(key=lambda v: _sort_key(v["key"]))

    # A rebuild must not rename the capture back to its generated name: the
    # person who typed a better one over it did so on purpose.
    existing = ctx.db.one("SELECT * FROM captures WHERE id=?", int(ctx.params["capture_id"])) \
        if ctx.params.get("capture_id") else None
    # The key comes from the filenames, so importing the session that
    # `grid_capture` already imported would land on *its* capture and quietly
    # rewrite six streams and a mosaic layout out from under 136,582
    # keyframes. Upserting onto our own is the point; upserting onto somebody
    # else's is a data-loss bug wearing the word "idempotent".
    clash = existing or ctx.db.one("SELECT * FROM captures WHERE key=?", key)
    if clash is not None and not existing and not ctx.params.get("replace") and \
            (clash["provider"], clash["provider_kind"]) != ("multiview", "disk"):
        raise ValueError(
            f"capture {key!r} already exists here — {clash['name']!r}, built by "
            f"{clash['provider'] or 'an older import'}. Rebuild that one instead, or tick "
            f"Replace to throw it away and import these files in its place.")
    default = f"Capture {stamp}" if stamp else f"{views[0]['key']} +{len(views) - 1} more"
    cid = ctx.write.capture(
        key=(existing["key"] if existing else key),
        name=name or (existing["name"] if existing else default),
        n_frames=0, fps=0.0, provider_kind="disk", state="draft",
        capture_id=(int(existing["id"]) if existing else None),
        config={"paths": [v["path"] for v in views], "stamp": stamp},
        replace=bool(ctx.params.get("replace")))
    cap = dict(ctx.db.one("SELECT * FROM captures WHERE id=?", cid))
    out = _finish(ctx, cid, cap, views, "disk")

    # The paths are kept in `config` so pressing Rebuild does the same import
    # again — which is what "Rebuild" means everywhere else here, and would
    # otherwise fail on a capture whose files were never in an upload.
    cfg = json.loads(ctx.db.one("SELECT config FROM captures WHERE id=?", cid)["config"] or "{}")
    cfg["paths"] = [v["path"] for v in views]
    cfg["stamp"] = stamp
    ctx.db.run("UPDATE captures SET config=? WHERE id=?", json.dumps(cfg), cid)
    return out
