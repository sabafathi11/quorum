"""The core REST surface. Eight nouns, none of them domain-specific."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse

from .. import probe
from ..db import now, row_to_dict
from ..filters import ObjectFilter
from ..jobs import Blocked
from ..sdk import Conflict
from .deps import current_user, require
from .media import serve_file

router = APIRouter()

CAP_JSON = ("config", "layout")


def _host(request: Request):
    return request.app.state.host


def _capture(host, cid: int) -> dict:
    r = host.db.one("SELECT * FROM captures WHERE id=?", cid)
    if r is None:
        raise HTTPException(404, f"no capture {cid}")
    return row_to_dict(r, CAP_JSON)


def _streams(host, cid: int) -> list[dict]:
    out = []
    for r in host.db.all("SELECT * FROM streams WHERE capture_id=? ORDER BY idx, key", cid):
        d = row_to_dict(r, ("media", "meta"))
        d["has_timestamps"] = bool(r["timestamps"])
        # What a browser can actually decode for this stream, largest last. The
        # viewport's level-of-detail policy is written against this list and
        # against nothing else, so "which video do I play" has one answer here
        # rather than one per zoom gesture.
        m = d.get("media") or {}
        rends = []
        if m.get("playable") and m.get("path"):
            rends.append({"id": "source", "w": m.get("width") or d["width"],
                          "h": m.get("height") or d["height"], "codec": m.get("codec", "")})
        for rid, rr in sorted((m.get("renditions") or {}).items(),
                              key=lambda kv: kv[1].get("w", 0)):
            # A rendition that could not be proved frame-aligned with its
            # source is never offered: it would look right and put every mask
            # one frame out. `media.prepare` says which, and why.
            if rr.get("aligned") is False:
                continue
            rends.append({"id": rid, "w": rr.get("w", 0), "h": rr.get("h", 0),
                          "codec": rr.get("codec", ""), "t_offset": rr.get("t_offset", 0.0)})
        rends.sort(key=lambda r2: r2["w"])
        d["renditions"] = rends
        out.append(d)
    return out


def _layers(host, cid: int) -> list[dict]:
    out = []
    for r in host.db.all("SELECT * FROM layers WHERE capture_id=? ORDER BY id", cid):
        d = row_to_dict(r, ("config",))
        d["n_objects"] = host.db.scalar("SELECT COUNT(*) FROM objects WHERE layer_id=?", d["id"])
        out.append(d)
    return out


# ------------------------------------------------------------------- session
@router.get("/session")
def session(request: Request, user: dict = Depends(current_user)):
    host = _host(request)
    return {
        "user": {k: user[k] for k in ("id", "name", "display", "role")},
        "server": {"version": "0.1.0", "authMode": host.cfg.auth_mode,
                   "startedAt": host.started},
        "plugins": host.manifests(),
        "pluginProblems": host.plugin_problems,
    }


@router.get("/plugins")
def plugins(request: Request, user: dict = Depends(current_user)):
    return {"plugins": _host(request).manifests(), "problems": _host(request).plugin_problems}


# ------------------------------------------------------------------ captures
@router.get("/captures")
def list_captures(request: Request, user: dict = Depends(current_user)):
    host = _host(request)
    out = []
    for r in host.db.all("SELECT * FROM captures ORDER BY key DESC"):
        d = row_to_dict(r, CAP_JSON)
        d["n_streams"] = host.db.scalar("SELECT COUNT(*) FROM streams WHERE capture_id=?", d["id"])
        d["n_layers"] = host.db.scalar("SELECT COUNT(*) FROM layers WHERE capture_id=?", d["id"])
        out.append(d)
    return {"captures": out}


@router.get("/captures/{cid}")
def get_capture(cid: int, request: Request, user: dict = Depends(current_user)):
    host = _host(request)
    cap = _capture(host, cid)
    cap["streams"] = _streams(host, cid)
    cap["layers"] = _layers(host, cid)
    cap["jobs"] = host.jobs.list(cid, limit=10)
    return cap


@router.post("/captures")
def create_capture(request: Request, body: dict = Body(...), user: dict = Depends(current_user)):
    """A *draft*: a capture with a name, a chosen provider, and nothing in it.

    Uploads need somewhere to go before the thing they describe exists, and
    "upload into a shopping basket, then create" loses everything if the tab
    closes. A draft is the basket, and it is a real capture from the first
    byte — you can see it, name it, and come back to it tomorrow.
    """
    host = _host(request)
    key = str(body.get("key") or "").strip()
    name = str(body.get("name") or key or "Untitled capture").strip()
    provider = str(body.get("provider") or "")
    kind = str(body.get("provider_kind") or "")
    if provider and provider not in host.plugins:
        raise HTTPException(400, f"no plugin {provider!r}")
    if not key:
        key = f"draft-{int(now())}"
    if host.db.one("SELECT id FROM captures WHERE key=?", key):
        raise HTTPException(409, f"a capture keyed {key!r} already exists")
    cid = host.db.insert("captures", key=key, name=name, provider=provider,
                         provider_kind=kind, state="draft",
                         config=json.dumps(body.get("config") or {}),
                         layout=json.dumps(body.get("layout") or {}),
                         n_frames=0, fps=float(body.get("fps") or 0), created=now())
    return {"capture": _capture(host, cid)}


@router.patch("/captures/{cid}")
def patch_capture(cid: int, request: Request, body: dict = Body(...),
                  user: dict = Depends(current_user)):
    """Everything about a capture that is a core fact rather than provider
    output: its name, its frame rate, its layout, and the config the provider
    will read next time it builds."""
    host = _host(request)
    cap = _capture(host, cid)
    sets, args = [], []
    for field, col in (("name", "name"), ("key", "key"), ("provider_kind", "provider_kind"),
                       ("state", "state")):
        if field in body:
            sets.append(f"{col}=?")
            args.append(str(body[field]))
    for field in ("fps",):
        if field in body:
            sets.append(f"{field}=?")
            args.append(float(body[field]))
    for field in ("n_frames",):
        if field in body:
            sets.append(f"{field}=?")
            args.append(int(body[field]))
    for field in ("config", "layout"):
        if field in body:
            sets.append(f"{field}=?")
            args.append(json.dumps(body[field] or {}))
    if sets:
        host.db.run(f"UPDATE captures SET {','.join(sets)} WHERE id=?", *args, cid)
    # The frame rate is half of what "which of this camera's frames is this?"
    # means, so changing it invalidates every map derived from it.
    if "fps" in body or "n_frames" in body:
        host.apply_stream_offsets(cid)
    host.publish(cid, {"t": "capture", "capture_id": cid})
    return {"capture": _capture(host, cid)}


@router.post("/captures/{cid}/build")
def build_capture(cid: int, request: Request, body: dict = Body(default={}),
                  user: dict = Depends(current_user)):
    """Run this capture's provider over its config and its assets.

    Building twice is allowed and expected — it is the only "edit" primitive a
    provider has to implement, and `Writer` upserts, so a rebuild after
    changing an offset, adding a camera or re-filing an asset produces the
    same capture rather than a second one.
    """
    host = _host(request)
    cap = _capture(host, cid)
    provider = cap["provider"]
    kind = cap.get("provider_kind") or ""
    if not provider or not kind:
        raise HTTPException(400, "this capture has no provider to build it — it was made by "
                                 "an older import. Create a new one to rebuild.")
    params = {**(cap.get("config") or {}), **(body.get("params") or {}), "capture_id": cid}
    try:
        job = host.jobs.submit(provider, f"provider:{kind}", params, cid, user["name"])
    except Blocked as e:
        raise HTTPException(409, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"job": job}


@router.delete("/captures/{cid}")
def delete_capture(cid: int, request: Request, user: dict = Depends(require("admin"))):
    host = _host(request)
    cap = _capture(host, cid)
    left = host.run_cleanup(cid)
    for r in host.db.all("SELECT path FROM assets WHERE capture_id=?", cid):
        try:
            Path(r["path"]).unlink(missing_ok=True)
        except OSError:
            pass
    host.db.run("DELETE FROM captures WHERE id=?", cid)
    return {"ok": True, "deleted": cap["name"], "cleanupProblems": left}


# ------------------------------------------------------------------ streams
@router.patch("/captures/{cid}/streams/{stream_key}")
def patch_stream(cid: int, stream_key: str, request: Request, body: dict = Body(...),
                 user: dict = Depends(current_user)):
    """Rename, re-order, enable, or correct the clock of one stream.

    The clock offset is not written here — it is appended as an op and folded,
    so it is attributed, appears in History and comes back with Ctrl+Z. What
    this endpoint does with it is apply the fold.
    """
    host = _host(request)
    _capture(host, cid)
    st = host.db.one("SELECT * FROM streams WHERE capture_id=? AND key=?", cid, stream_key)
    if st is None:
        raise HTTPException(404, f"no stream {stream_key}")
    sets, args = [], []
    if "name" in body:
        sets.append("name=?")
        args.append(str(body["name"]))
    if "idx" in body:
        sets.append("idx=?")
        args.append(int(body["idx"]))
    if "enabled" in body:
        sets.append("enabled=?")
        args.append(1 if body["enabled"] else 0)
    if sets:
        host.db.run(f"UPDATE streams SET {','.join(sets)} WHERE id=?", *args, st["id"])
    if "offset" in body:
        if not st["timestamps"]:
            raise HTTPException(
                409, f"{stream_key} has no frame timestamps, so its frame map cannot be "
                     f"recomputed — probe its media first (Capture ▸ Streams ▸ probe). "
                     f"Guessing would put every mask on the wrong picture.")
        host.append_op(cid, "core.stream_offset",
                       {"stream": stream_key, "seconds": float(body["offset"])},
                       user["name"])
        host.apply_stream_offsets(cid)
    host.publish(cid, {"t": "capture", "capture_id": cid})
    return {"stream": _streams(host, cid)}


@router.delete("/captures/{cid}/streams/{stream_key}")
def delete_stream(cid: int, stream_key: str, request: Request,
                  user: dict = Depends(current_user)):
    host = _host(request)
    st = host.db.one("SELECT id FROM streams WHERE capture_id=? AND key=?", cid, stream_key)
    if st is None:
        raise HTTPException(404, f"no stream {stream_key}")
    host.db.run("DELETE FROM streams WHERE id=?", st["id"])
    host.publish(cid, {"t": "capture", "capture_id": cid})
    return {"ok": True}


@router.post("/captures/{cid}/streams/{stream_key}/probe")
def probe_stream(cid: int, stream_key: str, request: Request,
                 user: dict = Depends(current_user)):
    """Read this stream's media for its dimensions and every frame's timestamp.

    ~0.2 s for a 140 MB camera video, because it reads packet headers and never
    decodes. Doing it makes the clock offset available: a frame map you cannot
    recompute is a frame map you cannot correct.
    """
    host = _host(request)
    st = host.db.one("SELECT * FROM streams WHERE capture_id=? AND key=?", cid, stream_key)
    if st is None:
        raise HTTPException(404, f"no stream {stream_key}")
    media = json.loads(st["media"] or "{}")
    path = media.get("path")
    if not path or not Path(path).is_file():
        raise HTTPException(409, f"{stream_key} has no media file to probe")
    try:
        f = probe.facts(path)
        stamps = probe.timestamps(path)
    except probe.NoFFmpeg as e:
        raise HTTPException(501, str(e))
    except Exception as e:
        raise HTTPException(500, f"could not probe {Path(path).name}: {e}")
    media.update({k: f[k] for k in ("codec", "width", "height", "playable", "duration")})
    host.db.run("UPDATE streams SET media=?, width=?, height=?, n_frames=?, duration=?, "
                "timestamps=? WHERE id=?",
                json.dumps(media), f["width"] or st["width"], f["height"] or st["height"],
                len(stamps), f["duration"], probe.pack(stamps), st["id"])
    host.apply_stream_offsets(cid)
    return {"stream": stream_key, "frames": len(stamps), **{k: f[k] for k in
            ("codec", "width", "height", "duration", "playable")}}


# ------------------------------------------------------------------- layers
@router.patch("/layers/{lid}")
def patch_layer(lid: int, request: Request, body: dict = Body(...),
                user: dict = Depends(current_user)):
    """Rename a layer, or change what kind of thing it holds.

    Changing `type` is the other half of "change the data type later": an
    import that guessed `mask.rle` for what are really boxes is re-labelled
    here rather than re-imported. The payloads are untouched — the core never
    opened them — so this is a claim about who should render them, and the
    plugin that owns the new type takes over on the next paint.
    """
    host = _host(request)
    lay = host.db.one("SELECT * FROM layers WHERE id=?", lid)
    if lay is None:
        raise HTTPException(404, f"no layer {lid}")
    sets, args = [], []
    for field in ("name", "key", "provenance"):
        if field in body:
            sets.append(f"{field}=?")
            args.append(str(body[field]))
    if "type" in body:
        t = str(body["type"])
        known = {lt.id for p in host.plugins.values() for lt in p.layer_types}
        if t not in known:
            raise HTTPException(400, f"no plugin declares a layer type named {t!r} — "
                                     f"known: {', '.join(sorted(known)) or 'none'}")
        sets.append("type=?")
        args.append(t)
    if "config" in body:
        sets.append("config=?")
        args.append(json.dumps(body["config"] or {}))
    if sets:
        host.db.run(f"UPDATE layers SET {','.join(sets)} WHERE id=?", *args, lid)
    host.publish(lay["capture_id"], {"t": "capture", "capture_id": lay["capture_id"]})
    return {"layer": row_to_dict(host.db.one("SELECT * FROM layers WHERE id=?", lid), ("config",))}


@router.delete("/layers/{lid}")
def delete_layer(lid: int, request: Request, user: dict = Depends(current_user)):
    host = _host(request)
    lay = host.db.one("SELECT * FROM layers WHERE id=?", lid)
    if lay is None:
        raise HTTPException(404, f"no layer {lid}")
    host.db.run("DELETE FROM layers WHERE id=?", lid)
    host.publish(lay["capture_id"], {"t": "capture", "capture_id": lay["capture_id"]})
    return {"ok": True}


# -------------------------------------------------------- labels, readiness
@router.get("/captures/{cid}/labels")
def capture_labels(cid: int, request: Request, user: dict = Depends(current_user)):
    """Every class in this capture, with how many objects carry it.

    A *class* is `objects.label`, which the core has always stored — so
    colouring by class and hiding a class are core features about core data,
    not a mask-shaped feature that every other layer type would have to
    reimplement.
    """
    host = _host(request)
    rows = host.db.all(
        "SELECT o.label AS label, COUNT(*) AS n, COUNT(DISTINCT o.layer_id) AS layers "
        "FROM objects o JOIN layers l ON l.id=o.layer_id WHERE l.capture_id=? "
        "GROUP BY o.label ORDER BY n DESC", cid)
    return {"labels": [{"label": r["label"], "n": r["n"], "layers": r["layers"]} for r in rows]}


@router.get("/captures/{cid}/readiness")
def capture_readiness(cid: int, request: Request, user: dict = Depends(current_user)):
    return {"plugins": _host(request).readiness(cid)}


@router.get("/captures/{cid}/media/{stream_key}")
def capture_media(cid: int, stream_key: str, request: Request, r: str = "source",
                  user: dict = Depends(current_user)):
    """One stream's video, at one *rendition*.

    A rendition is the same pixels at another size: `source` is the camera's own
    file, and anything else was made by `media.prepare` so that a browser can
    decode it, or so that six of them at once do not melt the machine. They all
    share the source's timeline, so `currentTime` means the same thing in each
    and switching between them mid-playback is safe.

    `_mosaic` is the legacy single-file grid, kept as a capture-level rendition
    for captures that have one. Nothing new produces it: see docs/DESIGN.
    """
    host = _host(request)
    cap = _capture(host, cid)
    if stream_key == "_mosaic":
        path = (cap.get("layout") or {}).get("mosaic", {}).get("path")
        if not path:
            raise HTTPException(404, "this capture has no mosaic video — its cells are "
                                     "composed in the viewport from the streams themselves")
    else:
        row = host.db.one("SELECT media FROM streams WHERE capture_id=? AND key=?", cid, stream_key)
        if row is None:
            raise HTTPException(404, f"no stream {stream_key}")
        media = json.loads(row["media"] or "{}")
        if r and r != "source":
            rend = (media.get("renditions") or {}).get(r)
            if not rend:
                raise HTTPException(404, f"{stream_key} has no {r!r} rendition — "
                                         f"run the media.prepare job to make one")
            path = rend.get("path")
        else:
            path = media.get("path")
        if not path:
            raise HTTPException(404, f"stream {stream_key} has no media file")
    return serve_file(request, Path(path), host.cfg)


@router.get("/captures/{cid}/frame/{stream_key}")
def capture_still(cid: int, stream_key: str, request: Request, frame: int = 0, w: int = 0,
                  user: dict = Depends(current_user)):
    """One full-resolution still of one camera at one capture frame.

    This is what makes zooming show real pixels on a capture whose sources a
    browser cannot decode and which nobody has transcoded yet. It is seeked by
    that stream's *own* timestamps and its clock offset, so the still is the
    same frame the masks are drawn for — a still one frame off would be worse
    than none, because it would look right.
    """
    host = _host(request)
    cap = _capture(host, cid)
    st = host.db.one("SELECT * FROM streams WHERE capture_id=? AND key=?", cid, stream_key)
    if st is None:
        raise HTTPException(404, f"no stream {stream_key}")
    media = json.loads(st["media"] or "{}")
    path = media.get("path")
    if not path or not Path(path).is_file():
        raise HTTPException(404, f"{stream_key} has no media file")
    fps = cap["fps"] or 0
    if not fps:
        raise HTTPException(409, "this capture has no frame rate, so a frame has no time")
    stamps = probe.unpack(st["timestamps"])
    if stamps is not None and len(stamps):
        fm = np.frombuffer(st["frame_map"], dtype="<i4") if st["frame_map"] else None
        i = int(fm[min(max(frame, 0), len(fm) - 1)]) if fm is not None and len(fm) else 0
        t = float(stamps[min(max(i, 0), len(stamps) - 1)]) / 1e6
    else:
        t = max(0.0, frame / fps - float(st["time_offset"] or 0.0))
    width = max(0, min(int(w or 0), 7680))
    out = host.cfg.cache_dir / f"{probe.cache_name(cid, stream_key, frame, width)}.jpg"
    if not out.exists():
        try:
            probe.still(path, t, width, out)
        except probe.NoFFmpeg as e:
            raise HTTPException(501, str(e))
        except Exception as e:
            raise HTTPException(500, f"could not read that frame: {e}")
    return FileResponse(out, media_type="image/jpeg",
                        headers={"cache-control": "private, max-age=86400"})


@router.get("/captures/{cid}/streams/{stream_key}/timestamps")
def stream_timestamps(cid: int, stream_key: str, request: Request,
                      user: dict = Depends(current_user)):
    """int64 little-endian microseconds, one per source frame, ascending.

    The frame map is *derived* from these and the stream's clock offset, so
    serving them is what makes that derivation checkable rather than a claim —
    `tests/invariants.mjs` recomputes the map and compares. ~93 kB for a
    ten-minute camera, cached hard because they never change.
    """
    host = _host(request)
    r = host.db.one("SELECT timestamps FROM streams WHERE capture_id=? AND key=?", cid, stream_key)
    if r is None:
        raise HTTPException(404, f"no stream {stream_key}")
    return Response(bytes(r["timestamps"] or b""), media_type="application/octet-stream",
                    headers={"cache-control": "private, max-age=86400"})


@router.get("/captures/{cid}/streams/{stream_key}/framemap")
def frame_map(cid: int, stream_key: str, request: Request, user: dict = Depends(current_user)):
    """int32 little-endian, one entry per capture frame. Empty = identity."""
    host = _host(request)
    r = host.db.one("SELECT frame_map FROM streams WHERE capture_id=? AND key=?", cid, stream_key)
    if r is None:
        raise HTTPException(404, f"no stream {stream_key}")
    blob = r["frame_map"] or b""
    return Response(bytes(blob), media_type="application/octet-stream",
                    headers={"cache-control": "private, max-age=86400"})


# -------------------------------------------------------------------- layers
@router.get("/layers/{lid}/objects")
def layer_objects(lid: int, request: Request, stream: str | None = None,
                  user: dict = Depends(current_user)):
    host = _host(request)
    sql = ("SELECT o.*, s.key AS stream FROM objects o JOIN streams s ON s.id=o.stream_id "
           "WHERE o.layer_id=?")
    args = [lid]
    if stream:
        sql += " AND s.key=?"
        args.append(stream)
    rows = host.db.all(sql + " ORDER BY s.idx, o.first_frame", *args)
    return {"objects": [row_to_dict(r, ("meta",)) for r in rows]}


@router.get("/layers/{lid}/frames")
def layer_frames(lid: int, frame: int, request: Request, span: int = 0,
                 user: dict = Depends(current_user)):
    """Keyframes needed to draw this layer from capture frame `frame` to
    `frame+span`: the shape active at the start plus every keyframe inside the
    window, per stream. The core applies the frame map; it does not look inside
    a payload."""
    host = _host(request)
    lay = host.db.one("SELECT * FROM layers WHERE id=?", lid)
    if lay is None:
        raise HTTPException(404, f"no layer {lid}")
    cid = lay["capture_id"]
    out: dict[str, dict] = {}
    for st in host.db.all("SELECT * FROM streams WHERE capture_id=?", cid):
        fm = st["frame_map"]
        if fm:
            a = np.frombuffer(fm, dtype="<i4")
            i0 = int(a[min(max(frame, 0), len(a) - 1)])
            i1 = int(a[min(max(frame + span, 0), len(a) - 1)])
        else:
            i0, i1 = frame, frame + span
        rows = host.db.all(
            "SELECT s.object_id, s.frame, s.outside, s.payload FROM shapes s "
            "JOIN objects o ON o.id = s.object_id "
            "WHERE o.layer_id=? AND o.stream_id=? AND o.last_frame >= ? "
            "AND s.frame > ? AND s.frame <= ? "
            "ORDER BY s.object_id, s.frame", lid, st["id"], i0, i0 - 1, i1)
        active = host.db.all(
            "SELECT s.object_id, s.frame, s.outside, s.payload FROM shapes s JOIN ("
            "  SELECT s2.object_id AS oid, MAX(s2.frame) AS f FROM shapes s2 "
            "  JOIN objects o ON o.id = s2.object_id "
            "  WHERE o.layer_id=? AND o.stream_id=? AND o.last_frame >= ? "
            "  AND s2.frame <= ? GROUP BY s2.object_id"
            ") m ON m.oid = s.object_id AND m.f = s.frame", lid, st["id"], i0, i0)
        seen = set()
        keys = []
        for r in list(active) + list(rows):
            k = (r["object_id"], r["frame"])
            if k in seen:
                continue
            seen.add(k)
            keys.append([r["object_id"], r["frame"], r["outside"], json.loads(r["payload"])])
        out[st["key"]] = {"from": i0, "to": i1, "keys": keys}
    return {"layer": lid, "frame": frame, "span": span, "streams": out}


# ----------------------------------------------------------------------- ops
@router.get("/captures/{cid}/ops")
def get_ops(cid: int, request: Request, since: int = 0, limit: int = 5000,
            all: bool = False, user: dict = Depends(current_user)):
    """`all=true` includes ops that were undone — the history panel wants them,
    a fold never does."""
    return {"ops": _host(request).ops_since(cid, since, limit, include_undone=all)}


@router.post("/captures/{cid}/ops/{oid}/undo")
def undo_op(cid: int, oid: int, request: Request, user: dict = Depends(current_user)):
    try:
        return {"op": _host(request).set_undone(cid, oid, True, user["name"])}
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.post("/captures/{cid}/ops/{oid}/redo")
def redo_op(cid: int, oid: int, request: Request, user: dict = Depends(current_user)):
    try:
        return {"op": _host(request).set_undone(cid, oid, False, user["name"])}
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.post("/captures/{cid}/undo")
def undo_last(cid: int, request: Request, kind: str = "", user: dict = Depends(current_user)):
    """Ctrl+Z: take back *your* last op, optionally only within one plugin's
    namespace. Someone else's work is never undone out from under them."""
    host = _host(request)
    op = host.last_op_of(cid, user["name"], kind)
    if op is None:
        return {"op": None, "message": "you have nothing to undo in this capture"}
    return {"op": host.set_undone(cid, op["id"], True, user["name"])}


@router.post("/captures/{cid}/ops")
def post_op(cid: int, request: Request, body: dict = Body(...),
            user: dict = Depends(current_user)):
    host = _host(request)
    _capture(host, cid)
    kind = body.get("kind")
    if not kind or "." not in kind:
        raise HTTPException(400, "an op kind must be namespaced, e.g. 'identity.merge'")
    ns = kind.split(".", 1)[0]
    if ns not in host.plugins:
        raise HTTPException(400, f"no plugin owns ops named {ns}.*")
    op = host.append_op(cid, kind, body.get("payload") or {}, user["name"], body.get("layer_id"))
    return {"op": op}


# ---------------------------------------------------------------------- docs
@router.get("/captures/{cid}/docs/{ns}/{key}")
def doc_get(cid: int, ns: str, key: str, request: Request, user: dict = Depends(current_user)):
    host = _host(request)
    value, version = host.ctx(ns, user["name"]).doc_get(cid, key, default=None)
    return {"value": value, "version": version}


@router.put("/captures/{cid}/docs/{ns}/{key}")
def doc_put(cid: int, ns: str, key: str, request: Request, body: dict = Body(...),
            user: dict = Depends(current_user)):
    host = _host(request)
    try:
        v = host.ctx(ns, user["name"]).doc_put(cid, key, body.get("value"),
                                               body.get("version"))
    except Conflict as e:
        raise HTTPException(409, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"version": v}


# ---------------------------------------------------------------------- jobs
@router.get("/jobs")
def list_jobs(request: Request, capture: int | None = None, user: dict = Depends(current_user)):
    return {"jobs": _host(request).jobs.list(capture)}


@router.post("/jobs")
def submit_job(request: Request, body: dict = Body(...), user: dict = Depends(current_user)):
    host = _host(request)
    try:
        job = host.jobs.submit(body["plugin"], body["kind"], body.get("params") or {},
                               body.get("capture_id"), user["name"])
    except Blocked as e:
        # Not a 400: the request was well formed, the world is not ready for it.
        # The reason names the missing thing and what to do about it.
        raise HTTPException(409, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"job": job}


@router.get("/jobs/{jid}")
def get_job(jid: str, request: Request, user: dict = Depends(current_user)):
    job = _host(request).jobs.get(jid)
    if job is None:
        raise HTTPException(404, f"no job {jid}")
    return {"job": job}


@router.post("/jobs/{jid}/cancel")
def cancel_job(jid: str, request: Request, user: dict = Depends(current_user)):
    return {"cancelled": _host(request).jobs.cancel(jid)}


# --------------------------------------------------------------------- users
@router.get("/users")
def list_users(request: Request, user: dict = Depends(require("admin"))):
    rows = _host(request).db.all("SELECT id, name, display, role, created FROM users ORDER BY id")
    return {"users": [dict(r) for r in rows]}
