"""The Host: the one object that knows the database, the config, the loaded
plugins, the job runner and the realtime hub — and nothing about masks,
identities, cameras or geometry."""
from __future__ import annotations

import json

from pathlib import Path

from . import probe
from .config import Config
from .db import Database, now
from .jobs import JobRunner
from .plugins import load_all
from .realtime import Hub
from .sdk import Ctx, Plugin


class Host:
    def __init__(self, cfg: Config, plugin_settings: dict | None = None):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.hub = Hub()
        self.jobs = JobRunner(self, cfg.jobs_workers)
        # `plugin_settings` is for a caller that has its own (the tests); the
        # ordinary source is the config file's `[plugins.<id>]` tables. Passing
        # `{}` explicitly still means "none", which is why this is not `or`.
        self.plugins, self.plugin_problems = load_all(
            cfg.plugin_paths, cfg.plugins_enabled,
            cfg.plugins if plugin_settings is None else plugin_settings)
        self.started = now()

    # -- plugins -------------------------------------------------------------
    def plugin(self, pid: str) -> Plugin:
        p = self.plugins.get(pid)
        if p is None:
            raise KeyError(f"no plugin {pid!r}")
        return p

    def ctx(self, pid: str, actor: str = "system") -> Ctx:
        return Ctx(self, self.plugin(pid), actor)

    def manifests(self) -> list[dict]:
        return [p.manifest() for p in self.plugins.values()]

    def layer_type_owner(self, type_id: str) -> Plugin | None:
        for p in self.plugins.values():
            if any(lt.id == type_id for lt in p.layer_types):
                return p
        return None

    def asset_kinds(self) -> dict[str, dict]:
        """Every declared kind, by id, with the plugin that owns it."""
        out: dict[str, dict] = {}
        for p in self.plugins.values():
            for ak in p.asset_kinds:
                out[ak.id] = {"plugin": p.id, "pluginName": p.name, "kind": ak}
        return out

    # -- readiness -----------------------------------------------------------
    # A plugin declares what it cannot work without; the core answers whether
    # it has it. Both halves matter: the UI greys a tool out, and `jobs.submit`
    # *refuses*, so a plugin cannot half-run because somebody found the CLI.
    def readiness(self, capture_id: int | None) -> dict[str, dict]:
        counts: dict[str, int] = {}
        if capture_id is not None:
            for r in self.db.all(
                    "SELECT kind, COUNT(*) n FROM assets WHERE capture_id=? AND state='ready' "
                    "GROUP BY kind", capture_id):
                counts[r["kind"]] = r["n"]
        out: dict[str, dict] = {}
        for p in self.plugins.values():
            missing = []
            for req in p.requirements:
                have = counts.get(req.asset_kind, 0)
                if req.asset_kind and have < req.at_least:
                    missing.append({
                        "assetKind": req.asset_kind, "have": have, "need": req.at_least,
                        "why": req.why, "fix": req.fix,
                        "tools": req.tools, "jobs": req.jobs})
            for fn in p.readiness_fns:
                try:
                    # A reason is a sentence, or a dict when it applies to only
                    # some of the plugin's jobs or tools. A `Requirement` can
                    # already say "only these jobs"; a check that runs code had
                    # no way to, so one plugin holding two unrelated abilities
                    # — an interactor that needs a model endpoint and a batch
                    # job that does not — had to block both or neither.
                    for reason in (fn(self, capture_id) or []):
                        if isinstance(reason, dict):
                            missing.append({"assetKind": "", "fix": "", "tools": [], "jobs": [],
                                            **reason, "why": str(reason.get("why", ""))})
                        else:
                            missing.append({"assetKind": "", "why": str(reason), "fix": "",
                                            "tools": [], "jobs": []})
                except Exception as e:                 # a broken check must not gate everything
                    missing.append({"assetKind": "", "why": f"readiness check failed: {e}",
                                    "fix": "", "tools": [], "jobs": []})
            out[p.id] = {"ok": not missing, "missing": missing}
        return out

    def blocked(self, plugin_id: str, capture_id: int | None,
                job: str = "", tool: str = "") -> str:
        """Why this job or tool cannot run here, or "" if it can."""
        state = self.readiness(capture_id).get(plugin_id)
        if state is None or state["ok"]:
            return ""
        for m in state["missing"]:
            if job and m["jobs"] and job not in m["jobs"]:
                continue
            if tool and m["tools"] and tool not in m["tools"]:
                continue
            if not job and not tool and (m["jobs"] or m["tools"]):
                continue
            fix = f" {m['fix']}" if m["fix"] else ""
            return f"{m['why']}.{fix}".strip()
        return ""

    # -- stream clocks -------------------------------------------------------
    # The offset is an op, folded last-write-wins, so it is attributed, shows
    # up in History and comes back with Ctrl+Z. `frame_map` is a cache of the
    # fold, not a fact: nothing here is state the log cannot rebuild.
    def stream_offsets(self, capture_id: int) -> dict[str, float]:
        out: dict[str, float] = {}
        for op in self.ops_since(capture_id, 0):
            if op["kind"] != "core.stream_offset":
                continue
            p = op["payload"] or {}
            key = str(p.get("stream", ""))
            if key:
                out[key] = float(p.get("seconds", 0.0))
        return out

    def apply_stream_offsets(self, capture_id: int) -> dict[str, float]:
        """Fold the offset ops onto the streams and rebuild their frame maps."""
        want = self.stream_offsets(capture_id)
        cap = self.db.one("SELECT n_frames, fps FROM captures WHERE id=?", capture_id)
        if cap is None:
            return {}
        applied: dict[str, float] = {}
        for st in self.db.all("SELECT * FROM streams WHERE capture_id=?", capture_id):
            off = want.get(st["key"], 0.0)
            stamps = probe.unpack(st["timestamps"])
            if stamps is None:
                # No timestamps means no map to rebuild. Say so rather than
                # inventing one: a plausible wrong frame map puts every mask on
                # the wrong picture with nothing on screen to admit it.
                if off != st["time_offset"]:
                    self.db.run("UPDATE streams SET time_offset=? WHERE id=?", off, st["id"])
                applied[st["key"]] = off
                continue
            fm = probe.frame_map(stamps, cap["n_frames"], cap["fps"], off)
            self.db.run("UPDATE streams SET time_offset=?, frame_map=? WHERE id=?",
                        off, fm.tobytes(), st["id"])
            applied[st["key"]] = off
        self.publish(capture_id, {"t": "streams", "capture_id": capture_id, "offsets": applied})
        return applied

    # -- assets --------------------------------------------------------------
    def upload_path(self, asset_id: int, name: str) -> Path:
        """Where an upload lands. The name on disk is ours, never the client's:
        an id plus a sanitised suffix, so a crafted filename cannot escape the
        directory or collide with somebody else's file."""
        safe = "".join(ch for ch in Path(name).name if ch.isalnum() or ch in "._- ")[-80:]
        d = self.cfg.upload_dir
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{asset_id:06d}_{safe or 'file'}"

    def run_startup(self) -> None:
        for p in self.plugins.values():
            for fn in p.on_start:
                fn(Ctx(self, p))

    def run_cleanup(self, capture_id: int) -> list[str]:
        """Tell every plugin a capture is going away. One that fails is
        reported, never allowed to block the delete — a half-deleted capture is
        worse than a stray file."""
        problems = []
        for p in self.plugins.values():
            for fn in p.on_delete:
                try:
                    fn(Ctx(self, p), capture_id)
                except Exception as e:
                    problems.append(f"{p.id}: {e}")
        return problems

    # -- ops -----------------------------------------------------------------
    def append_op(self, capture_id: int, kind: str, payload: dict,
                  actor: str, layer_id: int | None = None,
                  transient: dict | None = None) -> dict:
        oid = self.db.insert("ops", capture_id=capture_id, layer_id=layer_id, actor=actor,
                             ts=now(), kind=kind, payload=json.dumps(payload))
        op = {"id": oid, "capture_id": capture_id, "layer_id": layer_id, "actor": actor,
              "ts": now(), "kind": kind, "payload": payload, "undone": 0}
        # A caller may attach delivery-only information (for example, the
        # browser request that originated an edit).  It is deliberately not
        # stored in the op log: other clients need it only to recognise the
        # initial WebSocket echo, never to replay history.
        if transient:
            op.update(transient)
        self.publish(capture_id, {"t": "op", "op": op})
        return op

    def ops_since(self, capture_id: int, since: int = 0, limit: int = 5000,
                  include_undone: bool = False) -> list[dict]:
        """Undone ops are excluded by default, which is what makes undo work for
        every plugin whose state is a fold: nothing has to be inverted, the fold
        simply stops being told about the op."""
        sql = "SELECT * FROM ops WHERE capture_id=? AND id>?"
        if not include_undone:
            sql += " AND undone=0"
        rows = self.db.all(sql + " ORDER BY id LIMIT ?", capture_id, since, limit)
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def set_undone(self, capture_id: int, op_id: int, undone: bool, actor: str) -> dict:
        """Mark one op undone (or done again) and tell the room. An op is never
        deleted — who did what, and who took it back, are both part of the log."""
        r = self.db.one("SELECT * FROM ops WHERE id=? AND capture_id=?", op_id, capture_id)
        if r is None:
            raise KeyError(f"no op {op_id} in capture {capture_id}")
        self.db.run("UPDATE ops SET undone=? WHERE id=?", 1 if undone else 0, op_id)
        op = dict(r)
        op["payload"] = json.loads(op["payload"])
        op["undone"] = 1 if undone else 0
        # The core's own ops are the only ones it folds. Undoing a clock
        # correction has to actually move the pictures back, or "Ctrl+Z" would
        # be a lie for the one op kind the core owns.
        if op["kind"].startswith("core."):
            self.apply_stream_offsets(capture_id)
        self.publish(capture_id, {"t": "op", "op": op, "undo": True, "by": actor})
        return op

    def last_op_of(self, capture_id: int, actor: str, prefix: str = "") -> dict | None:
        sql = "SELECT * FROM ops WHERE capture_id=? AND actor=? AND undone=0"
        args = [capture_id, actor]
        if prefix:
            sql += " AND kind LIKE ?"
            args.append(prefix + "%")
        r = self.db.one(sql + " ORDER BY id DESC LIMIT 1", *args)
        if r is None:
            return None
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        return d

    def validate_op(self, capture_id: int, kind: str, payload: dict) -> str | None:
        """Ask the plugin that owns this op kind whether it still applies.

        Returns None when it does, or a human-readable reason when it does not.
        A plugin that registers no validator is taken at its word.
        """
        ns = str(kind).split(".", 1)[0]
        p = self.plugins.get(ns)
        fn = (p.validators.get(kind) if p else None)
        if fn is None:
            return None
        try:
            return fn(self, capture_id, payload or {})
        except Exception as e:                  # a broken validator must not block work
            return None

    # -- realtime ------------------------------------------------------------
    def publish(self, capture_id, msg: dict) -> None:
        self.hub.publish(capture_id, msg)
