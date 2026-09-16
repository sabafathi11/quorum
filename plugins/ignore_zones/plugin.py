"""Frame-specific pixel ignore zones.

Zones are intentionally separate from detections: they suppress a detection
only while its foreground pixels are completely contained by a zone.  Removing
the zone restores the original mask, identity and evidence unchanged.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
from fastapi import Body, Depends, HTTPException, Request

from quorum.api.deps import current_user
from quorum.sdk import Plugin, Tool

PLUGIN = Plugin(id="ignore_zones", name="Ignore zones",
                description="Mark pixel regions whose fully contained detections are ignored.",
                web="ignore_zones.js")
PLUGIN.tool(Tool(id="ignore-zones", title="Ignore zones", icon="⊘", order=25,
                 needs_layer_types=["mask.rle"],
                 description="Paint frame-specific regions excluded from annotation work."))


def _codec():
    p = Path(__file__).resolve().parent.parent / "mask_layer" / "plugin.py"
    spec = importlib.util.spec_from_file_location("_ignore_zone_codec", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CODEC = _codec()


def fold(ops):
    out = {}
    for op in ops:
        if op["kind"] != "ignore_zones.set":
            continue
        p = op.get("payload") or {}
        s, f = str(p.get("stream", "")), str(p.get("frame", ""))
        if not s or not f:
            continue
        if p.get("payload") is None:
            out.get(s, {}).pop(f, None)
        else:
            out.setdefault(s, {})[f] = p["payload"]
    return out


def state_for(host, capture_id):
    return {"zones": fold(host.ops_since(capture_id, 0))}


def zone_from_state(zones: dict, stream: str, frame: int):
    """CVAT-style keyframe hold: a zone stays active until the next edit in
    that camera. Streams are deliberately independent."""
    frames = (zones or {}).get(stream, {})
    prior = [int(f) for f in frames if int(f) <= int(frame)]
    return frames.get(str(max(prior))) if prior else None


def _stream(host, capture_id, key):
    row = host.db.one("SELECT * FROM streams WHERE capture_id=? AND key=?", capture_id, key)
    if row is None:
        raise HTTPException(400, f"unknown stream: {key!r}")
    return dict(row)


def _valid_payload(payload, st):
    if not isinstance(payload, dict):
        raise HTTPException(400, "zone payload must be an object")
    box = payload.get("box")
    if not isinstance(box, list) or len(box) != 4 or any(isinstance(v, bool) for v in box):
        raise HTTPException(400, "zone needs integer box [left, top, width, height]")
    try:
        l, t, w, h = [int(v) for v in box]
    except (TypeError, ValueError):
        raise HTTPException(400, "zone box must contain integers") from None
    if [l, t, w, h] != box or l < 0 or t < 0 or w <= 0 or h <= 0 or l + w > st["width"] or t + h > st["height"]:
        raise HTTPException(400, "zone box lies outside the stream image")
    try:
        runs = [int(v) for v in str(payload.get("rle", "")).split(",")]
    except ValueError:
        raise HTTPException(400, "zone RLE contains a non-integer run") from None
    if any(v < 0 for v in runs) or sum(runs) != w * h:
        raise HTTPException(400, "zone RLE runs must exactly cover its box")


@PLUGIN.route.get("/{capture_id}/state")
def state(capture_id: int, request: Request, user: dict = Depends(current_user)):
    return state_for(request.app.state.host, capture_id)


@PLUGIN.route.post("/{capture_id}/set")
def set_zone(capture_id: int, request: Request, body: dict = Body(...), user: dict = Depends(current_user)):
    host = request.app.state.host
    stream = str(body.get("stream", ""))
    st = _stream(host, capture_id, stream)
    try:
        frame = int(body.get("frame"))
    except (TypeError, ValueError):
        raise HTTPException(400, "frame must be an integer") from None
    if frame < 0 or frame >= st["n_frames"]:
        raise HTTPException(400, "frame lies outside this stream")
    payload = body.get("payload")
    if payload is not None:
        _valid_payload(payload, st)
    op = host.append_op(capture_id, "ignore_zones.set",
                        {"stream": stream, "frame": frame, "payload": payload}, user["name"])
    return {"op": op, **state_for(host, capture_id)}


def contains(zone: dict | None, mask: dict | None) -> bool:
    """True only when every foreground mask pixel lies in the zone."""
    if not zone or not mask:
        return False
    zl, zt, zw, zh = map(int, zone["box"])
    ml, mt, mw, mh = map(int, mask["box"])
    if ml < zl or mt < zt or ml + mw > zl + zw or mt + mh > zt + zh:
        return False
    src = CODEC.decode_rle(mask.get("rle", ""), mw, mh)
    if not src.any():
        return False
    dst = CODEC.decode_rle(zone.get("rle", ""), zw, zh)
    return bool(np.all(dst[mt - zt:mt - zt + mh, ml - zl:ml - zl + mw][src]))


def zone_at(host, capture_id: int, stream: str, frame: int):
    return zone_from_state(fold(host.ops_since(capture_id, 0)), stream, frame)


def ignored_object(host, capture_id: int, stream: str, object_id: int, frame: int) -> bool:
    """Shared server-side predicate for identity, review and exporters."""
    row = host.db.one("SELECT payload, outside FROM shapes WHERE object_id=? AND frame<=? "
                      "ORDER BY frame DESC LIMIT 1", object_id, int(frame))
    return bool(row and not row["outside"] and contains(zone_at(host, capture_id, stream, frame),
                                                           json.loads(row["payload"])))
