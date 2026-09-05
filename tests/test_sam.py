"""Tests for the SAM plugin: the codec, the frame, the prompt, the propagation.

The one that matters most is `test_a_frame_is_fetched_by_timestamp_not_by_index`.
Everything else here would fail loudly; that one fails *quietly* — a mask
segmented on the wrong picture looks entirely plausible and there is nothing on
screen to say otherwise. So it builds a genuinely variable-rate clip whose
frames are distinguishable by eye and by arithmetic, and asks for frames by
number.

Run with `.venv/bin/python tests/test_sam.py`, or with pytest.
"""
import atexit
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quorum import probe                   # noqa: E402
from quorum.config import Config           # noqa: E402
from quorum.host import Host               # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


C = _load("sam_client", "plugins/sam/client.py")
F = _load("sam_frames", "plugins/sam/frames.py")
A = _load("sam_auto", "plugins/sam/auto.py")
CODEC = _load("mask_codec_t", "plugins/mask_layer/plugin.py")


# --------------------------------------------------------------------- codec
def test_the_rle_survives_a_round_trip_through_sams_shape():
    """SAM's `[runs…, xmin, ymin, xmax, ymax]` and the layer's `{box, rle}` are
    the same runs with a different tail. Re-encoding either would be a bug: the
    round trip through CVAT is lossless precisely because nobody re-encodes."""
    payload = {"box": [10, 20, 4, 3], "rle": "2,5,1,4"}
    shape = C.payload_to_sam(payload)
    assert shape["type"] == "mask"
    assert shape["points"][-4:] == [10, 20, 13, 22], "inclusive box, as SAM writes it"
    assert C.sam_to_payload(shape["points"]) == payload


def test_an_empty_answer_is_an_answer_and_not_a_crash():
    assert C.sam_to_payload([]) is None
    assert C.sam_to_payload(None) is None
    assert C.sam_to_payload([1, 2]) is None, "too short to carry a box"


def test_a_crop_is_put_back_by_moving_the_box_and_nothing_else():
    p = {"box": [3, 4, 8, 9], "rle": "1,2,3"}
    out = C.shift(p, 100, 200)
    assert out == {"box": [103, 204, 8, 9], "rle": "1,2,3"}
    assert C.shift(None, 5, 5) is None


def test_the_runs_mean_the_same_thing_to_both_halves():
    """The proof that borrowing `mask_layer`'s codec is legitimate: a mask that
    came out of SAM decodes to the pixels the renderer will paint."""
    import numpy as np
    mask = np.zeros((3, 4), dtype=bool)
    mask[1, 1:3] = True
    rle = CODEC.encode_rle(mask)
    shape = C.payload_to_sam({"box": [0, 0, 4, 3], "rle": rle})
    back = C.sam_to_payload(shape["points"])
    assert (CODEC.decode_rle(back["rle"], 4, 3) == mask).all()


# ----------------------------------------------------------------------- roi
def test_a_small_object_is_cropped_and_a_large_one_is_left_alone():
    small = F.roi_for([[960, 540]], None, 1920, 1080)
    assert small is not None and small[2] >= 224, "SAM squashes its input; a tiny person needs a crop"
    assert small[0] <= 960 <= small[0] + small[2]
    assert small[2] % 2 == 0 and small[3] % 2 == 0, "yuv420p wants even sides"
    big = F.roi_for([[100, 100], [1800, 1000]], None, 1920, 1080)
    assert big is None, "cropping something that already fills the frame only loses context"


def test_a_crop_never_leaves_the_frame():
    for pt in ([[5, 5]], [[1915, 1075]], [[0, 1079]]):
        roi = F.roi_for(pt, None, 1920, 1080)
        if roi is None:
            continue
        x, y, w, h = roi
        assert x >= 0 and y >= 0 and x + w <= 1920 and y + h <= 1080, roi


def test_a_frame_number_outside_the_stream_is_refused_rather_than_clamped():
    import numpy as np
    stamps = np.asarray([0, 40000, 80000], dtype="<i8")
    assert F.times_of(stamps, [1]) == [0.04]
    for bad in (-1, 3, 99):
        try:
            F.times_of(stamps, [bad])
        except F.NoFrame:
            continue
        raise AssertionError(f"frame {bad} should be refused; a clamp is a wrong picture "
                             f"that looks like a right one")


# -------------------------------------------------------------------- frames
GREY = [40 + 7 * n for n in range(24)]          # the luma written into frame n


_VFR: list = []


def _make_vfr(_ignored=None) -> Path:
    """A 24-frame clip with irregular frame intervals and one flat grey per
    frame, so "which frame did I get" has an arithmetic answer.

    Built once per process and reused: it costs 25 ffmpeg invocations, and
    building it per test took this suite from 2 s to 15 s — which matters,
    because `mutants.py` runs the whole suite once per mutation and a suite
    nobody waits for is a suite nobody runs.
    """
    if _VFR:
        return _VFR[0]
    keep = tempfile.mkdtemp(prefix="quorum-vfr-")
    atexit.register(shutil.rmtree, keep, True)
    dirpath = Path(keep)
    lines = []
    for n, v in enumerate(GREY):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", f"color=c=gray:s=64x64:d=1,geq=lum={v}:cb=128:cr=128",
                        "-frames:v", "1", str(dirpath / f"f{n:02d}.png")], check=True)
        lines.append(f"file 'f{n:02d}.png'\nduration {0.05 + (n % 5) * 0.03:.3f}")
    lines.append(f"file 'f{len(GREY) - 1:02d}.png'")
    (dirpath / "list.txt").write_text("\n".join(lines) + "\n")
    out = dirpath / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(dirpath / "list.txt"), "-c:v", "libx264", "-crf", "0",
                    "-pix_fmt", "yuv420p", "-fps_mode", "passthrough", str(out)], check=True)
    _VFR.append(out)
    return out


def _luma(jpeg: bytes) -> int:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-vf", "scale=1:1",
                        "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                       input=jpeg, capture_output=True)
    return r.stdout[0]


def _expected(n: int) -> float:
    """What frame n's luma comes back as. The encode is limited-range and the
    decode expands it, so this is the mapping rather than the number written."""
    return max(0.0, min(255.0, (GREY[n] - 16) * 255 / 219))


def test_a_frame_is_fetched_by_timestamp_not_by_index():
    """The whole reason `frames.py` exists.

    These recordings are variable-rate. `n / fps` is not frame n's time — on
    this clip frame 11 is at 1.16 s while `11 / 24 fps` would say 0.46 s, four
    frames earlier. Asking by timestamp lands on the frame the mask belongs to;
    anything else lands on a different person doing something else.
    """
    with tempfile.TemporaryDirectory() as tmp:
        v = _make_vfr(Path(tmp))
        stamps = probe.timestamps(v)
        assert len(stamps) >= len(GREY)
        gaps = {int(stamps[i + 1] - stamps[i]) for i in range(len(GREY) - 1)}
        assert len(gaps) > 1, "this fixture is supposed to be variable-rate"
        for n in (0, 3, 11, 17, 23):
            got = _luma(F.one(v, F.times_of(stamps, [n])[0]))
            assert abs(got - _expected(n)) <= 4, \
                f"asked for frame {n} (luma ~{_expected(n):.0f}) and got luma {got}"


def test_a_run_of_frames_comes_back_in_order_and_complete():
    with tempfile.TemporaryDirectory() as tmp:
        v = _make_vfr(Path(tmp))
        stamps = probe.timestamps(v)
        want = [2, 5, 9, 14, 20]
        jpegs = F.many(v, F.times_of(stamps, want))
        assert len(jpegs) == len(want)
        for n, j in zip(want, jpegs):
            got = _luma(j)
            assert abs(got - _expected(n)) <= 4, f"frame {n}: luma {got}, wanted {_expected(n):.0f}"


def test_one_frame_and_a_run_of_one_agree():
    """`many` delegates a single frame to `one`; if the two paths ever disagree
    a propagation of length 1 would seed itself from a different picture."""
    with tempfile.TemporaryDirectory() as tmp:
        v = _make_vfr(Path(tmp))
        stamps = probe.timestamps(v)
        t = F.times_of(stamps, [7])
        assert _luma(F.one(v, t[0])) == _luma(F.many(v, t)[0])


def test_a_crop_reaches_the_extractor():
    with tempfile.TemporaryDirectory() as tmp:
        v = _make_vfr(Path(tmp))
        stamps = probe.timestamps(v)
        jpeg = F.one(v, F.times_of(stamps, [6])[0], roi=[0, 0, 32, 32])
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "csv=p=0", "-"],
                           input=jpeg, capture_output=True)
        assert r.stdout.decode().strip().startswith("32,32"), r.stdout


# --------------------------------------------------------------- the endpoint
class _Fake(BaseHTTPRequestHandler):
    """Stands in for the nuclio function. Answers the two shapes it answers."""
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"] or 0)) or b"{}")
        _Fake.seen.append(body)
        if not body:
            self.send_error(400, "empty")
            return
        one = {"type": "mask", "points": [0, 4, 4, 4, 1, 2, 4, 4]}   # a 4x3 box at (1,2)
        if "images" in body:
            out = {"shapes": [[one] * len(body["images"]) for _ in body["shapes"]],
                   "states": [None] * len(body["shapes"])}
        else:
            out = {"shapes": [one]}
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def _serve():
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


def _stop(srv):
    """`shutdown` only stops the loop; the socket stays bound until it is
    closed, and a test that then expects "nothing is listening" would find
    something listening."""
    srv.shutdown()
    srv.server_close()


def test_the_interactor_and_the_tracker_speak_the_protocol():
    srv, url = _serve()
    try:
        _Fake.seen.clear()
        sam = C.Sam(url, timeout=10)
        got = sam.interact(b"jpeg-bytes", pos=[[3, 4]], neg=[[9, 9]], bbox=[1, 2, 3, 4])
        assert got == {"box": [1, 2, 4, 3], "rle": "0,4,4,4"}
        sent = _Fake.seen[-1]
        assert sent["pos_points"] == [[3, 4]] and sent["neg_points"] == [[9, 9]]
        assert sent["obj_bbox"] == [1, 2, 3, 4]
        assert "image" in sent and "images" not in sent

        per_obj = sam.track([b"a", b"b", b"c"], [{"box": [0, 0, 2, 2], "rle": "0,4"}])
        assert len(per_obj) == 1 and len(per_obj[0]) == 3
        sent = _Fake.seen[-1]
        assert len(sent["images"]) == 3 and sent["states"] == [None]
        assert sent["shapes"][0]["type"] == "mask"
    finally:
        _stop(srv)


def test_a_running_endpoint_that_refuses_an_empty_post_still_counts_as_alive():
    """`alive` must not need the GPU. The function refusing nonsense *is* the
    proof it is there; only a transport failure means it is not."""
    srv, url = _serve()
    try:
        assert C.Sam(url).alive()[0] is True
    finally:
        _stop(srv)
    ok, why = C.Sam(url, timeout=2).alive(timeout=1)
    assert ok is False and "cannot reach" in why


def test_no_endpoint_configured_says_what_to_set():
    ok, why = C.Sam("").alive()
    assert ok is False
    try:
        C.Sam("").interact(b"x", pos=[[1, 1]])
    except C.SamError as e:
        assert "quorum.toml" in str(e) and "SAM_URL" in str(e)
        return
    raise AssertionError("an unconfigured endpoint must refuse, and say how to configure it")


# ----------------------------------------------------------- the auto-annotator
def test_prompts_and_labels_pair_by_position_and_refuse_to_collide():
    assert A.categories("person", "") == [("person", "person")]
    assert A.categories("hat, glove\nphone", "") == \
        [("hat", "hat"), ("glove", "glove"), ("phone", "phone")]
    assert A.categories("person sitting down", "customer") == [("person sitting down", "customer")]
    for prompts, labels, why in [
        ("", "", "no prompt at all"),
        ("hat, glove", "hat", "fewer labels than prompts"),
        ("hat, cap", "ppe, ppe", "two categories sharing one label"),
    ]:
        try:
            A.categories(prompts, labels)
        except ValueError:
            continue
        raise AssertionError(f"{why} should be refused before the GPU time is spent")


def test_the_flags_are_the_tuned_ones_and_blanks_are_left_out():
    args = A.build_args([("hat", "ppe_hat"), ("glove", "ppe_glove")],
                        {"chunk_frames": 64, "frame_stride": 5, "max_resolution": "",
                         "freeze_gap_ms": 400.0})
    assert args[:8] == ["--text_prompt", "hat", "--text_prompt", "glove",
                        "--label_name", "ppe_hat", "--label_name", "ppe_glove"], args
    assert "--chunk_frames" in args and args[args.index("--chunk_frames") + 1] == "64"
    assert "--max_resolution" not in args, "a blank field must not become --max_resolution ''"
    assert args[args.index("--freeze_gap_ms") + 1] == "400.0"


class _Sink:
    """Enough of a JobContext for the progress parser."""
    def __init__(self):
        self.cancelled = False
        self.lines = []

    def log(self, line):
        self.lines.append(line)

    def check(self):
        pass


def test_progress_comes_out_of_the_pipelines_own_output():
    """Unchanged when the transport moved from a subprocess pipe to HTTP —
    which is the point: the pipeline still prints these lines, and running it
    inside the model's process did not change what it says."""
    r = A.Run(_Sink(), "http://sam", {})
    seen = []
    for line in [
        "Video loaded: 9002 frames, 1920x1080, 15.00 fps",
        "=== Processing Window: Global Frames 0 to 63 ===",
        " [FREEZE] 1.20s of wall-clock missing between frames 120 and 121",
        "=== Processing Window: Global Frames 4500 to 4563 ===",
        " !!! PARTIAL CHUNK: only 12/64 frames",
        "Tracks found: 31 hat, 12 glove",
    ]:
        r._line(line, lambda f, m: seen.append((round(f, 3), m)))
    assert r.total == 9002
    assert seen[0][0] == 0.0
    assert seen[1] == (0.007, "frames 0–63 of 9002")
    assert seen[2][0] == round(4563 / 9002, 3)
    assert r.tracks == "31 hat, 12 glove"
    assert any("PARTIAL CHUNK" in n for n in r.notes), "the range with no masks must be reported"
    assert any("FREEZE" in n for n in r.notes)


def test_the_categories_are_sent_as_fields_and_not_repeated_as_flags():
    """The service takes prompts and labels as their own fields and builds the
    two flags itself. Sending them in `flags` as well would give the pipeline
    four prompts where the person asked for two — a mistake that costs an hour
    of GPU before anyone notices."""
    cats = A.categories("hat, glove", "")
    flags = A.build_args(cats, {"chunk_frames": 64}, prompts=False)
    assert "--text_prompt" not in flags and "--label_name" not in flags, flags
    assert "--chunk_frames" in flags
    with_prompts = A.build_args(cats, {}, prompts=True)
    assert with_prompts.count("--text_prompt") == 2


def test_max_num_objects_is_not_offered_per_job():
    """It is a constructor argument of the predictor, and the predictor is built
    once when the service starts. Accepting it per job and ignoring it is worse
    than not accepting it."""
    flags = A.build_args(A.categories("person", ""), {"max_num_objects": 50})
    assert "--max_num_objects" not in flags


def test_an_unreachable_service_says_so_rather_than_failing_oddly():
    try:
        A._get("http://127.0.0.1:1/health", timeout=2)
    except A.NoService as e:
        assert "cannot reach the SAM service" in str(e)
        return
    raise AssertionError("an unreachable service must be reported as one")


def test_no_endpoint_configured_names_the_setting():
    import os
    old = os.environ.pop("SAM_URL", None)
    try:
        A._service(lambda k, d=None: "")
    except A.NoService as e:
        assert "quorum.toml" in str(e) and "SAM_URL" in str(e)
    else:
        raise AssertionError("an unconfigured service must refuse and say how to fix it")
    finally:
        if old is not None:
            os.environ["SAM_URL"] = old


# ---------------------------------------------------- propagation, end to end
def _host(tmp):
    cfg = Config()
    cfg.data_dir = Path(tmp) / "d"
    cfg.db_path = Path(tmp) / "t.db"
    cfg.blob_dir = Path(tmp) / "b"
    cfg.upload_dir = Path(tmp) / "u"
    cfg.cache_dir = Path(tmp) / "c"
    for d in (cfg.data_dir, cfg.blob_dir, cfg.upload_dir, cfg.cache_dir):
        d.mkdir(parents=True, exist_ok=True)
    cfg.plugin_paths = [ROOT / "plugins"]
    return Host(cfg)


def _capture(host, video: Path, stamps):
    from quorum.sdk import Writer
    cid = host.db.insert("captures", key="k", name="n", provider="t", config="{}",
                         layout="{}", n_frames=len(stamps), fps=10.0, created=0)
    w = Writer(host, host.plugins["masks"])
    sid = w.stream(cid, key="cam1", idx=0, width=64, height=64, n_frames=len(stamps),
                   media={"path": str(video), "kind": "video/mp4"}, timestamps=stamps)
    lay = w.layer(cid, key="masks", name="Masks", type="mask.rle", provenance="model")
    w.objects(lay, sid, [{"key": "7", "label": "person", "frames": [
        {"frame": 2, "outside": 0, "payload": {"box": [0, 0, 8, 8], "rle": "0,64"}},
        {"frame": 20, "outside": 1, "payload": {"box": [0, 0, 8, 8], "rle": "0,64"}}]}])
    return cid, lay


def test_a_propagation_is_one_op_that_writes_real_keyframes():
    """The behaviour the desktop tool's `R` had, plus the thing it did not: the
    whole propagation is a single op, so one Ctrl+Z takes all of it back."""
    from quorum.sdk import JobContext
    with tempfile.TemporaryDirectory() as tmp:
        video = _make_vfr(Path(tmp))
        stamps = probe.timestamps(video)[:24]
        host = _host(tmp)
        cid, lay = _capture(host, video, stamps)
        SAMP = sys.modules["quorum_plugins.sam"]
        M = sys.modules[host.plugins["masks"].module_name]

        srv, url = _serve()
        real = SAMP.endpoint
        SAMP.endpoint = lambda _p: C.Sam(url, timeout=20)
        try:
            ctx = JobContext(host, host.plugins["sam"], "jid", {
                "capture_id": cid, "stream": "cam1", "key": "7", "frame": 2,
                "count": 5, "stride": 1}, cid, "tester")
            out = SAMP.track(ctx)
        finally:
            SAMP.endpoint = real
            _stop(srv)

        assert out["written"] == 5, out
        ops = [o for o in host.ops_since(cid, 0) if o["kind"] == "masks.keyframes"]
        assert len(ops) == 1, "a propagation is one decision and must be one op"
        assert [f["frame"] for f in ops[0]["payload"]["frames"]] == [3, 4, 5, 6, 7]
        assert ops[0]["actor"] == "tester", "the person who pressed the key owns the edit"

        der = M.derived_layer(host.db, cid)
        rows = host.db.all(
            "SELECT sh.frame FROM shapes sh JOIN objects o ON o.id=sh.object_id "
            "WHERE o.layer_id=? AND o.key='7' ORDER BY sh.frame", der["id"])
        assert {r["frame"] for r in rows} >= {2, 3, 4, 5, 6, 7}, \
            "the propagated frames must be materialised, not merely logged"

        # …and taking it back leaves exactly what was imported.
        host.set_undone(cid, ops[0]["id"], True, "tester")
        M.materialise(host, cid)
        rows = host.db.all("SELECT sh.frame FROM shapes sh JOIN objects o ON o.id=sh.object_id "
                           "WHERE o.layer_id=? AND o.key='7'", M.derived_layer(host.db, cid)["id"])
        assert not rows, "one undo must take the whole propagation back"


def test_the_seed_frame_the_person_accepted_is_not_overwritten():
    """SAM re-segments the frame it was seeded on. Quietly replacing the mask a
    human looked at and approved is how a tool loses the trust that made them
    press the key."""
    from quorum.sdk import JobContext
    with tempfile.TemporaryDirectory() as tmp:
        video = _make_vfr(Path(tmp))
        host = _host(tmp)
        cid, lay = _capture(host, video, probe.timestamps(video)[:24])
        SAMP = sys.modules["quorum_plugins.sam"]
        srv, url = _serve()
        real = SAMP.endpoint
        SAMP.endpoint = lambda _p: C.Sam(url, timeout=20)
        try:
            SAMP.track(JobContext(host, host.plugins["sam"], "j2", {
                "capture_id": cid, "stream": "cam1", "key": "7", "frame": 2,
                "count": 3}, cid, "tester"))
        finally:
            SAMP.endpoint = real
            _stop(srv)
        op = [o for o in host.ops_since(cid, 0) if o["kind"] == "masks.keyframes"][0]
        assert 2 not in [f["frame"] for f in op["payload"]["frames"]]


def test_propagating_a_track_with_no_mask_here_is_refused_with_a_reason():
    from fastapi import HTTPException
    from quorum.sdk import JobContext
    with tempfile.TemporaryDirectory() as tmp:
        video = _make_vfr(Path(tmp))
        host = _host(tmp)
        cid, _ = _capture(host, video, probe.timestamps(video)[:24])
        SAMP = sys.modules["quorum_plugins.sam"]
        try:
            SAMP.track(JobContext(host, host.plugins["sam"], "j3", {
                "capture_id": cid, "stream": "cam1", "key": "7", "frame": 22,
                "count": 2}, cid, "tester"))
        except HTTPException as e:
            assert "no mask at frame" in e.detail, e.detail
            return
        raise AssertionError("propagating from a frame where the track is outside must refuse")


def test_a_new_track_key_is_free_in_every_layer_and_in_the_log():
    class _Req:
        def __init__(self, host):
            self.app = type("a", (), {"state": type("s", (), {"host": host})()})()
    with tempfile.TemporaryDirectory() as tmp:
        video = _make_vfr(Path(tmp))
        host = _host(tmp)
        cid, _ = _capture(host, video, probe.timestamps(video)[:24])
        SAMP = sys.modules["quorum_plugins.sam"]
        req = _Req(host)
        assert SAMP.newkey(cid, req, "cam1", {"name": "t"})["key"] == "sam1"
        host.append_op(cid, "masks.create", {"stream": "cam1", "key": "sam1", "frames": []},
                       "tester")
        assert SAMP.newkey(cid, req, "cam1", {"name": "t"})["key"] == "sam2", \
            "a key claimed by an op nobody has materialised yet is still taken"
        assert SAMP.newkey(cid, req, "cam2", {"name": "t"})["key"] == "sam1", \
            "keys are per stream"


# ---------------------------------------------------------------------- main
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
