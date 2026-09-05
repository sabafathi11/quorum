"""Reading the server's own disks, inside the media roots and nowhere else.

The upload path answers "this file is on my laptop". This answers the other
half, which on the deployment that matters is the *only* half: the camera
videos are already on the box — on the GPU host they are a 4 TB read-only
share under `/media/jvn-server/…/records` that nobody is going to copy into
the tree, and pulling 150 MB down a browser and pushing it back up again to
reach a file the server can already open is work for its own sake.

So: list what is there, and let a provider be given a path.

The boundary is exactly the one that already exists. `media_roots` is what the
server is willing to read — it is what `serve_file` enforces on the way out —
and this enforces the same rule on the way in, through the same function.
There is no second list and no second policy: a path this returns is a path
that endpoint would have served, and a path outside is 403 here for the same
reason it is 403 there.

    GET /api/browse                     -> the roots
    GET /api/browse?path=/x/y           -> one directory
    GET /api/browse?path=/x/y&q=2026    -> …filtered, because `records` is one
                                           flat directory with 1,400 files in it
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from .deps import current_user

router = APIRouter()

# Enough to fill a picker without turning a directory of 40,000 files into a
# 6 MB response. The count is always the true one, so the client can say what
# it is not showing rather than quietly lying about what is there.
LIMIT = 2000


def _roots(cfg) -> list[Path]:
    seen, out = set(), []
    for r in [*cfg.media_roots, cfg.data_dir]:
        try:
            rp = r.resolve()
        except OSError:
            continue
        if rp not in seen and rp.is_dir():
            seen.add(rp)
            out.append(rp)
    return out


def _entry(p: Path, st: os.stat_result | None = None) -> dict:
    try:
        st = st or p.stat()
    except OSError:
        return {"name": p.name, "path": str(p), "dir": False, "size": 0, "mtime": 0,
                "readable": False}
    d = bool(st.st_mode & 0o040000)
    return {"name": p.name, "path": str(p), "dir": d,
            "size": 0 if d else int(st.st_size), "mtime": float(st.st_mtime),
            "readable": os.access(p, os.R_OK)}


@router.get("/browse")
def browse(request: Request, path: str = "", q: str = "",
           user: dict = Depends(current_user)):
    """One directory, or the roots when no path is given.

    `readable` is reported rather than assumed. The share on the GPU box is
    `drwxr-x--- root:root` and the container runs as root *because of that*;
    saying which entries this process can actually open turns "the capture
    built and every cell is black" into something the picker can grey out
    before you pick it.
    """
    cfg = request.app.state.host.cfg
    roots = _roots(cfg)
    if not path:
        return {"path": "", "parent": None, "at_root": True,
                "roots": [{"name": str(r), "path": str(r)} for r in roots],
                "entries": [_entry(r) for r in roots], "total": len(roots), "shown": len(roots)}

    p = Path(path)
    if not cfg.is_media_allowed(p):
        raise HTTPException(403, f"{path} is outside the media roots this server will read. "
                                 f"Roots: {', '.join(str(r) for r in roots) or 'none'}")
    if not p.is_dir():
        raise HTTPException(404 if not p.exists() else 400,
                            f"{path} is not a directory on this server")
    try:
        names = sorted(os.listdir(p))
    except PermissionError:
        raise HTTPException(403, f"{path} exists but this server cannot read it — "
                                 f"check the bind mount and who the container runs as")
    needle = q.strip().lower()
    entries, total = [], 0
    for name in names:
        if name.startswith("."):
            continue
        child = p / name
        is_dir = child.is_dir()
        # The filter is for files. A directory that matches nothing is still
        # the way to somewhere that does, so it always stays on screen.
        if needle and not is_dir and needle not in name.lower():
            continue
        total += 1
        if len(entries) < LIMIT:
            entries.append(_entry(child))
    entries.sort(key=lambda e: (not e["dir"], e["name"]))
    # The parent is only offered while it is still inside a root: the picker
    # must not be able to walk to `/` one `..` at a time.
    parent = str(p.parent) if (p.parent != p and cfg.is_media_allowed(p.parent)) else None
    return {"path": str(p), "parent": parent, "at_root": any(p == r for r in roots),
            "roots": [{"name": str(r), "path": str(r)} for r in roots],
            "entries": entries, "total": total, "shown": len(entries)}
