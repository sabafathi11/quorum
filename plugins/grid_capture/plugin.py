"""grid_capture — a capture provider for the 6-camera mosaic captures.

This is the plugin that knows what a "stamp" is, where `raw_vids/` lives, and
that a mosaic video is laid out three cells to a row. None of that belongs in
the core, which only knows that a capture has streams, a frame count, and an
optional layout hint the viewport can read.

Two providers:

  scan       list the captures on disk without importing anything
  prepared   import one, reusing the exact sync table `sync_id_ui/prepare.py`
             already built (`data/<stamp>/{meta.json,sync.npy}`) — the map from
             mosaic frame to each camera's own frame, built from real
             presentation timestamps rather than frame indices.

Importing the *annotations* is somebody else's job: see `cvat_xml`, or the
`tracks` importer here for the pickle `prepare.py` leaves behind.
"""
from __future__ import annotations

import json
import os
import pickle
from pathlib import Path

import numpy as np

from quorum.sdk import Plugin

PLUGIN = Plugin(
    id="grid_capture",
    name="Mosaic captures",
    description="Multi-camera captures laid out as one mosaic video, with a pts-exact frame map.",
)

# Where this plugin looks, unless quorum.toml says otherwise.
DEFAULT_ROOT = Path(__file__).resolve().parents[3]      # the annotation tree


def _root(ctx) -> Path:
    return Path(ctx.settings("root", DEFAULT_ROOT)).resolve()


def _prep_dir(ctx) -> Path:
    return Path(ctx.settings("prepared_dir", _root(ctx) / "sync_id_ui" / "data"))


def _stamps(ctx) -> list[str]:
    d = _prep_dir(ctx)
    return sorted((p.name for p in d.iterdir() if (p / "meta.json").exists()), reverse=True) \
        if d.is_dir() else []


@PLUGIN.provider("scan", title="Scan for captures",
                 description="List prepared captures without importing them.")
def scan(ctx):
    out = []
    for stamp in _stamps(ctx):
        meta = json.loads((_prep_dir(ctx) / stamp / "meta.json").read_text())
        known = ctx.db.scalar("SELECT id FROM captures WHERE key=?", stamp)
        out.append({"key": stamp, "cams": len(meta["cams"]),
                    "frames": meta["grid"]["n_frames"], "fps": meta["grid"]["fps"],
                    "imported": bool(known)})
    return {"captures": out, "message": f"{len(out)} capture(s) on disk"}


@PLUGIN.provider(
    "prepared", title="Import a prepared capture",
    params={"stamp": {"type": "string", "label": "Capture stamp", "required": True},
            "replace": {"type": "boolean", "label": "Replace if it exists", "default": False}},
    description="Reads sync_id_ui/data/<stamp>/ and the videos in raw_vids/<stamp>/.")
def prepared(ctx):
    stamp = str(ctx.params.get("stamp") or "").strip()
    if not stamp:
        raise ValueError("a stamp is required, e.g. 20260612_021454")
    pdir = _prep_dir(ctx) / stamp
    if not (pdir / "meta.json").exists():
        raise FileNotFoundError(
            f"{pdir}/meta.json is missing — run sync_id_ui/prepare.py {stamp} first")

    ctx.progress(0.05, f"reading {stamp}")
    meta = json.loads((pdir / "meta.json").read_text())
    sync = np.load(pdir / "sync.npy")                     # [cam, grid_frame] -> source frame
    grid = meta["grid"]
    vid_dir = _root(ctx) / "raw_vids" / stamp
    mosaic = (pdir / grid["video"]).resolve()
    if not mosaic.exists():
        alt = vid_dir / f"grid_{stamp}.mp4"
        mosaic = alt if alt.exists() else mosaic

    cells = []
    for c in meta["cams"]:
        cells.append({"stream": f"cam{c['n']}", "x": c["off_x"], "y": c["off_y"],
                      "w": grid["cell_w"], "h": grid["cell_h"]})

    layout = {
        "kind": "mosaic",
        "mosaic": {"path": str(mosaic), "w": grid["w"], "h": grid["h"]},
        "cells": cells,
        "cols": grid["cols"],
    }
    cap_id = ctx.write.capture(
        key=stamp, name=f"Capture {stamp}", n_frames=int(grid["n_frames"]),
        fps=float(grid["fps"]), layout=layout,
        config={"stamp": stamp, "source": str(pdir)},
        replace=bool(ctx.params.get("replace")))

    for i, c in enumerate(meta["cams"]):
        ctx.check()
        key = f"cam{c['n']}"
        fm = np.ascontiguousarray(sync[c["n"] - 1], dtype="<i4").tobytes()
        path = vid_dir / f"{key}_{stamp}.mp4"
        ctx.write.stream(
            cap_id, key=key, idx=i, name=key,
            width=c["src_w"], height=c["src_h"], n_frames=c["n_src_frames"],
            media={"path": str(path), "kind": "video/mp4", "present": path.exists()},
            meta={"cell": {"x": c["off_x"], "y": c["off_y"],
                           "w": grid["cell_w"], "h": grid["cell_h"]},
                  "offset_sec": c.get("offset_sec", 0.0),
                  "n_tracks_at_import": c.get("n_tracks", 0)},
            frame_map=fm)
        ctx.progress(0.1 + 0.7 * (i + 1) / len(meta["cams"]), f"{key} mapped")

    ctx.progress(1.0, f"{stamp}: {len(meta['cams'])} streams, {grid['n_frames']} frames")
    return {"capture_id": cap_id, "stamp": stamp, "streams": len(meta["cams"]),
            "message": f"imported {stamp}"}


@PLUGIN.importer(
    "tracks", title="Import tracks.pkl",
    params={"capture_id": {"type": "capture", "required": True},
            "layer": {"type": "string", "default": "masks", "label": "Layer key"},
            "label_filter": {"type": "string", "default": "", "label": "Only this label"},
            "max_frame": {"type": "integer", "default": 0,
                          "label": "Stop after this source frame (0 = all)"}})
def import_tracks(ctx):
    """The pickle `sync_id_ui/prepare.py` writes: {cam number: [track dicts]}.

    Kept as its own importer rather than folded into the provider because the
    geometry of a capture and the annotations drawn on it have different
    lifetimes — you re-import the annotations far more often than the cameras.
    """
    cid = int(ctx.params["capture_id"])
    cap = ctx.db.one("SELECT * FROM captures WHERE id=?", cid)
    if cap is None:
        raise ValueError(f"no capture {cid}")
    stamp = json.loads(cap["config"]).get("stamp") or cap["key"]
    pkl = _prep_dir(ctx) / stamp / "tracks.pkl"
    if not pkl.exists():
        raise FileNotFoundError(f"{pkl} is missing")
    only = (ctx.params.get("label_filter") or "").strip()
    max_frame = int(ctx.params.get("max_frame") or 0)

    ctx.progress(0.02, f"reading {pkl.name} ({pkl.stat().st_size / 1e6:.0f} MB)")
    with open(pkl, "rb") as f:
        by_cam = pickle.load(f)

    lay = ctx.write.layer(cid, key=str(ctx.params.get("layer") or "masks"),
                          name="Masks", type="mask.rle", provenance="model",
                          config={"source": str(pkl)}, replace=True)
    total = 0
    cams = sorted(by_cam)
    for i, cam in enumerate(cams):
        ctx.check()
        st = ctx.db.one("SELECT id FROM streams WHERE capture_id=? AND key=?", cid, f"cam{cam}")
        if st is None:
            continue
        items = []
        for t in by_cam[cam]:
            if only and t["label"] != only:
                continue
            frames = []
            for j, fr in enumerate(t["frames"]):
                if max_frame and fr > max_frame:
                    break
                frames.append({"frame": int(fr), "outside": int(t["outside"][j]),
                               "payload": {"box": [int(x) for x in t["boxes"][j]],
                                           "rle": t["rles"][j]}})
            if frames:
                items.append({"key": str(t["id"]), "label": t["label"], "frames": frames})
        ctx.write.objects(lay, int(st["id"]), items)
        total += sum(len(x["frames"]) for x in items)
        ctx.progress(0.05 + 0.9 * (i + 1) / len(cams),
                     f"cam{cam}: {len(items)} tracks, {total} keyframes so far")

    ctx.emit_op(cid, "imported", {"layer": lay, "keyframes": total, "source": str(pkl)})
    return {"layer_id": lay, "keyframes": total,
            "message": f"{total} keyframes into layer {lay}"}
