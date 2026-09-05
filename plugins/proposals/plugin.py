"""proposals — a queue of machine suggestions a human accepts or rejects.

Any plugin can put proposals here; nothing else in the system may write an
annotation on a machine's say-so. A proposal is a *pre-built op* plus the
evidence for it, so accepting one is exactly the edit the annotator would have
made by hand, attributed to them, in the same log.

    {"id": "...", "kind": "identity.assign", "payload": {...},
     "title": "merge cam4:88 into id 11",
     "confidence": 0.86, "support": {...}, "focus": {"frame": 4412, "objects": [...]},
     "state": "open" | "accepted" | "rejected"}

Storage is the per-plugin document store, which is the honest fit at this size:
a capture's queue is hundreds of rows, read whole, written rarely. When a queue
outgrows that it wants its own table, and that is a change to this plugin only.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time

from fastapi import Body, Depends, HTTPException, Request

from quorum.api.deps import current_user
from quorum.filters import ObjectFilter
from quorum.sdk import Conflict, Plugin, Tool

PLUGIN = Plugin(
    id="proposals",
    name="Proposals",
    description="Review queue for machine-generated suggestions. Generators write here; only humans accept.",
    web="proposals.js",
)

PLUGIN.tool(Tool(id="review", title="Review", icon="✓", order=30,
                 description="Work through machine proposals with their evidence."))

KEY = "queue"


# ------------------------------------------------------------ generator API
def add(host, capture_id: int, source: str, items: list[dict]) -> int:
    """Called by other plugins (see identity.link_gaps). Idempotent on id."""
    ctx = host.ctx("proposals")
    queue, ver = ctx.doc_get(capture_id, KEY, default={"items": []})
    queue = queue or {"items": []}
    have = {p["id"] for p in queue["items"]}
    added = 0
    for it in items:
        pid = it.get("id") or hashlib.sha1(
            json.dumps([source, it.get("kind"), it.get("payload")], sort_keys=True).encode()
        ).hexdigest()[:12]
        if pid in have:
            continue
        queue["items"].append({
            "id": pid, "source": source, "state": "open", "created": time.time(),
            "kind": it["kind"], "payload": it.get("payload") or {},
            "title": it.get("title") or it["kind"],
            "confidence": float(it.get("confidence", 0.0)),
            "support": it.get("support") or {},
            "focus": it.get("focus") or {},
            "note": it.get("note", ""),
        })
        added += 1
    queue["items"].sort(key=lambda p: -p["confidence"])
    ctx.doc_put(capture_id, KEY, queue, ver)
    host.publish(capture_id, {"t": "proposals", "capture_id": capture_id, "added": added})
    return added


def _queue(ctx, cid):
    q, ver = ctx.doc_get(cid, KEY, default={"items": []})
    return (q or {"items": []}), ver


# --------------------------------------------------------------------- http
def _ctx(request: Request, user):
    return request.app.state.host.ctx("proposals", user["name"])


def _focus_gone(host, capture_id: int):
    """Mask object ids a proposal may still name but nobody draws any more.

    A proposal carries two references to the same track and they age
    differently. Its *payload* names `[stream, key]` — that is what accepting
    it appends, and a key survives being cut or edited. Its *focus* names
    object ids, and object ids do not survive: `materialise` rebuilds the
    derived layer by deleting and rewriting its objects, and the imported copy
    of an edited track stops being drawn altogether.

    So a generator run against the imported layer can leave a proposal whose
    claim is still perfectly valid and whose evidence points at a copy that has
    been replaced. Offering it means seeking to a frame and ringing nothing,
    which reads as the queue being broken.

    Returning ids rather than repointing them is deliberate. The obvious fix —
    rewrite the focus to the current drawn copy — hands the client an id that
    is correct only until the next `materialise`, and the client is holding
    objects it loaded when the capture was opened. That trades a visible stale
    pointer for an invisible one. Staling the proposal says the true thing: the
    evidence is about a copy that no longer exists, and the generator should be
    re-run.

    The coupling to `masks` is guarded and optional, exactly as `identity`'s is:
    a layer type with no notion of an effective set stales nothing.
    """
    masks = host.plugins.get("masks")
    if masks is None:
        return lambda oid: False
    try:
        rows = sys.modules[masks.module_name].effective_objects(host, capture_id)
    except Exception:
        return lambda oid: False
    drawn = {r["object_id"] for r in rows}
    # Only mask objects are judged. An object id belonging to some other layer
    # type is none of this function's business, and staling it would be a
    # plugin quietly deciding for a plugin it knows nothing about.
    mine = {r["id"] for r in host.db.all(
        "SELECT o.id FROM objects o JOIN layers l ON l.id=o.layer_id "
        "WHERE l.capture_id=? AND l.type='mask.rle'", capture_id)}
    # Two ways a focus goes bad, and the second is the one that hides.
    # `mine - drawn` is a copy that still exists but has been superseded. A
    # *dangling* id is worse: `materialise` rebuilds the derived layer by
    # deleting its objects and writing new ones, so an id recorded before an
    # edit may name a row that no longer exists at all. It is in neither set,
    # and checking only for "superseded" would let it through forever.
    known = {r["id"] for r in host.db.all(
        "SELECT o.id FROM objects o JOIN layers l ON l.id=o.layer_id "
        "WHERE l.capture_id=?", capture_id)}
    replaced = mine - drawn
    return lambda oid: oid in replaced or oid not in known


def _mark_stale(host, capture_id: int, items: list[dict], focus_gone=None) -> int:
    """Ask each op's owner whether it still applies.

    A proposal is a pre-built op waiting for a human, and the world moves under
    it: a suggestion to link two tracks stops meaning anything once one of them
    has been joined into something else. Rather than delete those — a rejected
    generator still deserves to be scored — they are flagged, kept, and left out
    of the open queue.
    """
    n = 0
    cache: dict[str, str | None] = {}
    for p in items:
        if p["state"] not in ("open", "stale"):
            continue
        key = json.dumps([p["kind"], p["payload"]], sort_keys=True)
        if key not in cache:
            cache[key] = host.validate_op(capture_id, p["kind"], p["payload"])
        reason = cache[key]
        # …and even when the op still applies, the evidence may not.
        if not reason and focus_gone is not None:
            hit = [o for o in (p.get("focus") or {}).get("objects") or [] if focus_gone(o)]
            if hit:
                reason = ("its evidence points at a copy of that track which has been "
                          "replaced by an edit — re-run the generator")
        if reason and p["state"] == "open":
            p["state"] = "stale"
            p["stale_reason"] = reason
            n += 1
        elif not reason and p["state"] == "stale":
            p["state"] = "open"          # an undo can bring one back to life
            p.pop("stale_reason", None)
            n += 1
    return n


def _hidden_objects(host, capture_id: int, want: ObjectFilter) -> set:
    where, args = want.sql("o", "l", "s")
    rows = host.db.all(
        "SELECT o.id FROM objects o JOIN layers l ON l.id=o.layer_id "
        "JOIN streams s ON s.id=o.stream_id "
        f"WHERE l.capture_id=? AND NOT ({where})", capture_id, *args)
    return {r["id"] for r in rows}


@PLUGIN.route.get("/{capture_id}")
def list_proposals(capture_id: int, request: Request, state: str = "", filter: str = "",
                   user: dict = Depends(current_user)):
    """`filter` is the reviewer's object filter. This queue *offers objects to a
    human* — it seeks to a frame and selects what a proposal names — so a
    proposal about a class they are hiding is not something to put in front of
    them. It is held back rather than dropped: the generator is still scored on
    it, and unhiding the class brings it straight back."""
    host = request.app.state.host
    ctx = _ctx(request, user)
    q, ver = _queue(ctx, capture_id)
    changed = _mark_stale(host, capture_id, q["items"],
                          _focus_gone(host, capture_id))
    if changed:
        try:
            ctx.doc_put(capture_id, KEY, q, ver)
            ver += 1
        except Conflict:
            pass                          # somebody else wrote first; theirs is as good
    items = [p for p in q["items"] if not state or p["state"] == state]
    want = ObjectFilter.parse(filter)
    withheld = 0
    if not want.empty:
        hidden = _hidden_objects(host, capture_id, want)
        keep = []
        for p in items:
            named = set(p.get("focus", {}).get("objects") or [])
            if named and named <= hidden:
                withheld += 1
                continue
            keep.append(p)
        items = keep
    counts = {}
    for p in q["items"]:
        counts[p["state"]] = counts.get(p["state"], 0) + 1
    return {"items": items, "counts": counts, "version": ver, "restaled": changed,
            "withheld": withheld}


@PLUGIN.route.post("/{capture_id}/{pid}/{decision}")
def decide(capture_id: int, pid: str, decision: str, request: Request,
           body: dict = Body(default={}), user: dict = Depends(current_user)):
    """Accept: the proposal's own op is appended to the log **as this user**.
    Reject: recorded, kept, and filtered out — never deleted, so a generator can
    be scored against what humans actually did with it."""
    if decision not in ("accept", "reject", "reopen"):
        raise HTTPException(400, "decision must be accept, reject or reopen")
    host = request.app.state.host
    ctx = _ctx(request, user)
    q, ver = _queue(ctx, capture_id)
    prop = next((p for p in q["items"] if p["id"] == pid), None)
    if prop is None:
        raise HTTPException(404, f"no proposal {pid}")
    op = None
    if decision == "accept":
        if prop["state"] == "accepted":
            raise HTTPException(409, "that proposal was already accepted")
        # last word before it becomes a real edit
        reason = host.validate_op(capture_id, prop["kind"], prop["payload"])
        if reason:
            prop["state"] = "stale"
            prop["stale_reason"] = reason
            ctx.doc_put(capture_id, KEY, q, ver)
            raise HTTPException(409, f"this suggestion is out of date: {reason}")
        op = host.append_op(capture_id, prop["kind"], prop["payload"], user["name"])
        prop["state"] = "accepted"
    elif decision == "reject":
        prop["state"] = "rejected"
        prop["note"] = body.get("note", prop.get("note", ""))
    else:
        prop["state"] = "open"
    prop["decided_by"] = user["name"]
    prop["decided_at"] = time.time()
    ctx.doc_put(capture_id, KEY, q, ver)
    host.publish(capture_id, {"t": "proposals", "capture_id": capture_id, "decided": pid})
    return {"proposal": prop, "op": op}


@PLUGIN.route.delete("/{capture_id}")
def clear(capture_id: int, request: Request, source: str = "",
          user: dict = Depends(current_user)):
    ctx = _ctx(request, user)
    q, ver = _queue(ctx, capture_id)
    before = len(q["items"])
    q["items"] = [p for p in q["items"]
                  if (source and p["source"] != source) or p["state"] == "accepted"]
    ctx.doc_put(capture_id, KEY, q, ver)
    return {"removed": before - len(q["items"])}
