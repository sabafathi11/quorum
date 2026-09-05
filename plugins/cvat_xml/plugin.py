"""cvat_xml — read and write CVAT's track XML.

CVAT is where these annotations come from and where they often have to go back,
so the round trip is the thing that matters: a track imported and exported again
must be byte-comparable in the fields CVAT reads. Mask payloads are carried
verbatim (the RLE string is never re-encoded), which is what makes that true.
"""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

from quorum.sdk import AssetKind, Plugin

PLUGIN = Plugin(
    id="cvat_xml",
    name="CVAT XML",
    description="Import and export CVAT-format track XML, one file per stream.",
)

PLUGIN.asset_kind(AssetKind(
    id="tracks",
    title="CVAT track XML",
    description="One exported XML per stream. The stream is matched from the filename.",
    accept=[".xml"], multiple=True, icon="⟨⟩"))

STREAM_RE = re.compile(r"(cam\d+|stream[-_]?\w+)", re.I)


def parse_tracks(path: Path, max_frame: int = 0):
    """iterparse, because these files run to 40 MB each."""
    tracks, cur = [], None
    for event, el in ET.iterparse(path, events=("start", "end")):
        if event == "start" and el.tag == "track":
            cur = {"key": el.get("id"), "label": el.get("label", ""), "frames": []}
        elif event == "end":
            if el.tag in ("mask", "box", "polygon") and cur is not None:
                frame = int(el.get("frame"))
                if not max_frame or frame <= max_frame:
                    if el.tag == "mask":
                        payload = {"box": [int(float(el.get(k))) for k in ("left", "top", "width", "height")],
                                   "rle": el.get("rle", "")}
                    elif el.tag == "box":
                        x1, y1 = float(el.get("xtl")), float(el.get("ytl"))
                        x2, y2 = float(el.get("xbr")), float(el.get("ybr"))
                        payload = {"box": [int(x1), int(y1), int(x2 - x1), int(y2 - y1)]}
                    else:
                        payload = {"points": el.get("points", "")}
                    cur["frames"].append({"frame": frame,
                                          "outside": int(el.get("outside", "0")),
                                          "payload": payload})
                el.clear()
            elif el.tag == "track" and cur is not None:
                cur["frames"].sort(key=lambda f: f["frame"])
                if cur["frames"]:
                    tracks.append(cur)
                cur = None
                el.clear()
    return tracks


@PLUGIN.importer(
    "tracks", title="Import CVAT XML",
    params={"capture_id": {"type": "capture", "required": True},
            "assets": {"type": "asset", "kind": "cvat_xml.tracks", "multiple": True,
                       "label": "Uploaded XML files",
                       "description": "Each file is matched to a stream by the stream key "
                                      "in its name."},
            "dir": {"type": "string", "label": "…or a directory on the server", "default": "",
                    "description": "Left over from before uploads existed; either works."},
            "layer": {"type": "string", "default": "masks", "label": "Layer key"},
            "name": {"type": "string", "default": "Masks", "label": "Layer name"},
            "type": {"type": "string", "default": "mask.rle", "label": "Layer type"},
            "provenance": {"type": "string", "default": "model",
                           "options": ["human", "model", "derived"]},
            "max_frame": {"type": "integer", "default": 0, "label": "Stop after this frame (0 = all)"}})
def import_xml(ctx):
    """Uploaded files or a server directory, whichever was given.

    The importer does not know that uploading exists: `ctx.asset(id)` hands it
    a path, exactly as `Path(dir)` does. That is the point of the asset param
    type — no second code path, and no plugin learning about the upload
    machinery in order to be fed by it.
    """
    cid = int(ctx.params["capture_id"])
    streams = {r["key"]: r["id"] for r in
               ctx.db.all("SELECT id, key FROM streams WHERE capture_id=?", cid)}
    picked = ctx.params.get("assets")
    files, names = [], {}
    if picked:
        for aid in ([picked] if isinstance(picked, (int, str)) else picked):
            row = ctx.db.one("SELECT * FROM assets WHERE id=?", int(aid))
            p = ctx.asset(aid, "cvat_xml.tracks")
            files.append(p)
            names[p] = row["name"]           # match on what it was uploaded as
    elif str(ctx.params.get("dir") or "").strip():
        d = Path(str(ctx.params["dir"]).strip()).expanduser()
        if not d.is_dir():
            raise NotADirectoryError(f"{d} is not a directory")
        files = sorted(d.glob("*.xml"))
    else:
        raise ValueError("give some uploaded XML files, or a directory on the server")
    if not files:
        raise FileNotFoundError("no XML files to import")
    files = sorted(files, key=lambda f: names.get(f, f.name))

    lay = ctx.write.layer(cid, key=ctx.params.get("layer") or "masks",
                          name=ctx.params.get("name") or "Masks",
                          type=ctx.params.get("type") or "mask.rle",
                          provenance=ctx.params.get("provenance") or "model",
                          config={"source": ", ".join(names.get(f, f.name) for f in files)},
                          replace=True)
    total, matched = 0, 0
    for i, f in enumerate(files):
        ctx.check()
        m = STREAM_RE.search(names.get(f, f.name))
        key = m.group(1).lower() if m else None
        if key not in streams:
            ctx.log(f"{names.get(f, f.name)}: no stream matches — skipped")
            continue
        tracks = parse_tracks(f, int(ctx.params.get("max_frame") or 0))
        ctx.write.objects(lay, streams[key], tracks)
        matched += 1
        total += sum(len(t["frames"]) for t in tracks)
        ctx.progress((i + 1) / len(files), f"{key}: {len(tracks)} tracks, {total} keyframes")

    ctx.emit_op(cid, "imported", {"layer": lay, "keyframes": total,
                                  "files": [names.get(f, f.name) for f in files]})
    return {"layer_id": lay, "files": matched, "keyframes": total,
            "message": f"{matched} file(s), {total} keyframes"}


@PLUGIN.exporter(
    "tracks", title="Export CVAT XML",
    params={"capture_id": {"type": "capture", "required": True},
            "layer_id": {"type": "integer", "label": "Layer id", "default": 0,
                         "description": "0 exports what is on screen: every mask layer, "
                                        "minus tracks that were replaced, deleted or purged."}})
def export_xml(ctx):
    """One file per stream, keyed to that stream's own frames — so a result
    shared from here stays frame-for-frame in step with the original video.

    With no explicit layer this exports the *effective* set rather than one
    layer's rows. That distinction matters the moment mask editing exists: a
    cut track keeps its original key, so the imported layer still holds a copy
    of it, and exporting that layer would quietly ship the unedited masks.
    """
    cid = int(ctx.params["capture_id"])
    lid = int(ctx.params.get("layer_id") or 0)
    cap = ctx.db.one("SELECT * FROM captures WHERE id=?", cid)
    lay = ctx.db.one("SELECT * FROM layers WHERE id=?", lid) if lid else None
    if lid and lay is None:
        raise ValueError(f"no layer {lid}")

    masks = ctx.host.plugins.get("masks")
    if lid:
        picked, label = None, lay["key"]
    elif masks is None:
        raise ValueError("give a layer_id — the masks plugin is not installed, so there is "
                         "no edited set to export")
    else:
        rows = sys.modules[masks.module_name].effective_objects(ctx.host, cid)
        picked, label = {}, "edited"
        for r in rows:
            picked.setdefault(r["stream"], []).append(r)
        ctx.log(f"exporting {len(rows)} effective track(s)")
    out = ctx.cfg.data_dir / "exports" / f"{cap['key']}_{label}"
    out.mkdir(parents=True, exist_ok=True)

    streams = ctx.db.all("SELECT * FROM streams WHERE capture_id=? ORDER BY idx", cid)
    written = []
    for i, st in enumerate(streams):
        ctx.check()
        if picked is None:
            objs = [dict(r) for r in ctx.db.all(
                "SELECT * FROM objects WHERE layer_id=? AND stream_id=? ORDER BY id",
                lid, st["id"])]
        else:
            objs = [{"id": r["object_id"], "label": r["label"]} for r in picked.get(st["key"], [])]
        lines = ['<?xml version="1.0" encoding="utf-8"?>', "<annotations>",
                 '  <version>1.1</version>',
                 f'  <meta><task><name>{escape(cap["key"])}</name>'
                 f'<size>{st["n_frames"]}</size>'
                 f'<original_size><width>{st["width"]}</width>'
                 f'<height>{st["height"]}</height></original_size>'
                 f'</task><source>quorum</source></meta>']
        for n, o in enumerate(objs):
            lines.append(f'  <track id="{n}" label="{escape(o["label"] or "object")}" source="quorum">')
            for sh in ctx.db.all("SELECT * FROM shapes WHERE object_id=? ORDER BY frame", o["id"]):
                p = json.loads(sh["payload"])
                box = p.get("box")
                if box and "rle" in p:
                    l, t, w, hh = box
                    lines.append(
                        f'    <mask frame="{sh["frame"]}" keyframe="1" outside="{sh["outside"]}" '
                        f'occluded="0" rle="{p["rle"]}" left="{l}" top="{t}" width="{w}" height="{hh}" z_order="0"></mask>')
                elif box:
                    l, t, w, hh = box
                    lines.append(
                        f'    <box frame="{sh["frame"]}" keyframe="1" outside="{sh["outside"]}" '
                        f'occluded="0" xtl="{l}" ytl="{t}" xbr="{l + w}" ybr="{t + hh}" z_order="0"></box>')
            lines.append("  </track>")
        lines.append("</annotations>")
        p = out / f"{st['key']}_{cap['key']}_{label}.xml"
        p.write_text("\n".join(lines))
        written.append(str(p))
        ctx.progress((i + 1) / len(streams), f"{st['key']} written")

    return {"files": written, "dir": str(out),
            "message": f"{len(written)} file(s) → {out}"}
