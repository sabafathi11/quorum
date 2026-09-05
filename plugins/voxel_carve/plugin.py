"""voxel_carve — the cross-camera identity generator, and what it refuses to do.

The carve links a person seen in one camera to the same person in another by
intersecting their silhouettes in space: fill the room with voxels, keep the
ones that land inside a mask in enough cameras, and see whose masks agree.
That needs a **solved calibration** — where each camera is, which way it points,
and what its lens does — and there is nothing sensible to do without one. A
carve on a guessed calibration does not produce worse links; it produces
confident nonsense, which is the failure mode this whole review-queue design
exists to avoid.

So this plugin is the worked example of the requirement mechanism:

  · it declares an asset kind (`voxel_carve.calibration`) — that declaration is
    the *entire* contact between this plugin and the upload machinery. Nothing
    in the upload path knows a calibration exists; it offers the kind because
    somebody declared it, the same way it will offer whatever the next plugin
    needs;
  · it declares a `Requirement`, so the core greys the tool out, says why, and
    — the part that matters — **refuses the job on the server**, for the UI, the
    CLI and another plugin alike;
  · it validates what was uploaded, so "you have a calibration" means one that
    parses and covers the cameras in this capture, not merely a file of the
    right name.

**The carve itself is not implemented.** It is `calib_ui/carve.py` plus a C
kernel, and porting it is TODO §5. `suggest` exists, is gated, and says so
plainly rather than returning an empty queue that looks like agreement.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from fastapi import Depends, HTTPException, Request

from quorum.api.deps import current_user
from quorum.sdk import AssetKind, Plugin, Requirement, Tool

PLUGIN = Plugin(
    id="voxel_carve",
    name="Voxel carve",
    description="Link people across cameras by intersecting their silhouettes in space.",
)

PLUGIN.tool(Tool(id="carve", title="Carve", icon="⬚", order=40,
                 needs_layer_types=["mask.rle"],
                 description="Cross-camera identity proposals from silhouette intersection."))

PLUGIN.asset_kind(AssetKind(
    id="calibration",
    title="Camera calibration",
    description="The solved calibration — camera poses, intrinsics and lens model. "
                "`camera_pics` writes one as calibration_result.json.",
    accept=[".json"],
    multiple=False,
    on_ready="validate",
    icon="◎"))

PLUGIN.requires(Requirement(
    asset_kind="calibration", at_least=1,
    why="the carve intersects camera frusta, and without a solved calibration there is "
        "no geometry to intersect — it would return confident nonsense rather than "
        "nothing",
    fix="Upload the solved calibration as “Camera calibration” on the capture's Data panel."))


# ---------------------------------------------------------------- the file
def parse(doc: dict) -> dict:
    """Read a calibration into the few facts anything here needs.

    Deliberately forgiving about shape and unforgiving about meaning: several
    generations of `camera_pics` wrote slightly different JSON, and a file that
    parses but whose rotation is not a rotation is the dangerous one.
    """
    cams_raw = doc.get("cameras")
    if isinstance(cams_raw, dict):
        items = list(cams_raw.items())
    elif isinstance(cams_raw, list):
        items = [(c.get("name") or c.get("id") or f"cam{i + 1}", c) for i, c in enumerate(cams_raw)]
    else:
        raise ValueError("no `cameras` in this file — is it a calibration_result.json?")

    model = str(doc.get("lens_model") or doc.get("model") or "").lower()
    cams = {}
    problems = []
    for name, c in items:
        key = str(name).lower().replace(" ", "")
        try:
            R = c.get("R_w2c") or c.get("R") or c.get("rotation")
            C = c.get("C") or c.get("center") or c.get("position")
            f = c.get("f") or c.get("focal") or (c.get("K") or [[None]])[0][0]
            if R is None or C is None or f is None:
                raise ValueError("missing R, C or f")
            flat = [float(x) for row in (R if isinstance(R[0], list) else [R[:3], R[3:6], R[6:9]])
                    for x in row]
            if len(flat) != 9:
                raise ValueError("R is not 3×3")
            if not _is_rotation(flat):
                # Not pedantry: a non-orthonormal R silently bends every ray,
                # and the carve would still return a volume.
                raise ValueError("R is not a rotation matrix (not orthonormal)")
            cams[key] = {
                "name": key, "R": flat, "C": [float(x) for x in C][:3], "f": float(f),
                "cx": float(c.get("cx", 0)), "cy": float(c.get("cy", 0)),
                "k1": float(c.get("k1", 0)), "k2": float(c.get("k2", 0)),
                "width": int(c.get("W") or c.get("width") or 0),
                "height": int(c.get("H") or c.get("height") or 0),
                "rms": c.get("rms"),
                "model": str(c.get("model") or model or "fisheye").lower(),
            }
        except Exception as e:
            problems.append(f"{name}: {e}")
    return {"cameras": cams, "problems": problems,
            "lens_model": model or "fisheye",
            "meters_per_unit": doc.get("meters_per_unit"),
            "levelled": bool(doc.get("levelled", doc.get("floor_z") is not None))}


def _is_rotation(flat: list[float], tol: float = 1e-3) -> bool:
    r = [flat[0:3], flat[3:6], flat[6:9]]
    for i in range(3):
        for j in range(3):
            dot = sum(r[i][k] * r[j][k] for k in range(3))
            if abs(dot - (1.0 if i == j else 0.0)) > tol:
                return False
    det = (r[0][0] * (r[1][1] * r[2][2] - r[1][2] * r[2][1])
           - r[0][1] * (r[1][0] * r[2][2] - r[1][2] * r[2][0])
           + r[0][2] * (r[1][0] * r[2][1] - r[1][1] * r[2][0]))
    return abs(det - 1.0) < tol or math.isclose(abs(det), 1.0, abs_tol=tol)


# ------------------------------------------------------------------- jobs
@PLUGIN.job("validate", title="Check the calibration",
            params={"asset_id": {"type": "integer", "required": True},
                    "capture_id": {"type": "capture"}},
            description="Does this file parse, and does it cover this capture's cameras?")
def validate(ctx):
    """Run for us by the core when a calibration lands. "You have a
    calibration" has to mean one that parses and names these cameras — a file
    of the right name that covers a different room would satisfy a count and
    nothing else."""
    aid = int(ctx.params["asset_id"])
    row = ctx.db.one("SELECT * FROM assets WHERE id=?", aid)
    if row is None:
        raise ValueError(f"no asset {aid}")
    try:
        doc = json.loads(Path(row["path"]).read_text())
    except Exception as e:
        _remember(ctx, row, {"ok": False, "error": f"not readable JSON: {e}"})
        raise ValueError(f"{row['name']} is not readable JSON: {e}")
    try:
        cal = parse(doc)
    except Exception as e:
        _remember(ctx, row, {"ok": False, "error": str(e)})
        raise ValueError(f"{row['name']}: {e}")

    cid = row["capture_id"]
    streams = [r["key"] for r in ctx.db.all(
        "SELECT key FROM streams WHERE capture_id=? ORDER BY idx", cid)] if cid else []
    have = set(cal["cameras"])
    covered = [s for s in streams if s.lower() in have]
    missing = [s for s in streams if s.lower() not in have]
    summary = {
        "ok": bool(cal["cameras"]) and not cal["problems"] and not missing,
        "cameras": sorted(have), "covers": covered, "missing": missing,
        "problems": cal["problems"], "lens_model": cal["lens_model"],
        "levelled": cal["levelled"], "meters_per_unit": cal["meters_per_unit"],
        "rms": {k: v["rms"] for k, v in cal["cameras"].items() if v.get("rms") is not None},
    }
    _remember(ctx, row, summary)
    bits = [f"{len(have)} camera(s)"]
    if streams:
        bits.append(f"{len(covered)} of {len(streams)} streams covered")
    if missing:
        bits.append(f"no calibration for {', '.join(missing)}")
    if cal["problems"]:
        bits.append(f"{len(cal['problems'])} unusable")
    return {**summary, "message": "; ".join(bits)}


def _remember(ctx, row, summary: dict) -> None:
    meta = json.loads(row["meta"] or "{}")
    meta["calibration"] = summary
    ctx.db.run("UPDATE assets SET meta=? WHERE id=?", json.dumps(meta), row["id"])
    ctx.host.publish(row["capture_id"], {"t": "assets", "capture_id": row["capture_id"]})


@PLUGIN.readiness
def also_needs(host, capture_id):
    """A count of files is not the requirement; a calibration that covers these
    cameras is. The core's declarative check handles the first, and this
    handles the rest — which is exactly the split `@PLUGIN.readiness` is for."""
    if capture_id is None:
        return []
    rows = host.db.all("SELECT * FROM assets WHERE capture_id=? AND kind=? AND state='ready'",
                       capture_id, "voxel_carve.calibration")
    if not rows:
        return []                      # the Requirement already says this, once
    for r in rows:
        summary = (json.loads(r["meta"] or "{}") or {}).get("calibration") or {}
        if summary.get("ok"):
            return []
    r = rows[0]
    summary = (json.loads(r["meta"] or "{}") or {}).get("calibration") or {}
    if summary.get("error"):
        return [f"the uploaded calibration ({r['name']}) did not parse: {summary['error']}"]
    if summary.get("missing"):
        return [f"the uploaded calibration has no camera for "
                f"{', '.join(summary['missing'])} — it may be from another room"]
    if summary.get("problems"):
        return [f"the uploaded calibration has unusable cameras: "
                f"{'; '.join(summary['problems'][:2])}"]
    return [f"the uploaded calibration ({r['name']}) has not been checked yet"]


@PLUGIN.job("suggest", title="Propose cross-camera links",
            params={"capture_id": {"type": "capture", "required": True},
                    "layer_id": {"type": "integer", "default": 0, "label": "Mask layer"},
                    "min_views": {"type": "integer", "default": 3,
                                  "label": "Cameras that must agree"},
                    "voxel_cm": {"type": "number", "default": 6.0, "label": "Voxel size (cm)"}},
            description="Shape-from-silhouette across cameras, as proposals a human accepts.")
def suggest(ctx):
    """Gated by the core before it is ever entered — reaching this body at all
    means a valid calibration is present.

    And then it stops, because the carve is not ported. Returning an empty
    proposal queue instead would be the worse failure: an empty queue reads as
    "the generator looked and found nothing", which is a claim, and this
    plugin has not looked at anything.
    """
    raise NotImplementedError(
        "the carve itself is not ported yet — see calib_ui/carve.py and docs/TODO.md §5. "
        "The calibration is valid and everything around the carve is wired: this job is "
        "reached only because the requirement gate passed.")


@PLUGIN.route.get("/{capture_id}/calibration")
def calibration(capture_id: int, request: Request, user: dict = Depends(current_user)):
    """What was uploaded and what the core makes of it — so the Carve tool can
    show the reason it is disabled instead of just being grey."""
    host = request.app.state.host
    rows = host.db.all("SELECT * FROM assets WHERE capture_id=? AND kind=? ORDER BY id DESC",
                       capture_id, "voxel_carve.calibration")
    out = []
    for r in rows:
        meta = json.loads(r["meta"] or "{}")
        out.append({"asset_id": r["id"], "name": r["name"], "state": r["state"],
                    **(meta.get("calibration") or {})})
    ready = host.readiness(capture_id).get("voxel_carve", {})
    return {"calibrations": out, "ready": ready.get("ok", False),
            "missing": ready.get("missing", [])}
