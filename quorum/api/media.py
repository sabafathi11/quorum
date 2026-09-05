"""Byte-range file serving for video.

Written out rather than delegated because browsers seek by range and a video
endpoint that ignores Range makes scrubbing feel broken in a way that is hard
to attribute later.
"""
from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
CHUNK = 512 * 1024


def serve_file(request: Request, path: Path, cfg, download_name: str = "") -> Response:
    if not cfg.is_media_allowed(path):
        raise HTTPException(403, f"{path} is outside the configured media roots")
    if not path.is_file():
        raise HTTPException(404, f"missing media file {path.name}")
    size = path.stat().st_size
    ctype = mimetypes.guess_type(download_name or str(path))[0] or "application/octet-stream"
    rng = request.headers.get("range")
    if not rng:
        return FileResponse(path, media_type=ctype, filename=download_name or None,
                            content_disposition_type="inline",
                            headers={"accept-ranges": "bytes", "cache-control": "private, max-age=3600"})
    m = RANGE_RE.match(rng)
    if not m:
        raise HTTPException(416, "unsupported range")
    a, b = m.group(1), m.group(2)
    start = int(a) if a else max(0, size - int(b))
    end = int(b) if b and a else size - 1
    end = min(end, size - 1)
    if start > end:
        return Response(status_code=416, headers={"content-range": f"bytes */{size}"})

    def body():
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    return StreamingResponse(body(), status_code=206, media_type=ctype, headers={
        "content-range": f"bytes {start}-{end}/{size}",
        "accept-ranges": "bytes",
        "content-length": str(end - start + 1),
        "cache-control": "private, max-age=3600",
    })
