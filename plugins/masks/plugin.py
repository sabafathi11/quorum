"""masks — editing the tracks a model drew.

The half of `sync_id_ui` that Quorum was missing. Six structural edits, all of
them ops:

    masks.delete / restore   hide a track without losing it
    masks.purge  / unpurge   take it out of the working set entirely
    masks.split              cut one track in two at a frame
    masks.join   / unjoin    treat several tracks as one thing
    masks.keyframe           replace one keyframe's mask (what SAM writes)
    masks.keyframes          the same, in bulk — one propagation is one op
    masks.create             a track that did not exist before

Nothing here mutates the imported layer. The fold produces a *projection* —
which keys are hidden, where the cuts are, which keys travel together — and a
materialiser turns the projection into a second, small layer holding only the
tracks an edit actually changed. Everything untouched keeps being served from
the import, so a capture with three splits costs three tracks of storage, not
another 387 MB.

The derived keys are the ones the desktop tool already used and wrote into
`consistent_ids_<stamp>.json`: a segment cut at frame 2470 out of track 86 is
`86@2470`, and tracks 1, 2 and 3 joined are `1+2+3`. That is not a coincidence —
it is what lets `import:consistent_ids` replay edits made in the old tool, and
what makes the 44 identity assignments the importer had to leave dangling
resolve to real tracks.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import Body, Depends, HTTPException, Request

from quorum.api.deps import current_user
from quorum.sdk import Plugin, Tool, Writer

PLUGIN = Plugin(
    id="masks",
    name="Mask editing",
    description="Delete, split, join and repair the tracks a model drew. Every edit is an op.",
    web="masks.js",
)

PLUGIN.tool(Tool(id="edit", title="Edit", icon="✎", order=20,
                 needs_layer_types=["mask.rle"],
                 description="Fix what the model got wrong: hide, cut, merge, repaint."))

DERIVED_KEY = "edits"          # the layer key this plugin materialises into


# --------------------------------------------------------------------- codec
def _mask_mod():
    """mask_layer owns the RLE codec; borrowing it beats a second copy."""
    import importlib.util
    p = Path(__file__).resolve().parent.parent / "mask_layer" / "plugin.py"
    spec = importlib.util.spec_from_file_location("_mask_codec", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_CODEC = None


def codec():
    global _CODEC
    if _CODEC is None:
        _CODEC = _mask_mod()
    return _CODEC


# ---------------------------------------------------------------------- keys
def orig_of(key: str) -> str:
    """`86@2470` came out of `86`; `1+2+3` has no single origin."""
    return str(key).split("@", 1)[0]


def members_of(key: str) -> list[str]:
    return str(key).split("+") if "+" in str(key) else [str(key)]


def join_key(keys) -> str:
    """Plain lexicographic sort, deliberately: `11+12+21+23+6+7`, not
    `6+7+11+12+21+23`. It looks wrong and it is not — this is the key the
    desktop tool wrote into every `consistent_ids_<stamp>.json` on disk, and a
    tidier ordering would silently orphan every identity assigned to a join."""
    return "+".join(sorted(set(str(k) for k in keys)))


def _sortkey(k: str):
    """Sort `2` before `10` before `10@55`, so a join key is stable."""
    base, _, at = str(k).partition("@")
    try:
        b = (0, int(base))
    except ValueError:
        b = (1, base)
    return (b, int(at) if at.isdigit() else 0)


# --------------------------------------------------------------------- fold
class Projection:
    """What the ops add up to. Pure data; the materialiser does the work."""

    def __init__(self):
        self.deleted: dict[str, set] = {}
        self.purged: dict[str, set] = {}
        self.splits: dict[str, dict[str, set]] = {}      # stream -> orig -> frames
        self.joins: dict[str, list[set]] = {}            # stream -> disjoint groups
        self.edits: dict[str, dict[str, dict]] = {}      # stream -> key -> frame -> payload|None
        self.created: dict[str, dict[str, dict]] = {}    # stream -> key -> track dict
        self.labels: dict[str, dict[str, str]] = {}

    # -- joins are kept disjoint, exactly as the desktop tool's normalize() did
    def add_join(self, stream: str, keys) -> None:
        keys = set(str(k) for k in keys)
        if len(keys) < 2:
            return
        groups = self.joins.setdefault(stream, [])
        merged = set(keys)
        rest = []
        for g in groups:
            (merged.update(g) if g & merged else rest.append(g))
        rest.append(merged)
        self.joins[stream] = rest

    def unjoin(self, stream: str, key: str) -> None:
        want = set(members_of(key))
        self.joins[stream] = [g for g in self.joins.get(stream, []) if not (g & want)]

    def group_of(self, stream: str, key: str) -> set | None:
        for g in self.joins.get(stream, []):
            if str(key) in g:
                return g
        return None

    def as_dict(self) -> dict:
        return {
            "deleted": {s: sorted(v, key=_sortkey) for s, v in self.deleted.items() if v},
            "purged": {s: sorted(v, key=_sortkey) for s, v in self.purged.items() if v},
            "splits": {s: {k: sorted(f) for k, f in d.items() if f}
                       for s, d in self.splits.items() if d},
            "joins": {s: [sorted(g, key=_sortkey) for g in gs] for s, gs in self.joins.items() if gs},
            "edited": {s: sorted(d, key=_sortkey) for s, d in self.edits.items() if d},
            "created": {s: sorted(d, key=_sortkey) for s, d in self.created.items() if d},
        }


def fold(ops: list[dict]) -> Projection:
    """Op log -> Projection. Pure, and the only place edit semantics live."""
    p = Projection()
    for op in ops:
        kind = op["kind"]
        if not kind.startswith("masks."):
            continue
        d = op.get("payload") or {}
        s = str(d.get("stream", ""))
        keys = [str(k) for k in (d.get("keys") or ([d["key"]] if d.get("key") is not None else []))]
        what = kind.split(".", 1)[1]
        if what == "delete":
            p.deleted.setdefault(s, set()).update(keys)
        elif what == "restore":
            p.deleted.setdefault(s, set()).difference_update(keys)
        elif what == "purge":
            p.purged.setdefault(s, set()).update(keys)
            p.deleted.setdefault(s, set()).difference_update(keys)
        elif what == "unpurge":
            p.purged.setdefault(s, set()).difference_update(keys)
        elif what == "split":
            for k in keys:
                p.splits.setdefault(s, {}).setdefault(orig_of(k), set()).add(int(d["frame"]))
        elif what == "unsplit":
            for k in keys:
                p.splits.get(s, {}).get(orig_of(k), set()).discard(int(d["frame"]))
        elif what == "join":
            p.add_join(s, keys)
        elif what == "unjoin":
            for k in keys:
                p.unjoin(s, k)
        elif what == "keyframe":
            for k in keys:
                p.edits.setdefault(s, {}).setdefault(k, {})[int(d["frame"])] = d.get("payload")
        elif what == "keyframes":
            # The same edit, in bulk. It exists because a SAM propagation is
            # *one* decision — "carry this forward" — and sixty separate ops
            # would each be individually undoable and collectively unusable.
            for k in keys:
                into = p.edits.setdefault(s, {}).setdefault(k, {})
                for item in (d.get("frames") or []):
                    into[int(item["frame"])] = item.get("payload")
        elif what == "create":
            for k in keys:
                p.created.setdefault(s, {})[k] = {
                    "label": d.get("label", ""), "frames": d.get("frames") or []}
        if d.get("label"):
            for k in keys:
                p.labels.setdefault(s, {})[k] = d["label"]
    for s in list(p.joins):
        p.joins[s] = [g for g in p.joins[s] if len(g) > 1]
    return p


# ------------------------------------------------------------------ segments
def build_segments(track: dict, split_frames) -> list[dict]:
    """One track dict -> segment dicts, cut at `split_frames` (stream frames).

    The segment starting at the natural beginning keeps the original key; one
    beginning at cut `s` is `orig@s`. Each later segment gets a synthetic start
    keyframe holding whatever mask was showing at `s`, and each earlier one gets
    a terminating `outside` keyframe there, so a cut is visible exactly where it
    was made rather than a frame either side of it.
    """
    key = str(track["key"])
    frames = track["frames"]
    cuts = sorted(set(int(f) for f in split_frames))
    bounds = [None] + cuts
    segs = []
    for j, lo in enumerate(bounds):
        hi = bounds[j + 1] if j + 1 < len(bounds) else None
        sf: list = []
        if lo is not None:
            i = _recent(frames, lo)
            if i is not None and not track["outside"][i] and frames[i] < lo:
                sf.append({"frame": lo, "outside": 0, "payload": track["payload"][i]})
        for i, f in enumerate(frames):
            if (lo is None or f >= lo) and (hi is None or f < hi):
                sf.append({"frame": f, "outside": track["outside"][i], "payload": track["payload"][i]})
        if hi is not None and sf and not sf[-1]["outside"]:
            sf.append({"frame": hi, "outside": 1, "payload": sf[-1]["payload"]})
        if not sf:
            continue
        segs.append({"key": key if lo is None else f"{key}@{lo}", "orig": key,
                     "label": track.get("label", ""), "frames": sf})
    return segs


def _recent(frames: list[int], f: int):
    i = None
    for j, ff in enumerate(frames):
        if ff <= f:
            i = j
        else:
            break
    return i


def combine(members: list[dict]) -> dict:
    """Union several segments into one track: at every keyframe any member has,
    the union of whatever is showing. This is what "these are one person" means
    when two boxes each hold half of them."""
    import numpy as np
    C = codec()

    prep = []
    for m in members:
        fr = [k["frame"] for k in m["frames"]]
        prep.append({"fr": fr, "kf": m["frames"], "dec": {}})

    def active(mem, f):
        i = _recent(mem["fr"], f)
        if i is None or mem["kf"][i]["outside"]:
            return None
        got = mem["dec"].get(i)
        if got is None:
            pay = mem["kf"][i]["payload"]
            l, t, w, h = pay["box"]
            got = ((l, t, w, h), C.decode_rle(pay.get("rle", ""), w, h))
            mem["dec"][i] = got
        return got

    frames = sorted({k["frame"] for m in members for k in m["frames"]})
    out = []
    prev = {"box": [0, 0, 1, 1], "rle": "1"}
    for f in frames:
        present = [a for a in (active(m, f) for m in prep) if a is not None]
        if not present:
            out.append({"frame": f, "outside": 1, "payload": prev})
            continue
        L = min(b[0] for b, _ in present); T = min(b[1] for b, _ in present)
        R = max(b[0] + b[2] for b, _ in present); B = max(b[1] + b[3] for b, _ in present)
        canvas = np.zeros((B - T, R - L), dtype=bool)
        for (l, t, w, h), mk in present:
            canvas[t - T:t - T + h, l - L:l - L + w] |= mk
        ys, xs = np.nonzero(canvas)
        y0, y1, x0, x1 = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())
        sub = canvas[y0:y1 + 1, x0:x1 + 1]
        prev = {"box": [int(L + x0), int(T + y0), int(x1 - x0 + 1), int(y1 - y0 + 1)],
                "rle": C.encode_rle(sub)}
        out.append({"frame": f, "outside": 0, "payload": prev})
    return {"key": join_key(m["key"] for m in members), "orig": "",
            "label": members[0].get("label", ""), "frames": out,
            "members": sorted((m["key"] for m in members), key=_sortkey)}


# ------------------------------------------------------------- materialising
def source_layer(db, capture_id: int) -> dict | None:
    """The imported mask layer — the one this plugin reads and never writes."""
    for r in db.all("SELECT * FROM layers WHERE capture_id=? ORDER BY id", capture_id):
        if r["type"] == "mask.rle" and r["key"] != DERIVED_KEY:
            return dict(r)
    return None


def derived_layer(db, capture_id: int) -> dict | None:
    r = db.one("SELECT * FROM layers WHERE capture_id=? AND key=?", capture_id, DERIVED_KEY)
    return dict(r) if r else None


def _raw_track(db, layer_id: int, stream_id: int, key: str) -> dict | None:
    o = db.one("SELECT * FROM objects WHERE layer_id=? AND stream_id=? AND key=?",
               layer_id, stream_id, key)
    if o is None:
        return None
    rows = db.all("SELECT frame, outside, payload FROM shapes WHERE object_id=? ORDER BY frame",
                  o["id"])
    return {"key": key, "label": o["label"],
            "frames": [r["frame"] for r in rows],
            "outside": [r["outside"] for r in rows],
            "payload": [json.loads(r["payload"]) for r in rows]}


def touched(proj: Projection, stream: str) -> set:
    """Original keys whose effective shape differs from what was imported."""
    out = set(proj.splits.get(stream, {}))
    out |= set(proj.edits.get(stream, {}))
    for g in proj.joins.get(stream, []):
        for k in g:
            out.add(orig_of(k))
    return {k for k in out if k}


def materialise(host, capture_id: int, ctx=None) -> dict:
    """Rebuild the derived layer from the imported one plus the projection.

    Always a full rebuild for the streams that have edits: it is cheap because
    only edited tracks are ever written, and a rebuild-from-log is the only
    honest way to make undo work on structural edits.
    """
    db = host.db
    src = source_layer(db, capture_id)
    proj = fold(host.ops_since(capture_id, 0))
    # A capture with nothing imported is not an error any more: `create` draws
    # a track from scratch (the SAM interactor does exactly that), and the
    # derived layer is then the only mask layer there is. Everything else here
    # reads the imported one, so every read of `src` below is guarded.
    if src is None and not any(proj.created.values()):
        return {"tracks": 0, "message": "no imported mask layer to edit"}
    plugin = host.plugins["masks"]
    w = Writer(host, plugin)
    lay = w.layer(capture_id, key=DERIVED_KEY, name="Edits", type="mask.rle",
                  provenance="human",
                  config={"source_layer": src["id"] if src else None})

    streams = {r["key"]: dict(r) for r in
               db.all("SELECT * FROM streams WHERE capture_id=?", capture_id)}
    total = 0
    shadow: dict[str, list] = {}
    for skey, st in streams.items():
        purged = proj.purged.get(skey, set())
        universe: dict[str, dict] = {}
        for key in sorted(touched(proj, skey), key=_sortkey):
            raw = _raw_track(db, src["id"], st["id"], key) if src else None
            if raw is None:
                continue
            ed = proj.edits.get(skey, {}).get(key) or {}
            if ed:
                raw = _apply_edits(raw, ed)
            for seg in build_segments(raw, proj.splits.get(skey, {}).get(key, ())):
                universe[seg["key"]] = seg
        for key, made in (proj.created.get(skey) or {}).items():
            universe[key] = {"key": key, "orig": "", "label": made.get("label", ""),
                             "frames": made["frames"]}
        # a join may name a track nobody split or edited: pull it in whole
        for g in proj.joins.get(skey, []):
            for k in g:
                if k in universe:
                    continue
                raw = _raw_track(db, src["id"], st["id"], k) if src else None
                if raw is None:
                    continue
                universe[k] = {"key": k, "orig": k, "label": raw["label"],
                               "frames": [{"frame": f, "outside": o, "payload": p} for f, o, p
                                          in zip(raw["frames"], raw["outside"], raw["payload"])]}

        consumed: set = set()
        items = []
        for g in proj.joins.get(skey, []):
            mem = [universe[k] for k in sorted(g, key=_sortkey) if k in universe]
            if len(mem) < 2:
                continue
            consumed |= {m["key"] for m in mem}
            j = combine(mem)
            if j["key"] in purged:
                continue
            items.append({"key": j["key"], "label": j["label"], "frames": j["frames"],
                          "meta": {"derived": "join", "members": j["members"]}})
            if ctx:
                ctx.check()
        for key, seg in universe.items():
            if key in consumed or key in purged:
                continue
            items.append({"key": key, "label": seg["label"], "frames": seg["frames"],
                          "meta": {"derived": "split" if "@" in key or seg.get("orig") else "new",
                                   "orig": seg.get("orig", "")}})

        # A keyframe edit aimed at a *derived* key (repainting one half of a
        # split, or a join) lands here, after the thing it names exists.
        raw_keys = {r["key"] for r in db.all(
            "SELECT key FROM objects WHERE layer_id=? AND stream_id=?",
            src["id"], st["id"])} if src else set()
        for key, ed in (proj.edits.get(skey) or {}).items():
            if key in raw_keys:
                continue                      # already folded in before the cut
            for it in items:
                if it["key"] != key:
                    continue
                kfs = {int(k["frame"]): k for k in it["frames"]}
                for f, payload in ed.items():
                    if payload is None:
                        kfs.pop(int(f), None)
                    else:
                        kfs[int(f)] = {"frame": int(f), "outside": int(payload.get("outside", 0)),
                                       "payload": {k: v for k, v in payload.items() if k != "outside"}}
                it["frames"] = [kfs[f] for f in sorted(kfs)]
        # everything this stream now serves from the derived layer instead
        hide = set(touched(proj, skey)) | purged | consumed
        hide |= {orig_of(k) for k in consumed}
        shadow[skey] = sorted(hide, key=_sortkey)

        db.run("DELETE FROM objects WHERE layer_id=? AND stream_id=?", lay, st["id"])
        if items:
            w.objects(lay, st["id"], items)
        total += len(items)
        if ctx:
            ctx.progress(0.1 + 0.85 * (list(streams).index(skey) + 1) / len(streams),
                         f"{skey}: {len(items)} edited track(s)")

    host.ctx("masks").doc_put(capture_id, "shadow", {"streams": shadow, "layer": lay})
    # the identity walk caches visibility intervals off the shape table
    ident = host.plugins.get("identity")
    if ident is not None:
        sys.modules[ident.module_name].forget_intervals(capture_id)
    host.publish(capture_id, {"t": "masks", "capture_id": capture_id, "tracks": total})
    return {"layer_id": lay, "tracks": total, "shadow": shadow,
            "message": f"{total} edited track(s)"}


def _apply_edits(raw: dict, edits: dict) -> dict:
    """Keyframe overrides folded into a raw track. `None` drops a keyframe."""
    kfs = {int(f): {"outside": o, "payload": p}
           for f, o, p in zip(raw["frames"], raw["outside"], raw["payload"])}
    for f, payload in edits.items():
        if payload is None:
            kfs.pop(int(f), None)
        else:
            kfs[int(f)] = {"outside": int(payload.get("outside", 0)),
                           "payload": {k: v for k, v in payload.items() if k != "outside"}}
    order = sorted(kfs)
    return {"key": raw["key"], "label": raw["label"], "frames": order,
            "outside": [kfs[f]["outside"] for f in order],
            "payload": [kfs[f]["payload"] for f in order]}


# ------------------------------------------------------------------ effective
def effective_objects(host, capture_id: int, include_deleted: bool = False) -> list[dict]:
    """The tracks that actually exist right now, across every mask layer.

    A key can live in two layers at once — the first half of a cut track keeps
    its original key — so "all the objects in the mask layer" is the wrong set
    for anything that leaves this server. Exporters call this; so does anything
    that must not offer a track a human already deleted.

    Returns rows of {object_id, layer_id, stream, key, label}.
    """
    proj = fold(host.ops_since(capture_id, 0))
    doc, _ = host.ctx("masks").doc_get(capture_id, "shadow", default={"streams": {}})
    shadow = {s: set(v) for s, v in (doc or {}).get("streams", {}).items()}
    src = source_layer(host.db, capture_id)
    out = []
    for r in host.db.all(
            "SELECT o.id, o.layer_id, o.key, o.label, s.key AS stream FROM objects o "
            "JOIN streams s ON s.id=o.stream_id JOIN layers l ON l.id=o.layer_id "
            "WHERE l.capture_id=? AND l.type='mask.rle' ORDER BY l.id, s.idx, o.id", capture_id):
        stream, key = r["stream"], str(r["key"])
        if key in proj.purged.get(stream, set()):
            continue
        if not include_deleted and key in proj.deleted.get(stream, set()):
            continue
        # the imported copy of a key the edits replaced is not a track any more
        if src and r["layer_id"] == src["id"] and key in shadow.get(stream, set()):
            continue
        out.append({"object_id": r["id"], "layer_id": r["layer_id"], "stream": stream,
                    "key": key, "label": r["label"]})
    return out


@PLUGIN.route.get("/{capture_id}/effective")
def get_effective(capture_id: int, request: Request, include_deleted: bool = False,
                  user: dict = Depends(current_user)):
    rows = effective_objects(request.app.state.host, capture_id, include_deleted)
    return {"objects": rows, "count": len(rows)}


# ---------------------------------------------------------------------- http
def state_for(host, capture_id: int) -> dict:
    proj = fold(host.ops_since(capture_id, 0))
    src = source_layer(host.db, capture_id)
    der = derived_layer(host.db, capture_id)
    shadow, _ = host.ctx("masks").doc_get(capture_id, "shadow", default={"streams": {}})
    return {**proj.as_dict(),
            "sourceLayer": src["id"] if src else None,
            "derivedLayer": der["id"] if der else None,
            "shadow": (shadow or {}).get("streams", {})}


@PLUGIN.route.get("/{capture_id}/state")
def get_state(capture_id: int, request: Request, user: dict = Depends(current_user)):
    return state_for(request.app.state.host, capture_id)


@PLUGIN.route.post("/{capture_id}/op")
def post_edit(capture_id: int, request: Request, body: dict = Body(...),
              user: dict = Depends(current_user)):
    """Structural edits come through here rather than the generic op endpoint so
    they can be refused with a reason. Identity ops commute; these do not — two
    people splitting the same track at different frames is not a merge, it is a
    question, and answering it silently is how an annotation tool loses trust."""
    host = request.app.state.host
    what = str(body.get("kind", ""))
    kind = what if what.startswith("masks.") else f"masks.{what}"
    payload = body.get("payload") or {}
    proj = fold(host.ops_since(capture_id, 0))
    stream = str(payload.get("stream", ""))
    keys = [str(k) for k in (payload.get("keys") or
                             ([payload["key"]] if payload.get("key") is not None else []))]
    verb = kind.split(".", 1)[1]

    if verb in ("split", "join", "unjoin", "keyframe", "keyframes") and not keys:
        raise HTTPException(400, f"{verb} needs at least one track")
    if verb == "split":
        for k in keys:
            if k in proj.purged.get(stream, set()):
                raise HTTPException(409, f"{k} was purged; restore it before cutting it")
            if proj.group_of(stream, k):
                raise HTTPException(
                    409, f"{k} is joined to {join_key(proj.group_of(stream, k))} — "
                         "unjoin it first, or the cut would have to fall inside a union")
    if verb == "join":
        if len({orig_of(k) for k in keys}) < 2 and len(keys) < 2:
            raise HTTPException(400, "joining needs two different tracks")
        for k in keys:
            g = proj.group_of(stream, k)
            if g and not set(keys) <= g:
                raise HTTPException(
                    409, f"{k} is already joined to {join_key(g)}; joining these would "
                         "merge two groups — unjoin one of them if that is what you mean")

    op = host.append_op(capture_id, kind, payload, user["name"], body.get("layer_id"))
    result = {}
    if verb in ("split", "join", "unjoin", "unsplit", "keyframe", "keyframes", "create",
                "purge", "unpurge"):
        result = materialise(host, capture_id)
    return {"op": op, "state": state_for(host, capture_id), "materialised": result}


@PLUGIN.route.post("/{capture_id}/materialise")
def remake(capture_id: int, request: Request, user: dict = Depends(current_user)):
    return materialise(request.app.state.host, capture_id)


# ------------------------------------------------------------------- import
@PLUGIN.importer(
    "consistent_ids", title="Import edits from consistent_ids.json",
    params={"capture_id": {"type": "capture", "required": True},
            "path": {"type": "string", "label": "Path to consistent_ids_<stamp>.json",
                     "description": "Blank looks beside sync_id_ui/ for this capture's stamp."}})
def import_edits(ctx):
    """The structural half of the file `identity` already imports the labels
    from: which tracks were deleted, where they were cut, and which were joined.

    Replaying these is what makes the identity assignments on keys like
    `86@2470` and `1+2+3` point at real tracks instead of dangling.
    """
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
    name = Path(path).name
    n = 0
    ctx.progress(0.05, "deletions")
    for stream, keys in (doc.get("deleted") or {}).items():
        if keys:
            ctx.emit_op(cid, "delete", {"stream": stream, "keys": [str(k) for k in keys],
                                        "imported_from": name})
            n += len(keys)
    for stream, keys in (doc.get("purged") or {}).items():
        if keys:
            ctx.emit_op(cid, "purge", {"stream": stream, "keys": [str(k) for k in keys],
                                       "imported_from": name})
            n += len(keys)
    ctx.progress(0.15, "cuts")
    for stream, per_key in (doc.get("splits") or {}).items():
        for key, frames in per_key.items():
            for f in frames:
                ctx.emit_op(cid, "split", {"stream": stream, "key": str(key), "frame": int(f),
                                           "imported_from": name})
                n += 1
    ctx.progress(0.25, "joins")
    for stream, groups in (doc.get("joins") or {}).items():
        for g in groups:
            if len(g) > 1:
                ctx.emit_op(cid, "join", {"stream": stream, "keys": [str(k) for k in g],
                                          "imported_from": name})
                n += 1
    ctx.progress(0.3, "rebuilding the edited tracks")
    out = materialise(ctx.host, cid, ctx)
    return {"ops": n, **out,
            "message": f"{n} edit(s) from {name} → {out.get('tracks', 0)} derived track(s)"}


@PLUGIN.job("materialise", title="Rebuild edited tracks",
            params={"capture_id": {"type": "capture", "required": True}},
            description="Replay the edit log onto the imported masks. Safe to run any time.")
def job_materialise(ctx):
    return materialise(ctx.host, int(ctx.params["capture_id"]), ctx)


@PLUGIN.job("orphans", title="Identity keys with no track",
            params={"capture_id": {"type": "capture", "required": True}},
            description="Which identity assignments point at a track that does not exist.")
def orphans(ctx):
    """The measurement the handoff asked for: after replaying the edits, how
    many of the imported identity assignments still dangle?"""
    cid = int(ctx.params["capture_id"])
    ident = ctx.host.plugins.get("identity")
    if ident is None:
        raise RuntimeError("the identity plugin is not installed")
    assign = sys.modules[ident.module_name].state_for(ctx.host, cid)["assignments"]
    have: dict[str, set] = {}
    for r in ctx.db.all(
            "SELECT s.key AS stream, o.key AS okey FROM objects o "
            "JOIN streams s ON s.id=o.stream_id JOIN layers l ON l.id=o.layer_id "
            "WHERE l.capture_id=? AND l.type='mask.rle'", cid):
        have.setdefault(r["stream"], set()).add(str(r["okey"]))
    missing = []
    total = 0
    for stream, m in assign.items():
        for key in m:
            total += 1
            if str(key) not in have.get(stream, set()):
                missing.append(f"{stream}/{key}")
    return {"assignments": total, "orphans": len(missing), "examples": sorted(missing)[:20],
            "message": f"{len(missing)} of {total} identity assignments have no track"}
