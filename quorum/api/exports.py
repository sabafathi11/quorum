"""Getting results back out.

An exporter writes files into `data/exports/` and returns their paths, which is
right — a 40 MB XML per camera should not travel through a JSON response, and a
job that finishes while the tab is closed still has to leave its output
somewhere. But a path on the server's disk is not an answer to "how do I get my
annotations", and until this existed the only one was `ls`.

So: list what is there, and hand it over. Some exporters write one file and
some write a directory of them (`cvat_xml` writes one XML per stream), so a
directory is zipped on the way out rather than making somebody click six times.

The whole surface is confined to the exports directory by resolving the path
and checking it is inside — the client names a relative path, and a relative
path is the classic way to be handed `../../etc/passwd`.
"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from ..db import now
from .deps import current_user

router = APIRouter()

MAX_LIST = 500


def _root(host) -> Path:
    d = host.cfg.data_dir / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _inside(host, rel: str) -> Path:
    """A relative path from the client, resolved and proved to be ours."""
    root = _root(host).resolve()
    p = (root / str(rel or "").lstrip("/")).resolve()
    if not p.is_relative_to(root):
        raise HTTPException(403, "that path is outside the exports directory")
    if not p.exists():
        raise HTTPException(404, f"no export named {rel!r}")
    return p


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


@router.get("/exports")
def list_exports(request: Request, capture: int | None = None,
                 user: dict = Depends(current_user)):
    """Everything an exporter has left behind, newest first.

    `capture` filters by the capture's key appearing in the name, which is the
    convention every exporter here follows. It is a filter, not a guarantee —
    the core does not own these filenames, the exporters do — so an entry that
    does not match is still listed when no filter is given.
    """
    host = request.app.state.host
    root = _root(host)
    key = ""
    if capture is not None:
        r = host.db.one("SELECT key FROM captures WHERE id=?", capture)
        key = r["key"] if r else ""
    out = []
    for p in sorted(root.iterdir(), key=lambda x: -x.stat().st_mtime)[:MAX_LIST]:
        if p.name.startswith("."):
            continue
        if key and key not in p.name:
            continue
        out.append({
            "path": p.name,
            "name": p.name,
            "dir": p.is_dir(),
            "files": sum(1 for _ in p.rglob("*")) if p.is_dir() else 1,
            "size": _size(p),
            "updated": p.stat().st_mtime,
        })
    return {"exports": out, "root": str(root)}


@router.get("/exports/download")
def download(request: Request, path: str, user: dict = Depends(current_user)):
    p = _inside(request.app.state.host, path)
    if p.is_file():
        return FileResponse(p, filename=p.name, media_type="application/octet-stream")
    # A directory of per-stream files goes out as one zip rather than six
    # clicks. Written beside the cache because a 240 MB zip does not belong in
    # memory, and rebuilt whenever the directory is newer than the zip.
    host = request.app.state.host
    zpath = host.cfg.cache_dir / f"{p.name}.zip"
    newest = max((f.stat().st_mtime for f in p.rglob("*") if f.is_file()), default=0)
    if not zpath.exists() or zpath.stat().st_mtime < newest:
        tmp = zpath.with_suffix(".zip.part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(p.parent))
        tmp.replace(zpath)
    return FileResponse(zpath, filename=f"{p.name}.zip", media_type="application/zip")


@router.delete("/exports")
def remove(request: Request, path: str, user: dict = Depends(current_user)):
    p = _inside(request.app.state.host, path)
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)
    return {"ok": True, "deleted": p.name}
