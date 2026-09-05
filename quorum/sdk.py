"""The plugin SDK — everything a plugin is allowed to know about the core.

A plugin is a directory with a ``plugin.py`` that builds one ``Plugin`` object:

    from quorum.sdk import Plugin, LayerType, Tool

    PLUGIN = Plugin(id="mask", name="Masks", web="mask.js")
    PLUGIN.layer_type(LayerType(id="mask.rle", name="Mask (RLE)", renderer="MaskRenderer"))

    @PLUGIN.job("reindex", title="Re-index masks")
    def reindex(ctx: JobContext):
        ...

    @PLUGIN.route.get("/stats/{capture_id}")
    def stats(capture_id: int):
        ...

The core never imports a plugin's types and never interprets its payloads. What
it does for a plugin: stores its records, orders and broadcasts its ops, runs
its jobs, serves its ES module, and namespaces its routes under
``/api/p/<plugin id>/``.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Iterable

from fastapi import APIRouter

from .db import Database, now

SDK_VERSION = "0.1"


# --------------------------------------------------------------- declarations
@dataclass
class Tool:
    """A workspace: its own tab, panels, keymap and ops."""
    id: str
    title: str
    icon: str = "◉"
    order: int = 100
    description: str = ""
    # a tool can say it only makes sense when the capture has these layer types
    needs_layer_types: list[str] = field(default_factory=list)


@dataclass
class LayerType:
    """A geometry kind. `renderer` names an export of the plugin's ES module."""
    id: str
    name: str
    renderer: str = ""
    editable: bool = False
    payload_hint: str = ""          # human-readable shape of the opaque payload


@dataclass
class JobKind:
    kind: str
    title: str
    params: dict[str, Any] = field(default_factory=dict)   # name -> {type, default, label}
    description: str = ""


@dataclass
class ProviderKind:
    id: str
    title: str
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass
class IOKind:
    id: str
    title: str
    direction: str                  # "import" | "export"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssetKind:
    """A kind of file somebody uploads, and the *only* thing the upload
    machinery knows about it.

    The core stores bytes, a name, a size, a hash and this id. It never opens
    the file. The upload dialog is built from the list of declared kinds, so a
    plugin that needs a file of its own gets an upload UI without the upload UI
    learning anything about that plugin — which is the whole point: the voxel
    carve needs a calibration, and nothing in the upload path may know that a
    calibration exists.
    """
    id: str
    title: str
    description: str = ""
    accept: list[str] = field(default_factory=list)     # [".mp4", ".json"] — a hint, not a rule
    multiple: bool = False                              # may a capture have several?
    on_ready: str = ""                                  # a job of this plugin, given {asset_id}
    icon: str = "▤"


@dataclass
class Requirement:
    """Something a plugin cannot work without, declared rather than checked.

    The core answers `GET /captures/{id}/readiness` from these *and* refuses to
    start a job whose requirements are unmet. The greyed-out button is a
    courtesy; the server refusing is the guarantee — a plugin must not be able
    to half-run because somebody found the CLI.
    """
    asset_kind: str = ""            # namespaced, e.g. "voxel_carve.calibration"
    at_least: int = 1
    why: str = ""                   # what breaks without it, in one sentence
    fix: str = ""                   # what the person should do about it
    tools: list[str] = field(default_factory=list)   # empty = the whole plugin
    jobs: list[str] = field(default_factory=list)    # empty = every job


# --------------------------------------------------------------------- plugin
class Plugin:
    def __init__(self, id: str, name: str, version: str = "0.1.0",
                 description: str = "", web: str | None = None,
                 requires_core: str = SDK_VERSION):
        self.id = id
        self.name = name
        self.version = version
        self.description = description
        self.web = web                       # ES module filename inside <plugin>/web/
        self.requires_core = requires_core
        self.route = APIRouter()
        self.dir: Path | None = None         # set by the loader
        self.module_name: str = ""           # set by the loader; sys.modules key
        self.tools: list[Tool] = []
        self.layer_types: list[LayerType] = []
        self.job_kinds: list[JobKind] = []
        self.provider_kinds: list[ProviderKind] = []
        self.io_kinds: list[IOKind] = []
        self.asset_kinds: list[AssetKind] = []
        self.requirements: list[Requirement] = []
        self.jobs: dict[str, Callable] = {}
        self.providers: dict[str, Callable] = {}
        self.io: dict[str, Callable] = {}
        self.validators: dict[str, Callable] = {}
        self.readiness_fns: list[Callable] = []
        self.on_start: list[Callable] = []
        self.on_delete: list[Callable] = []
        self.settings: dict[str, Any] = {}   # from quorum.toml [plugins.<id>]

    # -- declarations --------------------------------------------------------
    def tool(self, t: Tool) -> Tool:
        self.tools.append(t)
        return t

    def layer_type(self, lt: LayerType) -> LayerType:
        self.layer_types.append(lt)
        return lt

    def asset_kind(self, ak: AssetKind) -> AssetKind:
        """Declare a kind of file. The id is namespaced to this plugin, so
        `AssetKind(id="calibration")` becomes `voxel_carve.calibration`."""
        if not ak.id.startswith(self.id + "."):
            ak.id = f"{self.id}.{ak.id}"
        self.asset_kinds.append(ak)
        return ak

    def requires(self, req: Requirement) -> Requirement:
        if req.asset_kind and "." not in req.asset_kind:
            req.asset_kind = f"{self.id}.{req.asset_kind}"
        self.requirements.append(req)
        return req

    def readiness(self, fn):
        """`fn(host, capture_id) -> list[str]` — reasons this plugin cannot
        work here, or `[]`. For anything an asset count cannot express."""
        self.readiness_fns.append(fn)
        return fn

    # -- handlers ------------------------------------------------------------
    def job(self, kind: str, title: str = "", params: dict | None = None,
            description: str = ""):
        """Register a background job. Handler takes a JobContext."""
        def deco(fn):
            self.job_kinds.append(JobKind(kind, title or kind, params or {}, description))
            self.jobs[kind] = fn
            return fn
        return deco

    def provider(self, id: str, title: str = "", params: dict | None = None,
                 description: str = ""):
        """Register a capture provider. Handler takes an IngestContext."""
        def deco(fn):
            self.provider_kinds.append(ProviderKind(id, title or id, params or {}, description))
            self.providers[id] = fn
            self.jobs[f"provider:{id}"] = fn      # providers run as jobs; ingest is never quick
            return fn
        return deco

    def importer(self, id: str, title: str = "", params: dict | None = None):
        def deco(fn):
            self.io_kinds.append(IOKind(id, title or id, "import", params or {}))
            self.io[("import", id)] = fn
            self.jobs[f"import:{id}"] = fn
            return fn
        return deco

    def exporter(self, id: str, title: str = "", params: dict | None = None):
        def deco(fn):
            self.io_kinds.append(IOKind(id, title or id, "export", params or {}))
            self.io[("export", id)] = fn
            self.jobs[f"export:{id}"] = fn
            return fn
        return deco

    def validator(self, *kinds: str):
        """Say whether one of your ops still makes sense.

        The handler takes ``(host, capture_id, payload)`` and returns ``None``
        if the op is still applicable, or a short reason if it is not.

        This exists because an op can be *stored before it is applied* — the
        proposal queue is a pile of pre-built ops waiting for a human, and the
        world moves under them: a suggestion to link two tracks is nonsense
        once somebody has joined one of them into something else. Only the
        plugin that owns the op kind can judge that, so the core asks rather
        than guesses.
        """
        def deco(fn):
            for k in kinds:
                self.validators[k if k.startswith(self.id + ".") else f"{self.id}.{k}"] = fn
            return fn
        return deco

    def startup(self, fn):
        self.on_start.append(fn)
        return fn

    def cleanup(self, fn):
        """`fn(ctx, capture_id)` — a capture is being deleted; drop what you
        made for it. The core deletes its own rows and the uploaded files; it
        has no idea a plugin wrote a transcoded video somewhere, so it asks
        rather than guesses, and a plugin that raises does not block the
        delete."""
        self.on_delete.append(fn)
        return fn

    # -- manifest ------------------------------------------------------------
    def manifest(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "requiresCore": self.requires_core,
            "web": f"/plugins/{self.id}/web/{self.web}" if self.web else None,
            "provides": {
                "tools": [asdict(t) for t in self.tools],
                "layerTypes": [asdict(t) for t in self.layer_types],
                "jobs": [asdict(t) for t in self.job_kinds],
                "providers": [asdict(t) for t in self.provider_kinds],
                "io": [asdict(t) for t in self.io_kinds],
                "assetKinds": [asdict(t) for t in self.asset_kinds],
            },
            "requires": [asdict(r) for r in self.requirements],
            "settings": self.settings,
        }


# -------------------------------------------------------------------- context
class Ctx:
    """What every handler gets: the database, config, and the plugin itself."""

    def __init__(self, host, plugin: Plugin, actor: str = "system"):
        self.host = host
        self.plugin = plugin
        self.actor = actor

    @property
    def db(self) -> Database:
        return self.host.db

    @property
    def cfg(self):
        return self.host.cfg

    def settings(self, key: str, default=None):
        return self.plugin.settings.get(key, default)

    # -- ops -----------------------------------------------------------------
    def emit_op(self, capture_id: int, kind: str, payload: dict,
                layer_id: int | None = None) -> dict:
        """Append an op to the capture's log and broadcast it. Plugin-namespaced."""
        if not kind.startswith(self.plugin.id + "."):
            kind = f"{self.plugin.id}.{kind}"
        return self.host.append_op(capture_id, kind, payload, self.actor, layer_id)

    # -- per-plugin document store ------------------------------------------
    def doc_get(self, capture_id: int, key: str, default=None):
        r = self.db.one("SELECT value, version FROM docs WHERE capture_id=? AND ns=? AND key=?",
                        capture_id, self.plugin.id, key)
        if r is None:
            return default, 0
        return json.loads(r["value"]), r["version"]

    def doc_put(self, capture_id: int, key: str, value, expect_version: int | None = None) -> int:
        with self.db.tx() as c:
            cur = c.execute("SELECT version FROM docs WHERE capture_id=? AND ns=? AND key=?",
                            (capture_id, self.plugin.id, key)).fetchone()
            have = cur["version"] if cur else 0
            if expect_version is not None and expect_version != have:
                raise Conflict(f"doc {key} is at version {have}, you have {expect_version}")
            c.execute(
                "INSERT INTO docs (capture_id, ns, key, version, value, updated) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(capture_id, ns, key) DO UPDATE SET "
                "version=excluded.version, value=excluded.value, updated=excluded.updated",
                (capture_id, self.plugin.id, key, have + 1, json.dumps(value), now()))
        return have + 1

    # -- assets --------------------------------------------------------------
    # A handler asks for a file by asset id and gets a path. It never learns
    # that uploading exists, and never grows a second code path for "a file on
    # disk" versus "a file somebody sent us".
    def asset(self, asset_id, kind: str = "") -> Path:
        r = self.db.one("SELECT * FROM assets WHERE id=?", int(asset_id))
        if r is None:
            raise FileNotFoundError(f"no asset {asset_id}")
        if r["state"] != "ready":
            raise FileNotFoundError(
                f"asset {r['name']!r} is {r['state']} — its upload never finished")
        want = kind if not kind or "." in kind else f"{self.plugin.id}.{kind}"
        if want and r["kind"] != want:
            raise ValueError(
                f"asset {r['name']!r} is filed as {r['kind'] or 'unsorted'!r}, not {want!r} — "
                f"change its kind on the capture's Data panel if that is wrong")
        p = Path(r["path"])
        if not p.is_file():
            raise FileNotFoundError(f"asset {r['name']!r} is recorded but its file is gone")
        return p

    def assets(self, capture_id: int | None, kind: str = "") -> list[dict]:
        """Every ready asset of a kind, oldest first. `kind` is namespaced to
        this plugin when it has no dot, exactly like op kinds."""
        want = kind if not kind or "." in kind else f"{self.plugin.id}.{kind}"
        sql = "SELECT * FROM assets WHERE state='ready'"
        args: list = []
        if capture_id is None:
            sql += " AND capture_id IS NULL"
        else:
            sql += " AND capture_id=?"
            args.append(int(capture_id))
        if want:
            sql += " AND kind=?"
            args.append(want)
        return [dict(r) for r in self.db.all(sql + " ORDER BY id", *args)]

    # -- blobs ---------------------------------------------------------------
    def blob_path(self, name: str) -> Path:
        d = self.cfg.blob_dir / self.plugin.id
        d.mkdir(parents=True, exist_ok=True)
        return d / name


class Conflict(Exception):
    """A write lost a race. Surfaced to the client as 409 with the reason."""


class Cancelled(Exception):
    pass


class JobContext(Ctx):
    def __init__(self, host, plugin, job_id: str, params: dict, capture_id: int | None,
                 actor: str = "system"):
        super().__init__(host, plugin, actor)
        self.job_id = job_id
        self.params = params
        self.capture_id = capture_id
        self._cancel = threading.Event()
        self.lines: list[str] = []

    # -- progress ------------------------------------------------------------
    def progress(self, frac: float, message: str = "") -> None:
        self.check()
        self.host.jobs.update(self.job_id, progress=max(0.0, min(1.0, frac)), message=message)

    def log(self, line: str) -> None:
        self.lines.append(line)
        self.host.jobs.update(self.job_id, message=line)

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    # -- writing captures ----------------------------------------------------
    @property
    def write(self) -> "Writer":
        return Writer(self.host, self.plugin)


class IngestContext(JobContext):
    pass


# --------------------------------------------------------------------- writer
class Writer:
    """Bulk-safe helpers for building a capture. The only way plugins write
    core tables — so the shapes of those tables stay the core's business."""

    def __init__(self, host, plugin: Plugin):
        self.host = host
        self.db: Database = host.db
        self.plugin = plugin

    def capture(self, key: str, name: str, n_frames: int, fps: float,
                layout: dict | None = None, config: dict | None = None,
                replace: bool = False, capture_id: int | None = None,
                provider_kind: str = "", state: str = "ready") -> int:
        """Make or update a capture. `capture_id` names an existing one — the
        draft the person uploaded into — and is how a provider stays idempotent:
        building twice must produce the same capture, not a second one."""
        old = (self.db.one("SELECT id FROM captures WHERE id=?", capture_id) if capture_id
               else self.db.one("SELECT id FROM captures WHERE key=?", key))
        if old and replace and not capture_id:
            self.db.run("DELETE FROM captures WHERE id=?", old["id"])
            old = None
        if old:
            self.db.run("UPDATE captures SET key=?, name=?, provider=?, provider_kind=?, "
                        "state=?, config=?, layout=?, n_frames=?, fps=? WHERE id=?",
                        key, name, self.plugin.id, provider_kind, state,
                        json.dumps(config or {}), json.dumps(layout or {}),
                        int(n_frames), float(fps), old["id"])
            return int(old["id"])
        return self.db.insert(
            "captures", key=key, name=name, provider=self.plugin.id,
            provider_kind=provider_kind, state=state,
            config=json.dumps(config or {}), layout=json.dumps(layout or {}),
            n_frames=int(n_frames), fps=float(fps), created=now())

    def stream(self, capture_id: int, key: str, idx: int = 0, name: str = "",
               width: int = 0, height: int = 0, n_frames: int = 0,
               media: dict | None = None, meta: dict | None = None,
               frame_map: bytes | None = None, timestamps=None,
               duration: float = 0.0, enabled: bool = True) -> int:
        """Upsert one stream.

        Give `timestamps` (an int64-µs array or its bytes) and the frame map is
        *derived* — from the capture's own frame rate and this stream's clock
        offset — rather than supplied. That is what makes a clock offset a
        slider instead of a re-run of somebody's preprocessing script. An
        explicit `frame_map` still works, for a provider that has one and no
        timestamps to go with it.
        """
        from . import probe
        import numpy as np

        stamps = timestamps
        if stamps is not None and not isinstance(stamps, (bytes, bytearray)):
            stamps = probe.pack(np.asarray(stamps))
        old = self.db.one("SELECT id, time_offset FROM streams WHERE capture_id=? AND key=?",
                          capture_id, key)
        offset = float(old["time_offset"]) if old else 0.0
        if stamps and frame_map is None:
            cap = self.db.one("SELECT n_frames, fps FROM captures WHERE id=?", capture_id)
            frame_map = probe.frame_map(probe.unpack(stamps), cap["n_frames"], cap["fps"],
                                        offset).tobytes() if cap else None
        cols = dict(idx=idx, name=name or key, width=width, height=height,
                    n_frames=n_frames, media=json.dumps(media or {}),
                    meta=json.dumps(meta or {}), duration=float(duration),
                    enabled=1 if enabled else 0)
        if old:
            sets = ",".join(f"{k}=?" for k in cols)
            args = list(cols.values())
            if stamps is not None:
                sets += ", timestamps=?"
                args.append(stamps)
            if frame_map is not None:
                sets += ", frame_map=?"
                args.append(frame_map)
            self.db.run(f"UPDATE streams SET {sets} WHERE id=?", *args, old["id"])
            return int(old["id"])
        return self.db.insert("streams", capture_id=capture_id, key=key,
                              timestamps=stamps, frame_map=frame_map, **cols)

    def layer(self, capture_id: int, key: str, name: str, type: str,
              provenance: str = "human", config: dict | None = None,
              replace: bool = False) -> int:
        old = self.db.one("SELECT id FROM layers WHERE capture_id=? AND key=?", capture_id, key)
        if old and replace:
            self.db.run("DELETE FROM layers WHERE id=?", old["id"])
            old = None
        if old:
            return int(old["id"])
        return self.db.insert("layers", capture_id=capture_id, key=key, name=name, type=type,
                              provenance=provenance, config=json.dumps(config or {}), created=now())

    def objects(self, layer_id: int, stream_id: int, items: Iterable[dict]) -> dict[str, int]:
        """items: {key, label, frames:[{frame, outside, payload}], meta}."""
        ids: dict[str, int] = {}
        with self.db.tx() as c:
            for it in items:
                frames = it.get("frames") or []
                fnums = [int(f["frame"]) for f in frames] or [0]
                cur = c.execute(
                    "INSERT INTO objects (layer_id, stream_id, key, label, first_frame, last_frame, meta) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(layer_id, stream_id, key) DO UPDATE SET "
                    "label=excluded.label, first_frame=excluded.first_frame, "
                    "last_frame=excluded.last_frame, meta=excluded.meta RETURNING id",
                    (layer_id, stream_id, str(it["key"]), it.get("label", ""),
                     min(fnums), max(fnums), json.dumps(it.get("meta") or {})))
                oid = int(cur.fetchone()[0])
                ids[str(it["key"])] = oid
                c.execute("DELETE FROM shapes WHERE object_id=?", (oid,))
                c.executemany(
                    "INSERT INTO shapes (object_id, frame, outside, payload) VALUES (?,?,?,?)",
                    [(oid, int(f["frame"]), int(f.get("outside", 0)),
                      f["payload"] if isinstance(f["payload"], str) else json.dumps(f["payload"]))
                     for f in frames])
        return ids
