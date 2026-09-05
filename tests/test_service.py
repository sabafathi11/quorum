"""The auto-annotator over HTTP, against a stub of the SAM service.

This replaces `test_docker.py`, which drove a real daemon because the job used
to start a container per stream. It does not any more — the pipeline runs
inside the service's own process, on the one predictor — so what has to be
checked is the *protocol*: a job starts, the pipeline's own stdout arrives and
becomes progress, a cancel stops it, and the XML that lands becomes tracks.

None of that needs docker or a GPU, so unlike its predecessor this runs in the
ordinary suite. What it cannot check is that the real service still speaks this
protocol; what it does check is that Quorum still understands it.

Run with `.venv/bin/python tests/test_service.py`, or with pytest.
"""
import json
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quorum.config import Config             # noqa: E402
from quorum.host import Host                 # noqa: E402
from quorum.sdk import Cancelled, JobContext  # noqa: E402

# The lines the pipeline actually prints, in the order it prints them. Copied
# from a real run's output on vm.3090 — if upstream stops printing them, this
# fixture is the record of what Quorum was written against.
SCRIPT = [
    "Tracking 1 category: 'person' -> person",
    "Video loaded: 600 frames, 1920x1080, 20.00 fps",
    "=== Processing Window: Global Frames 0 to 155 ===",
    "[person] Added text prompt 'person' at window start.",
    "[FREEZE] 2.00s of wall-clock missing between frames 200 and 201",
    "=== Processing Window: Global Frames 300 to 455 ===",
    " [Throughput] 608 frames in 155s (3.9 fps)",
    "=== Processing Window: Global Frames 460 to 599 ===",
    "1 discontinuity(ies): 0 repeated-frame stretch(es), 1 dropped-frame hole(s), 2.0s lost",
    "Tracks found: 3 person",
    "Done!",
]

XML = ('<?xml version="1.0" encoding="utf-8"?>\n<annotations>\n  <version>1.1</version>\n'
       '  <track id="0" label="person" source="auto">\n'
       '    <mask frame="0" keyframe="1" outside="0" occluded="0" rle="0,4,4,4" '
       'left="1" top="2" width="4" height="3" z_order="0"/>\n'
       '    <mask frame="300" keyframe="1" outside="0" occluded="0" rle="0,4,4,4" '
       'left="1" top="2" width="4" height="3" z_order="0"/>\n'
       '    <mask frame="599" keyframe="1" outside="1" occluded="0" rle="0,4,4,4" '
       'left="1" top="2" width="4" height="3" z_order="0"/>\n'
       '  </track>\n</annotations>\n')


class Stub(BaseHTTPRequestHandler):
    """Answers exactly what `plugins/sam/service/server.py` answers."""
    jobs: dict = {}
    delay = 0.05
    seen_requests: list = []

    def _send(self, obj, code=200):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            return self._send({"ok": True, "busy": False, "job": None,
                               "vram": {"used": 7152, "total": 24564, "free": 17412}})
        if path.startswith("/auto/"):
            j = Stub.jobs.get(path[len("/auto/"):])
            if j is None:
                return self._send({"error": "no such job"}, 404)
            return self._send({"id": j["id"], "state": j["state"], "error": j["error"],
                               "lines": j["lines"], "n_lines": len(j["lines"])})
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if path == "/auto":
            Stub.seen_requests.append(body)
            jid = f"j{len(Stub.jobs)}"
            job = {"id": jid, "state": "running", "lines": [], "error": "", "body": body}
            Stub.jobs[jid] = job
            threading.Thread(target=self._play, args=(job,), daemon=True).start()
            return self._send({"id": jid, "state": "running"})
        if path.startswith("/auto/") and path.endswith("/cancel"):
            j = Stub.jobs.get(path[len("/auto/"):-len("/cancel")])
            if j is None:
                return self._send({"error": "no such job"}, 404)
            j["cancel"] = True
            return self._send({"id": j["id"], "cancelling": True})
        self._send({"error": "not found"}, 404)

    def _play(self, job):
        for line in SCRIPT:
            if job.get("cancel"):
                job["state"] = "cancelled"
                return
            job["lines"].append(line)
            time.sleep(Stub.delay)
        Path(job["body"]["output"]).parent.mkdir(parents=True, exist_ok=True)
        Path(job["body"]["output"]).write_text(XML)
        job["state"] = "done"

    def log_message(self, *a):
        pass


def serve():
    Stub.jobs, Stub.seen_requests = {}, []
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


# ---------------------------------------------------------------- a capture
def _host(tmp, url):
    cfg = Config()
    cfg.data_dir = Path(tmp) / "d"
    cfg.db_path = Path(tmp) / "t.db"
    cfg.blob_dir = Path(tmp) / "b"
    cfg.upload_dir = Path(tmp) / "u"
    cfg.cache_dir = Path(tmp) / "c"
    for d in (cfg.data_dir, cfg.blob_dir, cfg.upload_dir, cfg.cache_dir):
        d.mkdir(parents=True, exist_ok=True)
    cfg.plugin_paths = [ROOT / "plugins"]
    cfg.plugins = {"sam": {"url": url}}
    return Host(cfg)


def _capture(host, video):
    from quorum.sdk import Writer
    cid = host.db.insert("captures", key="k", name="n", provider="t", config="{}",
                         layout="{}", n_frames=600, fps=20.0, created=0)
    w = Writer(host, host.plugins["sam"])
    w.stream(cid, key="cam1", idx=0, width=64, height=64, n_frames=600,
             media={"path": str(video), "kind": "video/mp4"})
    return cid


def _video(d: Path) -> Path:
    out = d / "cam1_test.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=size=64x64:rate=20:duration=1",
                    "-pix_fmt", "yuv420p", str(out)], check=True)
    return out


def _params(cid, **over):
    p = {"capture_id": cid, "prompts": "person", "labels": "",
         "streams": "cam1", "frame_stride": 5, "chunk_frames": 64,
         "layer": "sam_auto", "layer_name": "SAM auto",
         "import_result": True, "replace_layer": True, "force": True}
    p.update(over)
    return p


def _ctx(host, cid, jid="j", **over):
    import os
    os.environ.pop("SAM_URL", None)        # the config's url must be what is used
    return JobContext(host, host.plugins["sam"], jid, _params(cid, **over), cid, "tester")


# -------------------------------------------------------------------- tests
def test_a_run_reports_progress_and_its_tracks_become_a_layer():
    srv, url = serve()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            host = _host(tmp, url)
            cid = _capture(host, _video(Path(tmp)))
            SAMP = sys.modules["quorum_plugins.sam"]
            seen = []
            ctx = _ctx(host, cid)
            ctx.progress = lambda f, m="": seen.append((f, m))
            out = SAMP.auto_annotate(ctx)

            assert out["done"] == ["cam1"], out
            assert not out["failed"], out["failed"]
            fracs = [f for f, _ in seen]
            assert fracs == sorted(fracs), "progress must never go backwards"
            assert any("frames 460–599 of 600" in m for _, m in seen), seen
            assert out["tracks"] == 1 and out["keyframes"] == 3, out
            lay = host.db.one("SELECT * FROM layers WHERE capture_id=? AND key='sam_auto'", cid)
            assert lay["provenance"] == "model", \
                "work nobody looked at must be labelled as the model's"
            notes = out["streams"]["cam1"]["notes"]
            assert any("FREEZE" in n for n in notes), notes
            assert out["streams"]["cam1"]["tracks"] == "3 person"
    finally:
        srv.shutdown(); srv.server_close()


def test_the_categories_go_as_fields_not_as_duplicated_flags():
    """The bug this prevents costs an hour of GPU before anybody sees it: the
    pipeline would be given each prompt twice and track four categories."""
    srv, url = serve()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            host = _host(tmp, url)
            cid = _capture(host, _video(Path(tmp)))
            ctx = _ctx(host, cid, prompts="hat, glove", labels="ppe_hat, ppe_glove")
            ctx.progress = lambda f, m="": None
            sys.modules["quorum_plugins.sam"].auto_annotate(ctx)
            req = Stub.seen_requests[-1]
            assert req["prompts"] == ["hat", "glove"], req
            assert req["labels"] == ["ppe_hat", "ppe_glove"], req
            assert "--text_prompt" not in req["flags"], req["flags"]
            assert "--frame_stride" in req["flags"]
    finally:
        srv.shutdown(); srv.server_close()


def test_cancelling_reaches_the_service_and_the_job_says_cancelled():
    srv, url = serve()
    Stub.delay = 0.4                       # long enough to cancel mid-run
    try:
        with tempfile.TemporaryDirectory() as tmp:
            host = _host(tmp, url)
            cid = _capture(host, _video(Path(tmp)))
            ctx = _ctx(host, cid, jid="c1")
            ctx.progress = lambda f, m="": None
            threading.Timer(1.0, ctx.cancel).start()
            try:
                sys.modules["quorum_plugins.sam"].auto_annotate(ctx)
            except Cancelled:
                pass
            else:
                raise AssertionError("a cancelled run must raise, so the job reads cancelled")
            assert any(j.get("cancel") for j in Stub.jobs.values()), \
                "the cancel must reach the service, not just stop the poller"
    finally:
        Stub.delay = 0.05
        srv.shutdown(); srv.server_close()


def test_a_second_run_reuses_the_xml_unless_you_force_it():
    srv, url = serve()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            host = _host(tmp, url)
            cid = _capture(host, _video(Path(tmp)))
            SAMP = sys.modules["quorum_plugins.sam"]
            c1 = _ctx(host, cid, jid="a"); c1.progress = lambda f, m="": None
            SAMP.auto_annotate(c1)
            before = len(Stub.jobs)
            c2 = _ctx(host, cid, jid="b", force=False); c2.progress = lambda f, m="": None
            out = SAMP.auto_annotate(c2)
            assert out["skipped"] == ["cam1"] and out["done"] == [], out
            assert len(Stub.jobs) == before, "a resume must not ask the model to run again"
    finally:
        srv.shutdown(); srv.server_close()


def test_a_service_that_is_down_is_reported_as_the_one_thing_that_matters():
    srv, url = serve()
    srv.shutdown(); srv.server_close()          # nothing listening now
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp, url)
        cid = _capture(host, _video(Path(tmp)))
        ctx = _ctx(host, cid)
        ctx.progress = lambda f, m="": None
        try:
            sys.modules["quorum_plugins.sam"].auto_annotate(ctx)
        except Exception as e:
            assert "cannot reach the SAM service" in str(e), e
            return
        raise AssertionError("a dead service must be named, not silently produce nothing")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    bad = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as e:
            bad += 1
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - bad}/{len(tests)} passed")
    sys.exit(1 if bad else 0)
