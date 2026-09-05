"""CLI: serve, plugins, ingest, token."""
from __future__ import annotations

import argparse
import json
import secrets
import sys

from .config import Config
from .db import now


def main(argv=None):
    ap = argparse.ArgumentParser("quorum")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the server")
    s.add_argument("--host"), s.add_argument("--port", type=int)
    s.add_argument("--reload", action="store_true")

    sub.add_parser("plugins", help="list discovered plugins")

    g = sub.add_parser("ingest", help="run a capture provider without the UI")
    g.add_argument("provider", help="<plugin>.<provider id>, e.g. grid_capture.prepared")
    g.add_argument("params", nargs="*", help="key=value")

    j = sub.add_parser("job", help="run any plugin job and wait")
    j.add_argument("job", help="<plugin>.<job kind>, e.g. grid_capture.import:tracks")
    j.add_argument("params", nargs="*", help="key=value")

    u = sub.add_parser("user", help="create a user and print a token")
    u.add_argument("name"), u.add_argument("--role", default="annotator")

    args = ap.parse_args(argv)
    cfg = Config.load()

    if args.cmd == "serve":
        import uvicorn
        if args.host: cfg.host = args.host
        if args.port: cfg.port = args.port
        print(f"quorum → http://{cfg.host}:{cfg.port}  (auth: {cfg.auth_mode})")
        uvicorn.run("quorum.api.app:create_app", factory=True, host=cfg.host, port=cfg.port,
                    reload=args.reload, log_level="info",
                    # Opening a capture is a dozen requests, and on the GPU box
                    # this server is reached over `ssh -L`, where a TCP handshake
                    # plus TLS-less setup is a round trip of its own. uvicorn's
                    # default is to hang up after 5 idle seconds, which on a
                    # ~700 ms link means a person who reads a panel for six
                    # seconds pays a new connection for their next click. Keeping
                    # idle connections for a minute and a bit costs a socket and
                    # removes that entirely.
                    timeout_keep_alive=75,
                    # A forwarded port does not report its own death, so the
                    # server has to notice on its own — otherwise a room fills
                    # with connections belonging to tabs that are long gone, and
                    # presence lists everybody twice.
                    ws_ping_interval=20, ws_ping_timeout=20)
        return 0

    from .host import Host
    host = Host(cfg)

    if args.cmd == "plugins":
        for p in host.plugins.values():
            m = p.manifest()
            prov = m["provides"]
            print(f"{p.id:<14} {p.version:<8} {p.name}")
            for k in ("tools", "layerTypes", "jobs", "providers", "io"):
                if prov[k]:
                    print(f"  {k:<11} " + ", ".join(x.get("id", x.get("kind", "?")) for x in prov[k]))
        for bad in host.plugin_problems:
            print("FAILED", bad, file=sys.stderr)
        return 0

    if args.cmd in ("ingest", "job"):
        spec = args.provider if args.cmd == "ingest" else args.job
        pid, _, kind = spec.partition(".")
        if args.cmd == "ingest":
            kind = f"provider:{kind}"
        params = {}
        for kv in args.params:
            k, _, v = kv.partition("=")
            try:
                params[k] = json.loads(v)
            except json.JSONDecodeError:
                params[k] = v
        job = host.jobs.submit(pid, kind, params, params.get("capture_id"), "cli")
        import time
        while True:
            j = host.jobs.get(job["id"])
            print(f"\r{j['state']:<9} {j['progress']*100:5.1f}%  {j['message'][:70]:<70}", end="")
            if j["state"] in ("done", "failed", "cancelled"):
                print()
                print(json.dumps(j["result"], indent=2)[:2000])
                return 0 if j["state"] == "done" else 1
            time.sleep(0.3)

    if args.cmd == "user":
        tok = secrets.token_urlsafe(24)
        host.db.insert("users", name=args.name, display=args.name, role=args.role,
                       token=tok, created=now())
        print(f"{args.name} ({args.role})\ntoken: {tok}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
