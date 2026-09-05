"""Uploads. The core stores bytes and a kind string; it never opens the file.

Resumable, because a camera video here is 150 MB and this is meant to run over
a 1.5 MB/s link one day. Three calls:

    POST   /api/captures/{cid}/assets   {name, size, kind}   -> {asset}
    PUT    /api/assets/{id}/data?offset=N   <raw bytes>      -> {received}
    POST   /api/assets/{id}/complete                         -> {asset}

`offset` is checked against what is actually on disk, so a client that
reconnects asks `GET /api/assets/{id}` where it got to and carries on. An
interrupted upload leaves a row in state `uploading`, which is a thing you can
see and resume rather than a mystery file.

`PATCH` changes an asset's *kind* — the "I filed this as the wrong thing"
case, which must never mean re-uploading 150 MB.

And one call that skips all three, for the file that is *already on the
server*:

    POST   /api/captures/{cid}/assets/link  {path, kind}  -> {asset}

It copies nothing. The row points at the file where it lies, inside the media
roots, and is `ready` from the first instant — which is the whole point: on the
deployment that matters the videos are a read-only share the server can already
open, and uploading one to itself is work for its own sake. A linked asset is
marked `meta.linked`, and deleting it deletes the row and never the file.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from ..db import now, row_to_dict
from .deps import current_user
from .media import serve_file

router = APIRouter()

CHUNK = 1024 * 1024
ASSET_JSON = ("meta",)


def _host(request: Request):
    return request.app.state.host


def _asset(host, aid: int) -> dict:
    r = host.db.one("SELECT * FROM assets WHERE id=?", aid)
    if r is None:
        raise HTTPException(404, f"no asset {aid}")
    return dict(r)


def _out(host, row: dict) -> dict:
    d = row_to_dict(row if not hasattr(row, "keys") else row, ASSET_JSON)
    kinds = host.asset_kinds()
    k = kinds.get(d.get("kind"))
    d["kindTitle"] = k["kind"].title if k else (d.get("kind") or "unsorted")
    d["kindPlugin"] = k["plugin"] if k else ""
    d["known"] = bool(k)
    # A linked asset is the one case where the path *is* the client's
    # business: it is what the person picked, it is not ours to have moved,
    # and "which file is this row about" is unanswerable without it.
    linked = bool((d.get("meta") or {}).get("linked"))
    d["linked"] = linked
    if linked:
        d["source_path"] = row["path"]
        d["present"] = Path(row["path"]).is_file()
    d.pop("path", None)          # otherwise the server's filesystem is not their business
    return d


# ------------------------------------------------------------------ listing
@router.get("/assets")
def list_assets(request: Request, capture: int | None = None, kind: str = "",
                user: dict = Depends(current_user)):
    host = _host(request)
    sql = "SELECT * FROM assets WHERE 1=1"
    args: list = []
    if capture is not None:
        sql += " AND capture_id=?"
        args.append(capture)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    rows = host.db.all(sql + " ORDER BY id DESC", *args)
    return {"assets": [_out(host, dict(r)) for r in rows],
            "kinds": [{"id": k, "plugin": v["plugin"], "pluginName": v["pluginName"],
                       **{f: getattr(v["kind"], f) for f in
                          ("title", "description", "accept", "multiple", "icon")}}
                      for k, v in sorted(host.asset_kinds().items())]}


@router.get("/assets/{aid}")
def get_asset(aid: int, request: Request, user: dict = Depends(current_user)):
    host = _host(request)
    return {"asset": _out(host, _asset(host, aid))}


# ------------------------------------------------------------------- upload
@router.post("/captures/{cid}/assets")
def create_asset(cid: int, request: Request, body: dict = Body(...),
                 user: dict = Depends(current_user)):
    return _create(request, cid, body, user)


@router.post("/assets")
def create_library_asset(request: Request, body: dict = Body(...),
                         user: dict = Depends(current_user)):
    """An asset with no capture — something reused across captures, like one
    calibration for a room that does not move."""
    return _create(request, None, body, user)


def _create(request: Request, cid: int | None, body: dict, user: dict) -> dict:
    host = _host(request)
    if cid is not None and host.db.one("SELECT id FROM captures WHERE id=?", cid) is None:
        raise HTTPException(404, f"no capture {cid}")
    name = str(body.get("name") or "file").strip()
    size = int(body.get("size") or 0)
    kind = str(body.get("kind") or "")
    if size > host.cfg.max_upload:
        raise HTTPException(413, f"{name} is {size / 1e9:.1f} GB; the limit is "
                                 f"{host.cfg.max_upload / 1e9:.1f} GB")
    if kind and kind not in host.asset_kinds():
        raise HTTPException(400, f"no plugin declares an asset kind named {kind!r}")
    t = now()
    aid = host.db.insert("assets", capture_id=cid, kind=kind, name=name, path="",
                         size=size, received=0, content_type=str(body.get("content_type") or ""),
                         state="uploading", meta="{}", actor=user["name"],
                         created=t, updated=t)
    path = host.upload_path(aid, name)
    path.write_bytes(b"")
    host.db.run("UPDATE assets SET path=? WHERE id=?", str(path), aid)
    return {"asset": _out(host, _asset(host, aid))}


# -------------------------------------------------------------------- link
@router.post("/captures/{cid}/assets/link")
def link_asset(cid: int, request: Request, body: dict = Body(...),
               user: dict = Depends(current_user)):
    return _link(request, cid, body, user)


@router.post("/assets/link")
def link_library_asset(request: Request, body: dict = Body(...),
                       user: dict = Depends(current_user)):
    return _link(request, None, body, user)


def _link(request: Request, cid: int | None, body: dict, user: dict) -> dict:
    """Register a file that is already on the server, by path.

    The one rule is the one the whole project already has: it must be inside
    `media_roots`, checked with the same function that guards serving it. That
    is not a formality here — this is the endpoint where a path arrives from a
    browser, so it is the endpoint where `../../etc/shadow` would arrive too.

    Inputs are never written, so nothing is copied and nothing is moved. The
    consequence to keep in mind is that a linked asset is only as stable as the
    file behind it: if somebody unmounts the share, the row stays and the file
    is gone. That is visible — `present` says so — and is the honest trade for
    not duplicating 150 MB per camera.
    """
    host = _host(request)
    if cid is not None and host.db.one("SELECT id FROM captures WHERE id=?", cid) is None:
        raise HTTPException(404, f"no capture {cid}")
    path = Path(str(body.get("path") or "").strip())
    if not str(path):
        raise HTTPException(400, "a path on the server is required")
    if not host.cfg.is_media_allowed(path):
        raise HTTPException(403, f"{path} is outside the media roots this server will read")
    if not path.is_file():
        raise HTTPException(404, f"{path} is not a file on this server")
    if not os.access(path, os.R_OK):
        raise HTTPException(403, f"{path} exists but this server cannot read it — "
                                 f"check the bind mount and who the container runs as")
    kind = str(body.get("kind") or "")
    if kind and kind not in host.asset_kinds():
        raise HTTPException(400, f"no plugin declares an asset kind named {kind!r}")
    dup = host.db.one("SELECT * FROM assets WHERE path=? AND (capture_id IS ? OR capture_id=?)",
                      str(path), cid, cid)
    if dup is not None:
        # Linking the same file twice is a double-click, not an error worth
        # losing the click over. Give back the row that already exists.
        return {"asset": _out(host, dict(dup)), "existing": True}
    t = now()
    size = path.stat().st_size
    aid = host.db.insert("assets", capture_id=cid, kind=kind,
                         name=str(body.get("name") or path.name), path=str(path),
                         size=size, received=size, content_type="",
                         state="ready", meta=json.dumps({"linked": True}),
                         actor=user["name"], created=t, updated=t)
    a = _asset(host, aid)
    job = _on_ready(host, a, user)
    host.publish(cid, {"t": "assets", "capture_id": cid})
    return {"asset": _out(host, a), "job": job}


@router.put("/assets/{aid}/data")
async def put_data(aid: int, request: Request, offset: int = 0,
                   user: dict = Depends(current_user)):
    host = _host(request)
    a = _asset(host, aid)
    if a["state"] == "ready":
        raise HTTPException(409, f"{a['name']} is already uploaded — delete it to replace it")
    path = Path(a["path"])
    have = path.stat().st_size if path.exists() else 0
    if offset != have:
        # Not an error the client can ignore: it tells them exactly where to
        # resume from, which is the entire point of doing it this way.
        raise HTTPException(409, f"resume from byte {have}, not {offset}",
                            headers={"x-quorum-received": str(have)})
    written = have
    with open(path, "ab") as f:
        async for chunk in request.stream():
            if not chunk:
                continue
            written += len(chunk)
            if written > host.cfg.max_upload:
                f.flush()
                raise HTTPException(413, "upload exceeds the configured limit")
            f.write(chunk)
    host.db.run("UPDATE assets SET received=?, updated=? WHERE id=?", written, now(), aid)
    return {"received": written, "size": a["size"]}


@router.post("/assets/{aid}/complete")
def complete(aid: int, request: Request, user: dict = Depends(current_user)):
    host = _host(request)
    a = _asset(host, aid)
    path = Path(a["path"])
    if not path.exists():
        raise HTTPException(409, f"{a['name']} has no bytes on disk")
    size = path.stat().st_size
    if a["size"] and size != a["size"]:
        raise HTTPException(409, f"{a['name']} is {size} bytes, {a['size']} were promised — "
                                 f"resume from byte {size}")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while (b := f.read(1 << 20)):
            h.update(b)
    host.db.run("UPDATE assets SET state='ready', size=?, received=?, sha256=?, updated=? "
                "WHERE id=?", size, size, h.hexdigest(), now(), aid)
    a = _asset(host, aid)
    job = _on_ready(host, a, user)
    host.publish(a["capture_id"], {"t": "assets", "capture_id": a["capture_id"]})
    return {"asset": _out(host, a), "job": job}


def _on_ready(host, a: dict, user: dict) -> dict | None:
    """An asset kind may name a job to run when a file of that kind lands —
    probing a video, validating a calibration. The core submits it and knows
    nothing about what it does."""
    spec = host.asset_kinds().get(a["kind"])
    if not spec or not spec["kind"].on_ready:
        return None
    try:
        return host.jobs.submit(spec["plugin"], spec["kind"].on_ready,
                                {"asset_id": a["id"], "capture_id": a["capture_id"]},
                                a["capture_id"], user["name"])
    except Exception:
        return None                    # a failed probe must not fail the upload


# ------------------------------------------------------------ edit / delete
@router.patch("/assets/{aid}")
def patch_asset(aid: int, request: Request, body: dict = Body(...),
                user: dict = Depends(current_user)):
    """Rename, re-file, or move an asset to another capture.

    Changing `kind` is the "I uploaded this as the wrong thing" case. It must
    never mean uploading 150 MB again, and it must not need the plugin that
    consumes the new kind to be involved — whoever wants that kind will simply
    see it appear.
    """
    host = _host(request)
    a = _asset(host, aid)
    sets, args = [], []
    if "kind" in body:
        kind = str(body["kind"] or "")
        if kind and kind not in host.asset_kinds():
            raise HTTPException(400, f"no plugin declares an asset kind named {kind!r}")
        sets.append("kind=?")
        args.append(kind)
    if "name" in body:
        sets.append("name=?")
        args.append(str(body["name"]))
    if "capture_id" in body:
        sets.append("capture_id=?")
        args.append(body["capture_id"])
    if "meta" in body:
        sets.append("meta=?")
        args.append(json.dumps(body["meta"] or {}))
    if not sets:
        return {"asset": _out(host, a)}
    sets.append("updated=?")
    args.append(now())
    host.db.run(f"UPDATE assets SET {','.join(sets)} WHERE id=?", *args, aid)
    a2 = _asset(host, aid)
    if body.get("kind") and body["kind"] != a["kind"]:
        _on_ready(host, a2, user)
    host.publish(a2["capture_id"], {"t": "assets", "capture_id": a2["capture_id"]})
    return {"asset": _out(host, a2)}


@router.delete("/assets/{aid}")
def delete_asset(aid: int, request: Request, user: dict = Depends(current_user)):
    host = _host(request)
    a = _asset(host, aid)
    # An asset we were *given* is ours to delete; one we were merely pointed at
    # is not. Getting this backwards would mean a picker click could erase a
    # camera's only copy of a recording off a read-only share the day somebody
    # remounts it read-write.
    if not json.loads(a["meta"] or "{}").get("linked"):
        try:
            Path(a["path"]).unlink(missing_ok=True)
        except OSError:
            pass
    host.db.run("DELETE FROM assets WHERE id=?", aid)
    host.publish(a["capture_id"], {"t": "assets", "capture_id": a["capture_id"]})
    return {"ok": True, "deleted": a["name"]}


# ------------------------------------------------------------------ serving
@router.get("/assets/{aid}/data")
def asset_data(aid: int, request: Request, user: dict = Depends(current_user)):
    host = _host(request)
    a = _asset(host, aid)
    return serve_file(request, Path(a["path"]), host.cfg,
                      download_name=a["name"])
