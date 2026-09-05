"""mask_layer — the RLE mask layer type.

Declares one layer type (``mask.rle``) and the codec for it. The payload the
core stores opaquely is::

    {"box": [left, top, width, height], "rle": "runs,alternating,from,background"}

which is CVAT's own mask encoding, kept verbatim so importing and exporting are
lossless and a track id means the same thing here as in the tools this replaces.
"""
from __future__ import annotations

import numpy as np

from quorum.sdk import LayerType, Plugin

PLUGIN = Plugin(
    id="mask_layer",
    name="Masks",
    description="Run-length encoded segmentation masks, drawn per stream.",
    web="mask.js",
)

PLUGIN.layer_type(LayerType(
    id="mask.rle", name="Mask (RLE)", renderer="MaskRenderer", editable=False,
    payload_hint='{"box":[l,t,w,h], "rle":"a,b,c…"}'))


# ------------------------------------------------------------------- codec
def decode_rle(rle: str, w: int, h: int) -> np.ndarray:
    """CVAT RLE (runs alternating, starting with background) -> bool (h, w)."""
    flat = np.zeros(w * h, dtype=bool)
    if not rle:
        return flat.reshape(h, w)
    runs = np.fromstring(rle, dtype=np.int64, sep=",") if hasattr(np, "fromstring") \
        else np.array([int(x) for x in rle.split(",")], dtype=np.int64)
    idx, val = 0, False
    for run in runs:
        if val:
            flat[idx:idx + run] = True
        idx += int(run)
        val = not val
    return flat.reshape(h, w)


def encode_rle(mask: np.ndarray) -> str:
    flat = np.asarray(mask, dtype=bool).ravel()
    if flat.size == 0:
        return ""
    changes = np.flatnonzero(np.diff(flat)) + 1
    bounds = np.concatenate(([0], changes, [flat.size]))
    runs = np.diff(bounds)
    if flat[0]:                      # the encoding always starts with background
        runs = np.concatenate(([0], runs))
    return ",".join(str(int(r)) for r in runs)


def area(payload: dict) -> int:
    box = payload.get("box") or [0, 0, 0, 0]
    m = decode_rle(payload.get("rle", ""), int(box[2]), int(box[3]))
    return int(m.sum())


# -------------------------------------------------------------------- jobs
@PLUGIN.job("stats", title="Mask statistics",
            params={"capture_id": {"type": "capture", "required": True},
                    "layer_id": {"type": "integer", "label": "Layer id", "required": True}},
            description="Per-stream track and keyframe counts, and mean mask area.")
def stats(ctx):
    lid = int(ctx.params["layer_id"])
    rows = ctx.db.all(
        "SELECT s.key AS stream, COUNT(DISTINCT o.id) AS tracks, COUNT(*) AS keyframes "
        "FROM shapes sh JOIN objects o ON o.id=sh.object_id "
        "JOIN streams s ON s.id=o.stream_id WHERE o.layer_id=? GROUP BY s.key ORDER BY s.key", lid)
    out = []
    for i, r in enumerate(rows):
        ctx.progress((i + 1) / max(len(rows), 1), f"{r['stream']}")
        sample = ctx.db.all(
            "SELECT sh.payload FROM shapes sh JOIN objects o ON o.id=sh.object_id "
            "JOIN streams s ON s.id=o.stream_id "
            "WHERE o.layer_id=? AND s.key=? ORDER BY RANDOM() LIMIT 60", lid, r["stream"])
        import json
        areas = [area(json.loads(x["payload"])) for x in sample]
        out.append({"stream": r["stream"], "tracks": r["tracks"], "keyframes": r["keyframes"],
                    "mean_px": int(np.mean(areas)) if areas else 0})
    return {"streams": out, "message": f"{sum(o['tracks'] for o in out)} tracks"}
