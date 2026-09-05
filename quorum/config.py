"""Server configuration.

Everything is resolved from one TOML file (``quorum.toml`` beside the data
directory, or ``$QUORUM_CONFIG``) with environment overrides, so a lab server
and a laptop differ by a file rather than by a branch.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # quorum/
REPO = ROOT.parent                                     # the annotation tree


@dataclass
class Config:
    # where state lives
    data_dir: Path = ROOT / "data"
    db_path: Path = ROOT / "data" / "quorum.db"
    blob_dir: Path = ROOT / "data" / "blobs"
    upload_dir: Path = ROOT / "data" / "uploads"
    cache_dir: Path = ROOT / "data" / "cache"       # stills; disposable

    # biggest single upload, bytes. One camera video here is ~150 MB.
    max_upload: int = 8 * 1024 ** 3

    # where plugins are looked for, in order; later paths win on id collision
    plugin_paths: list[Path] = field(default_factory=lambda: [ROOT / "plugins"])
    plugins_enabled: list[str] | None = None           # None = all discovered

    # `[plugins.<id>]` tables, reaching a plugin as `ctx.settings("key")`. The
    # core never looks inside one: which keys a plugin understands is the
    # plugin's business, and a core that validated them would have to know what
    # every plugin is for.
    plugins: dict = field(default_factory=dict)

    # media roots a capture provider is allowed to serve files from.
    # Nothing outside these is readable over HTTP, ever.
    media_roots: list[Path] = field(default_factory=lambda: [REPO])

    # http
    host: str = "127.0.0.1"
    port: int = 8600

    # auth: "open" trusts everybody as `dev_user` (laptop mode);
    #       "token" requires a bearer token from the users table (lab server).
    auth_mode: str = "open"
    dev_user: str = "local"

    jobs_workers: int = 2

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        cfg = cls()
        p = Path(path or os.environ.get("QUORUM_CONFIG") or ROOT / "quorum.toml")
        if p.exists():
            raw = tomllib.loads(p.read_text())
            for k, v in raw.items():
                if not hasattr(cfg, k):
                    raise SystemExit(f"{p}: unknown setting {k!r}")
                cur = getattr(cfg, k)
                if isinstance(cur, Path):
                    v = (p.parent / str(v)).resolve()
                elif isinstance(cur, list) and cur and isinstance(cur[0], Path):
                    v = [(p.parent / str(x)).resolve() for x in v]
                setattr(cfg, k, v)
        if os.environ.get("QUORUM_PORT"):
            cfg.port = int(os.environ["QUORUM_PORT"])
        if os.environ.get("QUORUM_HOST"):
            cfg.host = os.environ["QUORUM_HOST"]
        if os.environ.get("QUORUM_AUTH"):
            cfg.auth_mode = os.environ["QUORUM_AUTH"]
        for d in (cfg.data_dir, cfg.blob_dir, cfg.upload_dir, cfg.cache_dir):
            d.mkdir(parents=True, exist_ok=True)
        return cfg

    def is_media_allowed(self, path: Path) -> bool:
        """A capture provider may hand out any path under a configured root.

        Our own data directory always counts: a capture built from uploaded
        videos has its media *inside* it, and a server that refused to serve
        what it was just given would be an odd kind of careful.
        """
        try:
            rp = path.resolve()
        except OSError:
            return False
        roots = [*self.media_roots, self.data_dir]
        return any(rp.is_relative_to(r.resolve()) for r in roots)
