"""Background jobs.

The core owns the queue, the state machine and cancellation; plugins own what
a job actually does. Long work belongs here because the alternative — doing it
inside a request — is how annotation servers get a reputation for hanging.
"""
from __future__ import annotations

import json
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

from .db import Database, now
from .sdk import Cancelled, JobContext


class Blocked(Exception):
    """A job whose plugin's requirements are not met on this capture. Surfaced
    to the client as 409 with the reason and the fix."""


class JobRunner:
    def __init__(self, host, workers: int = 2):
        self.host = host
        self.db: Database = host.db
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="job")
        self.live: dict[str, JobContext] = {}
        self.lock = threading.Lock()

    # -- api -----------------------------------------------------------------
    def submit(self, plugin_id: str, kind: str, params: dict,
               capture_id: int | None, actor: str) -> dict:
        plugin = self.host.plugins.get(plugin_id)
        if plugin is None or kind not in plugin.jobs:
            raise KeyError(f"no job {plugin_id}.{kind}")
        # A plugin that declared what it cannot work without does not get to
        # half-run here. The greyed-out button in the UI is a courtesy; this is
        # the guarantee, and it holds for the CLI and for another plugin too.
        blocked = self.host.blocked(plugin_id, capture_id, job=kind)
        if blocked:
            raise Blocked(f"{plugin_id}.{kind} cannot run on this capture: {blocked}")
        jid = uuid.uuid4().hex[:12]
        t = now()
        self.db.insert("jobs", id=jid, capture_id=capture_id, plugin=plugin_id, kind=kind,
                       params=json.dumps(params), state="queued", progress=0.0, message="",
                       result="{}", actor=actor, created=t, updated=t)
        ctx = JobContext(self.host, plugin, jid, params, capture_id, actor)
        with self.lock:
            self.live[jid] = ctx
        self.pool.submit(self._run, ctx, plugin.jobs[kind])
        job = self.get(jid)
        self.host.publish(capture_id, {"t": "job", "job": job})
        return job

    def cancel(self, jid: str) -> bool:
        with self.lock:
            ctx = self.live.get(jid)
        if ctx is None:
            return False
        ctx.cancel()
        self.update(jid, message="cancelling…")
        return True

    def get(self, jid: str) -> dict | None:
        r = self.db.one("SELECT * FROM jobs WHERE id=?", jid)
        if r is None:
            return None
        d = dict(r)
        d["params"] = json.loads(d["params"])
        d["result"] = json.loads(d["result"])
        return d

    def list(self, capture_id: int | None = None, limit: int = 50) -> list[dict]:
        if capture_id is None:
            rows = self.db.all("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", limit)
        else:
            rows = self.db.all("SELECT * FROM jobs WHERE capture_id=? ORDER BY created DESC LIMIT ?",
                               capture_id, limit)
        out = []
        for r in rows:
            d = dict(r)
            d["params"] = json.loads(d["params"])
            d["result"] = json.loads(d["result"])
            out.append(d)
        return out

    # -- internals -----------------------------------------------------------
    def update(self, jid: str, **cols) -> None:
        cols["updated"] = now()
        sets = ",".join(f"{k}=?" for k in cols)
        self.db.run(f"UPDATE jobs SET {sets} WHERE id=?", *cols.values(), jid)
        job = self.get(jid)
        if job:
            self.host.publish(job["capture_id"], {"t": "job", "job": job})

    def _run(self, ctx: JobContext, fn) -> None:
        self.update(ctx.job_id, state="running", message="started")
        try:
            result = fn(ctx) or {}
            self.update(ctx.job_id, state="done", progress=1.0,
                        result=json.dumps(result), message=result.get("message", "done"))
        except Cancelled:
            self.update(ctx.job_id, state="cancelled", message="cancelled")
        except Exception as e:
            traceback.print_exc()
            self.update(ctx.job_id, state="failed",
                        message=f"{type(e).__name__}: {e}",
                        result=json.dumps({"error": str(e), "log": ctx.lines[-20:]}))
        finally:
            with self.lock:
                self.live.pop(ctx.job_id, None)
