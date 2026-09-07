"""Talking to the SAM 3.1 function, and the two mask encodings on either side.

The function serves both roles on one endpoint:

    interactor  {"image": b64, "pos_points": [[x,y]…], "neg_points": [[x,y]…],
                 "obj_bbox": [x1,y1,x2,y2] | null}
                -> {"shapes": [{"type": "mask", "points": rle}]}

    tracker     {"images": [b64…], "shapes": [shape|null …],
                 "states": [state|null …]}
                -> {"shapes": [[shape per image] per object], "states": […]}

**Pixels are posted here, and that is a deliberate reversal.** `sync_id_ui`
went to great lengths not to: it shipped frame *timestamps* over ssh and ran a
helper inside a container on the far box, because the link to it ran at
~1.5 MB/s and a 1080p JPEG is ~670 kB — uploading a frame cost about what the
GPU work cost. Quorum runs on the same host as the function and reaches it over
a docker network, so that trade is gone: a JPEG is a millisecond, and the whole
ssh + remote-helper + `docker port` apparatus buys nothing. If this ever moves
back to the far side of a slow link, the thing to restore is the helper, not
this file.

Mask format: the function's RLE is ``[run, run, …, xmin, ymin, xmax, ymax]``
with the first run counting *background* pixels — the same run convention
`mask_layer` uses, plus a trailing inclusive bounding box. So converting is
splitting the tail off, never re-encoding, and the round trip is lossless.

stdlib only, on purpose: the venv has no `requests`, and one POST does not
justify a dependency.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request


class SamError(RuntimeError):
    """The endpoint refused, timed out, or answered something that is not a mask.

    Always carries a sentence somebody can act on — this reaches a person as a
    toast, and "request failed" would leave them pressing the key again.
    """


# ------------------------------------------------------------------- codecs
def sam_to_payload(points) -> dict | None:
    """SAM rle ``[runs…, xmin, ymin, xmax, ymax]`` -> ``{box, rle}``.

    Returns None for an empty mask: the function sends ``[]`` when the prompt
    found nothing, which is an answer ("not here"), not a failure.
    """
    if not points or len(points) < 5:
        return None
    xmin, ymin, xmax, ymax = (int(v) for v in points[-4:])
    runs = ",".join(str(int(r)) for r in points[:-4])
    return {"box": [xmin, ymin, xmax - xmin + 1, ymax - ymin + 1], "rle": runs}


def payload_to_sam(payload: dict) -> dict:
    """``{box, rle}`` -> a SAM ``mask`` shape dict, for seeding the tracker."""
    l, t, w, h = (int(v) for v in payload["box"])
    rle = payload.get("rle") or ""
    runs = [int(x) for x in rle.split(",")] if rle else [w * h]
    return {"type": "mask", "points": runs + [l, t, l + w - 1, t + h - 1]}


def shift(payload: dict | None, dx: int, dy: int) -> dict | None:
    """Move a mask by whole pixels. Only the box moves — the RLE is relative to
    it — which is what makes the ROI crop free rather than a re-encode."""
    if payload is None:
        return None
    l, t, w, h = payload["box"]
    return {"box": [int(l + dx), int(t + dy), int(w), int(h)], "rle": payload.get("rle", "")}


# ------------------------------------------------------------------- client
class Sam:
    """One endpoint, two calls. Holds no state; safe to make per job."""

    def __init__(self, url: str, timeout: float = 900.0, probe_timeout: float = 3.0):
        self.url = (url or "").strip()
        self.timeout = float(timeout)
        # Only the readiness probe uses this. It is short because a *down*
        # endpoint costs it on every check and a dead workspace should say so
        # quickly — but "short" is a LAN's opinion. A service reached across a
        # relayed link needs two or three round trips before it has said
        # anything at all, so this is settable rather than a constant.
        self.probe_timeout = float(probe_timeout)

    # -- transport ---------------------------------------------------------
    def post(self, payload: dict, timeout: float | None = None) -> dict:
        if not self.url:
            raise SamError(
                "no SAM endpoint is configured — set [plugins.sam] url in quorum.toml "
                "(or SAM_URL in the environment) to the function's address, e.g. "
                "http://nuclio-nuclio-pth-comfyorg-sam3-1-v2:8080/")
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                body = r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            detail = (e.read().decode(errors="replace") or "").strip()[:300]
            raise SamError(f"the SAM endpoint answered {e.code}: {detail or e.reason}")
        except urllib.error.URLError as e:
            raise SamError(
                f"cannot reach the SAM endpoint at {self.url} ({e.reason}). "
                f"Is the function deployed, and is this server on the same docker "
                f"network as its container?")
        except TimeoutError:
            raise SamError(f"the SAM endpoint did not answer within {timeout or self.timeout:.0f}s")
        try:
            out = json.loads(body)
        except ValueError:
            raise SamError(f"the SAM endpoint returned something that is not JSON: {body[:200]}")
        if isinstance(out, dict) and out.get("error"):
            raise SamError(str(out["error"])[:400])
        return out

    def alive(self, timeout: float | None = None) -> tuple[bool, str]:
        """Is it there? Answers without asking it to segment anything.

        An empty POST is refused by the function — that refusal *is* the proof
        it is running, so a 4xx counts as alive and only a transport failure
        counts as dead.
        """
        if not self.url:
            return False, "no endpoint configured"
        try:
            self.post({}, timeout=timeout or self.probe_timeout)
            return True, ""
        except SamError as e:
            msg = str(e)
            if "answered 4" in msg or "answered 5" in msg or "not JSON" in msg:
                return True, ""
            return False, msg

    # -- the two calls -----------------------------------------------------
    def interact(self, jpeg: bytes, pos=(), neg=(), bbox=None,
                 timeout: float = 120.0) -> dict | None:
        """Segment one object in one frame. Returns ``{box, rle}`` or None.

        Coordinates are in the pixels of the image handed over — the caller
        shifts them into a crop and shifts the answer back out, because only
        the caller knows where the crop came from.
        """
        res = self.post({"image": base64.b64encode(jpeg).decode(),
                         "pos_points": [[int(p[0]), int(p[1])] for p in pos],
                         "neg_points": [[int(p[0]), int(p[1])] for p in neg],
                         "obj_bbox": [int(v) for v in bbox] if bbox else None}, timeout)
        shapes = res.get("shapes") or []
        return sam_to_payload(shapes[0].get("points")) if shapes else None

    def track(self, jpegs: list[bytes], seeds: list[dict],
              timeout: float | None = None) -> list[list[dict | None]]:
        """Propagate `seeds` (given on the first frame) across every frame.

        Returns, per seeded object, one ``{box, rle} | None`` per frame —
        *including* the first, which comes back re-segmented rather than echoed.
        """
        res = self.post({"images": [base64.b64encode(j).decode() for j in jpegs],
                         "shapes": [payload_to_sam(s) for s in seeds],
                         "states": [None] * len(seeds)}, timeout)
        per_obj = res.get("shapes") or []
        if len(jpegs) == 1:                    # the function unwraps this case
            per_obj = [[s] for s in per_obj]
        return [[sam_to_payload((s or {}).get("points")) for s in seq] for seq in per_obj]
