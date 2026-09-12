"""identity — the cross-stream claim that two tracks are the same thing.

This is the only entity in the system that spans streams, and it is a plugin
because most multi-view work never needs it. Three ops, folded into one map:

    identity.link    {items}        these belong together (smallest existing id wins, else a new one)
    identity.assign  {items, cid}   put these on exactly this id
    identity.clear   {items}        back to unassigned

`items` is a list of [stream key, object key]. The fold is order-dependent only
where it has to be (allocating a fresh id), which is why the op log is the
source of truth and the map below is a cache of it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import Depends, Request

from quorum.api.deps import current_user
from quorum.filters import ObjectFilter
from quorum.sdk import Plugin, Tool

PLUGIN = Plugin(
    id="identity",
    name="Identity",
    description="Stitch tracks across streams into one identity per person.",
    web="identity.js",
)

PLUGIN.tool(Tool(id="identity", title="Identity", icon="◈", order=10,
                 needs_layer_types=["mask.rle"],
                 description="Give the same id to tracks that are the same thing in different cameras."))

OUTSIDE = 200          # the sentinel the sync-id UI uses for 'person is outside'


# --------------------------------------------------------------------- fold
def fold(ops: list[dict]) -> dict:
    """Op log -> {stream: {object key: cid}}. Pure, so a client can run it too."""
    a: dict[str, dict[str, int]] = {}
    used: set[int] = set()

    def cur(stream, key):
        return a.get(stream, {}).get(key)

    for op in ops:
        kind = op["kind"]
        p = op.get("payload") or {}
        items = [(str(s), str(k)) for s, k in (p.get("items") or [])]
        if kind == "identity.clear":
            for s, k in items:
                a.get(s, {}).pop(k, None)
        elif kind == "identity.assign":
            cid = int(p.get("cid"))
            if cid != OUTSIDE:
                used.add(cid)
            for s, k in items:
                a.setdefault(s, {})[k] = cid
        elif kind == "identity.link":
            now = [cur(s, k) for s, k in items]
            have = sorted({c for c in now if c is not None and c != OUTSIDE})
            if have:
                cid = have[0]
            elif now and all(c == OUTSIDE for c in now):
                cid = OUTSIDE          # linking two tracks both flagged outside keeps them outside
            else:
                cid = max(used, default=0) + 1
            if cid != OUTSIDE:
                used.add(cid)
            # linking two groups re-labels both, not just the clicked members
            merge = set(have)
            for s, m in a.items():
                for k, c in list(m.items()):
                    if c in merge:
                        m[k] = cid
            for s, k in items:
                a.setdefault(s, {})[k] = cid
        elif kind == "identity.outside":
            for s, k in items:
                a.setdefault(s, {})[k] = OUTSIDE
    return {s: m for s, m in a.items() if m}


def state_for(host, capture_id: int) -> dict:
    ops = [o for o in host.ops_since(capture_id, 0) if o["kind"].startswith("identity.")]
    assign = fold(ops)
    cids = sorted({c for m in assign.values() for c in m.values() if c != OUTSIDE})
    return {"assignments": assign, "lastOp": ops[-1]["id"] if ops else 0,
            "cids": cids, "nextCid": (max(cids) + 1) if cids else 1,
            "counts": {str(c): sum(1 for m in assign.values() for v in m.values() if v == c)
                       for c in cids + [OUTSIDE]}}


@PLUGIN.route.get("/{capture_id}/state")
def get_state(capture_id: int, request: Request, user: dict = Depends(current_user)):
    return state_for(request.app.state.host, capture_id)


# ---------------------------------------------------------------- problems
# The `Shift`+`Space` walk. A *problem frame* is one showing either a track with
# no identity, or the same identity twice in one camera — which is impossible,
# one person cannot be in two places in the same picture. OUTSIDE is exempt: it
# is a flag, not an identity, so any number of tracks may carry it.
#
# This is server-side because it needs every shape's outside flag, and the
# client holds only a two-second window. Intervals are cached per layer and
# survive identity edits, which are the ones that actually happen often.
_INTERVALS: dict[tuple, tuple] = {}


def mask_layers(host, capture_id: int) -> list[int]:
    return [r["id"] for r in host.db.all(
        "SELECT id FROM layers WHERE capture_id=? AND type='mask.rle' ORDER BY id", capture_id)]


def hidden_filter(host, capture_id: int):
    """Is this `(stream, key, layer)` still a track a human should be offered?

    Two different things are being asked here and they have different scopes,
    which is the whole reason this exists as one function:

      **superseded** — a track that was cut or joined is still sitting in the
        *imported* layer, but the edited copy in the derived layer is what
        anyone is actually looking at. Counting the imported copy as
        unidentified would send the walk to frames where nothing is wrong.
        This applies **only to the source layer**.

      **gone** — deleted or purged. Somebody has already decided about it, in
        every layer it appears in.

    Flattening those two into one `{stream: keys}` set is a bug with a long
    fuse, and it shipped. `masks.effective()` states the rule correctly —

        if src and r["layer_id"] == src["id"] and key in shadow.get(stream, set()):

    — and this did not, so a key was dropped from *every* layer. On a capture
    built the ordinary way that is merely wrong-but-quiet: the imported copy is
    skipped (right) and so is the edited copy (wrong), and with 1,813 tracks
    there is always another problem to walk to, so nobody notices.

    On a capture with **no imported layer at all** it is fatal. Every track was
    created in place — which is what the SAM-first workflow on the GPU box
    produces — so `source_layer` is None, the only layer *is* the derived one,
    and every edited track shadows itself out of existence. Shift+Space then
    reports "Nothing left" on a capture full of unidentified tracks, which is
    exactly how it was found.

    Returns a predicate rather than a set, because the layer is half the
    question and a set cannot carry it.
    """
    masks = host.plugins.get("masks")
    shadow: dict[str, set] = {}
    gone: dict[str, set] = {}
    src_id = None
    if masks is not None:
        try:
            mod = sys.modules[masks.module_name]
            doc, _ = host.ctx("masks").doc_get(capture_id, "shadow", default={"streams": {}})
            for s, v in (doc or {}).get("streams", {}).items():
                shadow.setdefault(s, set()).update(str(k) for k in v)
            proj = mod.fold(host.ops_since(capture_id, 0))
            for m in (proj.deleted, proj.purged):
                for s, keys in m.items():
                    gone.setdefault(s, set()).update(str(k) for k in keys)
            src = mod.source_layer(host.db, capture_id)
            src_id = src["id"] if src else None
        except Exception:
            pass

    def hidden(stream: str, key, layer_id: int | None = None) -> bool:
        k = str(key)
        if k in gone.get(stream, ()):
            return True
        # `layer_id is None` means the caller does not know which copy this is.
        # Then the shadow cannot be applied safely in either direction, and not
        # hiding a live track is the better failure of the two.
        return (src_id is not None and layer_id == src_id
                and k in shadow.get(stream, ()))

    return hidden


def visibility(host, capture_id: int, layer_id: int, streams: set[str] | None = None) -> dict[str, list]:
    """{stream key: [(start, end, object id, object key)]} in *stream* frames.

    A track is visible from a keyframe with outside=0 until the next keyframe
    with outside=1 (or its last frame). That is CVAT's interpolation rule and
    the one every tool in this tree already assumes.
    """
    n = host.db.scalar(
        "SELECT COUNT(*) FROM shapes sh JOIN objects o ON o.id=sh.object_id WHERE o.layer_id=?",
        layer_id)
    stream_key = tuple(sorted(streams)) if streams is not None else None
    cache_key = (capture_id, layer_id, stream_key)
    hit = _INTERVALS.get(cache_key)
    if hit and hit[0] == n:
        return hit[1]
    out: dict[str, list] = {}
    for st in host.db.all("SELECT id, key FROM streams WHERE capture_id=? ORDER BY idx", capture_id):
        if streams is not None and st["key"] not in streams:
            continue
        spans = []
        rows = host.db.all(
            "SELECT o.id AS oid, o.key AS okey, o.last_frame, sh.frame, sh.outside "
            "FROM shapes sh JOIN objects o ON o.id=sh.object_id "
            "WHERE o.layer_id=? AND o.stream_id=? ORDER BY o.id, sh.frame",
            layer_id, st["id"])
        cur_oid = None
        open_at = None
        for r in rows:
            if r["oid"] != cur_oid:
                if open_at is not None:
                    spans.append((open_at, last_end, cur_oid, cur_key))
                cur_oid, cur_key, open_at = r["oid"], r["okey"], None
            last_end = r["last_frame"]
            if r["outside"]:
                if open_at is not None:
                    spans.append((open_at, r["frame"], cur_oid, cur_key))
                    open_at = None
            elif open_at is None:
                open_at = r["frame"]
        if open_at is not None:
            spans.append((open_at, last_end, cur_oid, cur_key))
        out[st["key"]] = spans
    _INTERVALS[cache_key] = (n, out)
    return out


def forget_intervals(capture_id: int | None = None) -> None:
    """Called when a plugin rewrites shapes (mask editing does)."""
    for k in [k for k in _INTERVALS if capture_id is None or k[0] == capture_id]:
        _INTERVALS.pop(k, None)


def _filtered_out(host, capture_id: int, want: ObjectFilter) -> set:
    """Object ids the user is not being shown, by the core's own vocabulary."""
    where, args = want.sql("o", "l", "s")
    rows = host.db.all(
        "SELECT o.id FROM objects o JOIN layers l ON l.id=o.layer_id "
        "JOIN streams s ON s.id=o.stream_id "
        f"WHERE l.capture_id=? AND NOT ({where})", capture_id, *args)
    return {r["id"] for r in rows}


def _frame_maps(host, capture_id: int) -> dict[str, "np.ndarray | None"]:
    import numpy as np
    out = {}
    for st in host.db.all("SELECT key, frame_map FROM streams WHERE capture_id=?", capture_id):
        fm = st["frame_map"]
        out[st["key"]] = np.frombuffer(fm, dtype="<i4") if fm else None
    return out


def scan(host, capture_id: int, layer_id: int = 0, frame: int = 0,
         direction: int = 1, limit: int = 1, filter: str = "",
         include_hidden: bool = False) -> dict:
    """The next capture frame at or after `frame` that needs a human.

    Every mask layer is scanned, not one: after an edit, a track may be served
    from the imported layer or from the derived one, and superseded keys are
    dropped so a joined track does not report its own ingredients as unset.

    `limit` is a small navigation batch.  This deliberately does *not* count
    all problems: the annotator needs the next few places to visit, not a
    number that makes every click scan the rest of a long capture.

    `filter` is the client's object filter, verbatim. This endpoint *offers an
    object to a human*, so it has to honour it: a class the user has hidden
    must not be what the walk stops on. The client's display authority would
    refuse the answer anyway (see docs/DESIGN §4, "four doors"), but that would
    mean walking to a frame and then declining to point at anything, which
    reads as a bug. Skipping here is what makes it feel right.

    A plain function, taking a host rather than a request, because the whole of
    Shift+Space is decided in here and it had no unit test — its only coverage
    was a browser check needing the six-camera fixture, which cannot express
    the case that was broken (a capture with no imported layer). A route is a
    bad place to keep logic you need to test against a database you built three
    lines ago. See tests/test_identity.py.
    """
    import numpy as np
    assign = state_for(host, capture_id)["assignments"]
    hidden = hidden_filter(host, capture_id)
    want = ObjectFilter.parse(filter)
    excluded = set() if want.empty else _filtered_out(host, capture_id, want)
    allowed_streams = {r["key"] for r in host.db.all(
        "SELECT key FROM streams WHERE capture_id=? AND (? OR enabled=1)",
        capture_id, 1 if include_hidden else 0)}
    layers = [layer_id] if layer_id else mask_layers(host, capture_id)
    spans: dict[str, list] = {}
    for lid in layers:
        if lid in want.layers_exclude:
            continue
        for skey, sp in visibility(host, capture_id, lid, allowed_streams).items():
            spans.setdefault(skey, []).extend(
                s for s in sp
                if not hidden(skey, s[3], lid) and s[2] not in excluded)
    fmaps = _frame_maps(host, capture_id)
    n_frames = host.db.scalar("SELECT n_frames FROM captures WHERE id=?", capture_id) or 0

    # Every span, in capture frames. searchsorted inverts a nondecreasing map.
    events: list[tuple[int, int, str, int, str]] = []     # (start, end, stream, oid, key)
    for skey, sp in spans.items():
        fm = fmaps.get(skey)
        for (a, b, oid, okey) in sp:
            if fm is not None and len(fm):
                ca = int(np.searchsorted(fm, a, side="left"))
                cb = int(np.searchsorted(fm, b, side="left"))
            else:
                ca, cb = int(a), int(b)
            if cb <= ca:
                cb = ca + 1
            events.append((ca, min(cb, n_frames), skey, oid, str(okey)))
    events.sort()

    # One forward sweep with an incremental active set. Only frames where a
    # span starts or ends can change the answer, so those are the only ones
    # examined, and each span is added and removed exactly once.
    starts: dict[int, list] = {}
    ends: dict[int, list] = {}
    for e in events:
        starts.setdefault(e[0], []).append(e)
        ends.setdefault(e[1], []).append(e)
    marks = sorted(set(starts) | set(ends))

    live: dict[int, tuple] = {}                 # object id -> event
    found: list[dict] = []                      # the nearby navigation batch
    take = max(1, min(int(limit), 16))          # never turn navigation into a full scan
    prev_sig = None
    for m in marks:
        if m >= n_frames:
            break
        for e in ends.get(m, ()):
            live.pop(e[3], None)
        for e in starts.get(m, ()):
            live[e[3]] = e
        if m < 0:
            continue
        unset = []
        by_cid: dict[tuple, list] = {}
        for (_, _, skey, oid, okey) in live.values():
            cid = (assign.get(skey) or {}).get(okey)
            if cid is None:
                unset.append((skey, oid, okey))
            elif cid != OUTSIDE:
                by_cid.setdefault((skey, cid), []).append((skey, oid, okey))
        dup = [(k[1], v) for k, v in by_cid.items() if len(v) > 1]
        if not unset and not dup:
            prev_sig = None
            continue
        if dup:
            cid, members = sorted(dup)[0]
            p = {"frame": m, "kind": "duplicate", "cid": cid, "stream": members[0][0],
                 "objects": sorted(o for (_, o, _) in members),
                 "why": f"id {cid} is on {len(members)} masks in {members[0][0]}"}
        else:
            skey, oid, okey = sorted(unset)[0]
            p = {"frame": m, "kind": "unset", "stream": skey, "objects": [oid],
                 "why": f"{skey}: track {okey} has no identity"}
        # collapse a run of marks that all complain about the same thing: the
        # walk should land on the moment a problem *appears*, not on every
        # frame where some other track happens to start.
        sig = (p["kind"], p.get("cid"), tuple(p["objects"]))
        if sig != prev_sig:
            if (direction >= 0 and m >= frame) or (direction < 0 and m <= frame):
                found.append(p)
                # The normal Next path is a forward walk.  Once its small
                # batch is full, later marks cannot change any answer in it.
                if direction >= 0 and len(found) >= take:
                    break
        prev_sig = sig

    if direction < 0:
        found.reverse()
    return {"problems": found[:take], "spans": len(events)}


@PLUGIN.route.get("/{capture_id}/problems")
def problems(capture_id: int, request: Request, layer_id: int = 0, frame: int = 0,
             direction: int = 1, limit: int = 1, filter: str = "",
             include_hidden: bool = False,
             user: dict = Depends(current_user)):
    """`scan`, over HTTP. Everything it decides is in `scan`."""
    return scan(request.app.state.host, capture_id, layer_id=layer_id, frame=frame,
                direction=direction, limit=limit, filter=filter, include_hidden=include_hidden)


# ---------------------------------------------------------------- validator
_LIVE: dict[int, tuple] = {}


def _world_version(host, capture_id: int) -> tuple:
    """Cheap stamp for "has anything that matters changed": the newest op and
    how many objects exist. Both are indexed lookups."""
    return (host.db.scalar("SELECT MAX(id) FROM ops WHERE capture_id=?", capture_id) or 0,
            host.db.scalar(
                "SELECT COUNT(*) FROM objects o JOIN layers l ON l.id=o.layer_id "
                "WHERE l.capture_id=?", capture_id) or 0)


def live_keys(host, capture_id: int) -> dict[str, set]:
    """{stream: keys of tracks that exist and are on screen right now}.

    Memoised: the validator runs once per queued proposal, and there are a
    thousand of them. Recomputing this for each turned listing the queue into a
    two-second request — and a queue nobody waits for is a queue nobody reads.
    """
    ver = _world_version(host, capture_id)
    hit = _LIVE.get(capture_id)
    if hit and hit[0] == ver:
        return hit[1]
    hidden = hidden_filter(host, capture_id)
    out: dict[str, set] = {}
    # `o.layer_id` is selected because the shadow only applies to the imported
    # copy. Without it every edited track counts as gone, the validator then
    # refuses identity ops that name a perfectly live track, and the person is
    # told their track "is no longer there" while they are looking at it.
    for r in host.db.all(
            "SELECT s.key AS stream, o.key AS okey, o.layer_id AS lid FROM objects o "
            "JOIN streams s ON s.id=o.stream_id JOIN layers l ON l.id=o.layer_id "
            "WHERE l.capture_id=? AND l.type='mask.rle'", capture_id):
        key = str(r["okey"])
        if hidden(r["stream"], key, r["lid"]):
            continue
        out.setdefault(r["stream"], set()).add(key)
    _LIVE[capture_id] = (ver, out)
    return out


@PLUGIN.validator("link", "assign", "clear", "outside")
def still_applies(host, capture_id: int, payload: dict) -> str | None:
    """An identity op names tracks. If they have since been cut, joined away or
    deleted, accepting it would write an assignment onto a key nothing points
    at — silent, invisible, and impossible to notice later."""
    live = live_keys(host, capture_id)
    gone = [f"{s}/{k}" for s, k in (payload.get("items") or [])
            if str(k) not in live.get(str(s), ())]
    if not gone:
        return None
    return (f"{gone[0]} no longer exists" if len(gone) == 1
            else f"{len(gone)} of these tracks no longer exist ({', '.join(gone[:3])})")


# ------------------------------------------------------------------- import
@PLUGIN.importer(
    "consistent_ids", title="Import consistent_ids.json",
    params={"capture_id": {"type": "capture", "required": True},
            "path": {"type": "string", "label": "Path to consistent_ids_<stamp>.json",
                     "description": "Leave blank to look beside sync_id_ui/ for this capture's stamp."}})
def import_ids(ctx):
    """Bring existing hand-made work in as ops, so history starts where the
    desktop tool left off instead of at zero."""
    cid = int(ctx.params["capture_id"])
    cap = ctx.db.one("SELECT * FROM captures WHERE id=?", cid)
    stamp = json.loads(cap["config"]).get("stamp") or cap["key"]
    path = ctx.params.get("path") or ""
    if not path:
        root = Path(__file__).resolve().parents[3]
        for cand in (root / "sync_id_ui" / f"consistent_ids_{stamp}.json",
                     root / "sync_id_ui" / "consistent_ids.json"):
            if cand.exists():
                path = str(cand)
                break
    if not path or not Path(path).exists():
        raise FileNotFoundError("no consistent_ids file found — give an explicit path")
    doc = json.loads(Path(path).read_text())
    assign = doc.get("assignments") or doc
    by_cid: dict[int, list] = {}
    for stream, m in assign.items():
        for key, c in m.items():
            by_cid.setdefault(int(c), []).append([stream, str(key)])
    for i, (c, items) in enumerate(sorted(by_cid.items())):
        ctx.check()
        ctx.emit_op(cid, "assign", {"items": items, "cid": c, "imported_from": Path(path).name})
        ctx.progress((i + 1) / len(by_cid), f"id {c}: {len(items)} tracks")
    n = sum(len(v) for v in by_cid.values())
    return {"identities": len(by_cid), "tracks": n,
            "message": f"{len(by_cid)} identities over {n} tracks from {Path(path).name}"}


@PLUGIN.exporter(
    "consistent_ids", title="Export consistent_ids.json",
    params={"capture_id": {"type": "capture", "required": True}})
def export_ids(ctx):
    """Writes into quorum's own export directory. Nothing this server does ever
    overwrites a file the desktop tools own."""
    cid = int(ctx.params["capture_id"])
    cap = ctx.db.one("SELECT * FROM captures WHERE id=?", cid)
    stamp = json.loads(cap["config"]).get("stamp") or cap["key"]
    st = state_for(ctx.host, cid)
    out = ctx.cfg.data_dir / "exports"
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"consistent_ids_{stamp}.json"
    p.write_text(json.dumps({"assignments": st["assignments"]}, indent=1, sort_keys=True))
    return {"path": str(p), "identities": len(st["cids"]),
            "message": f"{len(st['cids'])} identities → {p}"}


# ------------------------------------------------------------- a generator
@PLUGIN.job(
    "link_gaps", title="Suggest re-links after a gap",
    params={"capture_id": {"type": "capture", "required": True},
            "layer_id": {"type": "integer", "label": "Mask layer id", "required": True},
            "max_gap": {"type": "integer", "default": 60,
                        "label": "Largest gap to bridge (source frames)"},
            "max_move": {"type": "number", "default": 1.2,
                         "label": "Allowed drift, in box widths"}},
    description="Within one camera: a track that ends where another begins, a moment later, "
                "is usually the same person. Emits proposals — it never assigns anything.")
def link_gaps(ctx):
    """A deliberately small generator, here to prove the whole path from a
    machine opinion to a human decision exists. It uses only the boxes the masks
    already carry: no calibration, no model, nothing that has to be right about
    the world. A stronger generator (the voxel carve, which can link *across*
    cameras rather than only within one) is a drop-in replacement — same output
    shape, same queue, same accept button."""
    cid = int(ctx.params["capture_id"])
    lid = int(ctx.params["layer_id"])
    max_gap = int(ctx.params.get("max_gap") or 60)
    max_move = float(ctx.params.get("max_move") or 1.2)

    streams = ctx.db.all("SELECT * FROM streams WHERE capture_id=? ORDER BY idx", cid)
    # never propose a link between tracks a human already cut apart, joined or
    # deleted — a generator that argues with decisions already made is worse
    # than one that stays quiet
    hidden = hidden_filter(ctx.host, cid)
    props = []
    for si, st in enumerate(streams):
        ctx.check()
        objs = [dict(r) for r in ctx.db.all(
            "SELECT id, key, first_frame, last_frame FROM objects "
            "WHERE layer_id=? AND stream_id=? ORDER BY first_frame", lid, st["id"])
            if not hidden(st["key"], r["key"], lid)]
        edge = {}
        for o in objs:
            for which, frame in (("first", o["first_frame"]), ("last", o["last_frame"])):
                r = ctx.db.one("SELECT payload FROM shapes WHERE object_id=? AND frame=?",
                               o["id"], frame)
                if r:
                    edge[(o["id"], which)] = json.loads(r["payload"]).get("box")
        for i, a in enumerate(objs):
            for b in objs[i + 1:]:
                gap = b["first_frame"] - a["last_frame"]
                if gap <= 0:
                    continue
                if gap > max_gap:
                    break                      # objs is sorted by first_frame
                ba, bb = edge.get((a["id"], "last")), edge.get((b["id"], "first"))
                if not ba or not bb:
                    continue
                ca = (ba[0] + ba[2] / 2, ba[1] + ba[3] / 2)
                cb = (bb[0] + bb[2] / 2, bb[1] + bb[3] / 2)
                w = max(ba[2], bb[2], 1)
                move = ((ca[0] - cb[0]) ** 2 + (ca[1] - cb[1]) ** 2) ** 0.5 / w
                if move > max_move:
                    continue
                area_a, area_b = ba[2] * ba[3], bb[2] * bb[3]
                size = min(area_a, area_b) / max(area_a, area_b, 1)
                conf = max(0.0, min(1.0, (1 - gap / max_gap) * 0.5
                                    + (1 - move / max_move) * 0.35 + size * 0.15))
                props.append({
                    "kind": "identity.link",
                    "payload": {"items": [[st["key"], a["key"]], [st["key"], b["key"]]]},
                    "title": f"{st['key']}: link {a['key']} → {b['key']}",
                    "confidence": round(conf, 3),
                    "support": {"gap_frames": int(gap), "drift_box_widths": round(move, 2),
                                "size_ratio": round(size, 2), "stream": st["key"]},
                    "focus": {"stream_frame": int(b["first_frame"]), "stream": st["key"],
                              "objects": [a["id"], b["id"]]},
                    "note": "same camera, short gap, the box barely moved",
                })
        ctx.progress((si + 1) / len(streams), f"{st['key']}: {len(props)} candidate(s) so far")

    proposals = ctx.host.plugins.get("proposals")
    if proposals is None:
        raise RuntimeError("the proposals plugin is not installed, so there is nowhere to put these")
    added = sys.modules[proposals.module_name].add(ctx.host, cid, "identity.link_gaps", props)
    return {"found": len(props), "added": added,
            "message": f"{added} new proposal(s) from {len(props)} candidate link(s)"}
