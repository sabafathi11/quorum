"""Tests for the parts that would be expensive to get wrong.

Run with `.venv/bin/python tests/test_core.py` (no pytest needed) or with
pytest if you have it.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

from quorum.config import Config          # noqa: E402
from quorum.db import Database            # noqa: E402
from quorum.host import Host              # noqa: E402


# ------------------------------------------------------------------ identity
def _fold():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "identity_plugin", Path(__file__).resolve().parents[1] / "plugins/identity/plugin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def op(kind, **payload):
    return {"kind": kind, "payload": payload}


def test_fold_assign_and_clear():
    f = _fold().fold
    a = f([op("identity.assign", items=[["cam1", "7"], ["cam2", "3"]], cid=5)])
    assert a == {"cam1": {"7": 5}, "cam2": {"3": 5}}
    a = f([op("identity.assign", items=[["cam1", "7"]], cid=5),
           op("identity.clear", items=[["cam1", "7"]])])
    assert a == {}


def test_fold_link_allocates_then_reuses():
    f = _fold().fold
    a = f([op("identity.link", items=[["cam1", "1"], ["cam2", "2"]])])
    assert a["cam1"]["1"] == a["cam2"]["2"] == 1
    a = f([op("identity.link", items=[["cam1", "1"], ["cam2", "2"]]),
           op("identity.link", items=[["cam3", "9"], ["cam4", "4"]])])
    assert a["cam3"]["9"] == 2, "a second, unrelated link gets its own identity"


def test_fold_link_merges_two_existing_groups_whole():
    f = _fold().fold
    a = f([op("identity.assign", items=[["cam1", "1"], ["cam2", "2"]], cid=4),
           op("identity.assign", items=[["cam3", "3"], ["cam4", "4"]], cid=9),
           op("identity.link", items=[["cam1", "1"], ["cam3", "3"]])])
    assert set(v for m in a.values() for v in m.values()) == {4}, \
        "linking two groups re-labels every member, not just the two clicked"


def test_fold_outside_is_a_sentinel_not_an_identity():
    m = _fold()
    a = m.fold([op("identity.outside", items=[["cam1", "1"], ["cam1", "2"]]),
                op("identity.link", items=[["cam1", "1"], ["cam1", "2"]])])
    assert a["cam1"]["1"] == m.OUTSIDE, "linking two outside tracks must not invent an identity"
    a = m.fold([op("identity.outside", items=[["cam1", "1"]]),
                op("identity.assign", items=[["cam2", "2"]], cid=3),
                op("identity.link", items=[["cam1", "1"], ["cam2", "2"]])])
    assert a["cam1"]["1"] == 3, "a real identity wins over the outside flag"
    a = m.fold([op("identity.outside", items=[["cam1", "1"]]),
                op("identity.link", items=[["cam2", "2"], ["cam3", "3"]])])
    assert a["cam2"]["2"] == 1, "allocation never counts the outside sentinel"


def test_fold_outside_never_seeds_allocation_however_it_was_set():
    """The existing test covers `outside`; the sentinel can also arrive as an
    explicit `assign` to 200, and that path allocated 201 for the next person."""
    m = _fold()
    a = m.fold([op("identity.assign", items=[["cam1", "1"]], cid=m.OUTSIDE),
                op("identity.link", items=[["cam2", "2"], ["cam3", "3"]])])
    assert a["cam2"]["2"] == 1, \
        f"assigning the OUTSIDE sentinel must not push the next identity to {a['cam2']['2']}"
    a = m.fold([op("identity.assign", items=[["cam1", "1"]], cid=m.OUTSIDE),
                op("identity.assign", items=[["cam2", "2"]], cid=4),
                op("identity.link", items=[["cam3", "3"], ["cam4", "4"]])])
    assert a["cam3"]["3"] == 5, "allocation counts real identities and only those"


# ----------------------------------------------------------------- mask codec
def test_rle_roundtrip():
    import importlib.util
    import numpy as np
    spec = importlib.util.spec_from_file_location(
        "mask_plugin", Path(__file__).resolve().parents[1] / "plugins/mask_layer/plugin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rng = np.random.default_rng(7)
    for _ in range(20):
        w, h = int(rng.integers(3, 40)), int(rng.integers(3, 40))
        m = rng.random((h, w)) > 0.6
        assert (mod.decode_rle(mod.encode_rle(m), w, h) == m).all()
    assert mod.decode_rle("", 4, 3).sum() == 0


# ------------------------------------------------------------------ core db
def test_ops_are_append_only_and_ordered():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config()
        cfg.data_dir = Path(d); cfg.db_path = Path(d) / "t.db"; cfg.blob_dir = Path(d) / "b"
        cfg.plugin_paths = []
        host = Host(cfg)
        cid = host.db.insert("captures", key="k", name="n", provider="t", config="{}",
                             layout="{}", n_frames=10, fps=15, created=0)
        for i in range(5):
            host.append_op(cid, f"t.op{i}", {"i": i}, "tester")
        ops = host.ops_since(cid, 0)
        assert [o["payload"]["i"] for o in ops] == [0, 1, 2, 3, 4]
        assert host.ops_since(cid, ops[2]["id"]) == ops[3:]


def test_doc_store_rejects_a_stale_write():
    from quorum.sdk import Conflict, Ctx, Plugin
    with tempfile.TemporaryDirectory() as d:
        cfg = Config()
        cfg.data_dir = Path(d); cfg.db_path = Path(d) / "t.db"; cfg.blob_dir = Path(d) / "b"
        cfg.plugin_paths = []
        host = Host(cfg)
        cid = host.db.insert("captures", key="k", name="n", provider="t", config="{}",
                             layout="{}", n_frames=1, fps=1, created=0)
        p = Plugin(id="x", name="X")
        host.plugins["x"] = p
        ctx = Ctx(host, p)
        v = ctx.doc_put(cid, "q", {"a": 1})
        assert ctx.doc_get(cid, "q") == ({"a": 1}, v)
        try:
            ctx.doc_put(cid, "q", {"a": 2}, expect_version=v - 1)
        except Conflict:
            pass
        else:
            raise AssertionError("a write against a stale version must be refused")


# ---------------------------------------------------------------- the clock
# `frame_map` stopped being a fact and became a derivation, so the derivation
# is what has to be pinned. These reproduce, on a hand-written timeline, the
# rule `sync_id_ui/prepare.py` used — including which side of a boundary it
# lands on, which is the difference between a mask on the right picture and a
# mask one frame out.
def test_frame_map_takes_the_frame_most_recently_presented():
    import numpy as np
    from quorum import probe
    # a camera at 10 fps: frames at 0.0, 0.1, 0.2 …
    stamps = np.arange(10, dtype="<i8") * 100_000
    # a capture at 5 fps: 0.0, 0.2, 0.4 …
    fm = probe.frame_map(stamps, 5, 5.0)
    assert list(fm) == [0, 2, 4, 6, 8], list(fm)

    # exactly on a boundary belongs to the frame that starts there, not the one
    # before it: at t=0.1 the camera is showing frame 1, not frame 0
    fm = probe.frame_map(stamps, 3, 10.0)
    assert list(fm) == [0, 1, 2], list(fm)


def test_frame_map_offset_delays_a_camera_that_runs_ahead():
    import numpy as np
    from quorum import probe
    stamps = np.arange(20, dtype="<i8") * 100_000          # 10 fps
    none = probe.frame_map(stamps, 6, 5.0, 0.0)
    ahead = probe.frame_map(stamps, 6, 5.0, +0.2)          # its clock is 0.2 s fast
    behind = probe.frame_map(stamps, 6, 5.0, -0.2)
    # +ve means "this camera's events happen early", so it is read further back
    assert list(ahead) == [0, 0, 2, 4, 6, 8], list(ahead)
    assert list(behind) == [2, 4, 6, 8, 10, 12], list(behind)
    assert list(none) == [0, 2, 4, 6, 8, 10]


def test_frame_map_clamps_rather_than_running_off_the_end():
    import numpy as np
    from quorum import probe
    stamps = np.arange(3, dtype="<i8") * 100_000
    fm = probe.frame_map(stamps, 10, 10.0)                 # asks for more than exists
    assert list(fm) == [0, 1, 2, 2, 2, 2, 2, 2, 2, 2], list(fm)
    assert len(probe.frame_map(None, 4, 10.0)) == 4        # no timestamps: zeros, not a crash


# ------------------------------------------------------------- the filter
# One vocabulary, two implementations — the browser's and this one. If they
# disagree the walk offers what the user is hiding, which is the bug the whole
# display design exists to prevent.
def test_object_filter_round_trips_and_refuses_what_it_should():
    from quorum.filters import ObjectFilter
    f = ObjectFilter.parse('{"labels": {"exclude": ["forklift"]}, "layers": {"exclude": [7]}}')
    assert not f.empty
    assert f.allows(label="person", layer_id=3)
    assert not f.allows(label="forklift", layer_id=3)
    assert not f.allows(label="person", layer_id=7)
    assert ObjectFilter.parse(json.dumps(f.to_dict())).to_dict() == f.to_dict()

    where, args = f.sql("o", "l", "s")
    assert "o.label NOT IN" in where and "o.layer_id NOT IN" in where
    assert args == ["forklift", 7]


def test_a_broken_filter_shows_everything_rather_than_hiding_at_random():
    from quorum.filters import ObjectFilter
    for junk in (None, "", "not json", "[1,2,3]", '{"labels": 5}'):
        f = ObjectFilter.parse(junk)
        assert f.empty, junk
        assert f.allows(label="anything", layer_id=1)


# --------------------------------------------------------- renditions
# A rendition is a second copy of a camera's pixels. It is only usable if its
# frame N is the source's frame N — a rendition off by one looks perfectly
# fine and puts every mask one frame out, so `media.prepare` has to prove it
# rather than assume it.
def test_a_rendition_must_prove_it_is_frame_aligned():
    import importlib.util
    import numpy as np
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("_media", root / "plugins/media/plugin.py")
    media = importlib.util.module_from_spec(spec)
    sys.modules["_media"] = media
    spec.loader.exec_module(media)

    from quorum import probe
    src = np.arange(100, dtype="<i8") * 40_000                 # 25 fps

    calls = {}

    def fake(path):
        return calls[str(path)]

    real, probe.timestamps = probe.timestamps, fake
    media.probe.timestamps = fake
    try:
        calls["a"] = src
        calls["same"] = src.copy()
        assert media._alignment(Path("a"), Path("same")) == {
            "aligned": True, "t_offset": 0.0, "why": ""}

        calls["rebased"] = src + 120_000                       # a constant shift is fine…
        got = media._alignment(Path("a"), Path("rebased"))
        assert got["aligned"] and abs(got["t_offset"] - 0.12) < 1e-9, got

        calls["short"] = src[:-1].copy()                       # …a dropped frame is not
        got = media._alignment(Path("a"), Path("short"))
        assert not got["aligned"] and "99 frames out of 100" in got["why"], got

        wobbly = src.copy()
        wobbly[50] += 30_000                                   # …nor a wandering timeline
        calls["wobbly"] = wobbly
        got = media._alignment(Path("a"), Path("wobbly"))
        assert not got["aligned"] and "wander" in got["why"], got
    finally:
        probe.timestamps = real
        media.probe.timestamps = real


# ------------------------------------------------------------- requirements
# The greyed-out button in the UI is a courtesy. *This* is the guarantee, and
# it has to hold in-process, for the CLI and for another plugin, not only for
# the browser — so it is tested here rather than through the server.
def _host_with_real_plugins(d):
    cfg = Config.load()
    cfg.data_dir = Path(d); cfg.db_path = Path(d) / "t.db"; cfg.blob_dir = Path(d) / "b"
    cfg.upload_dir = Path(d) / "u"; cfg.cache_dir = Path(d) / "c"
    cfg.plugin_paths = [Path(__file__).resolve().parents[1] / "plugins"]
    for p in (cfg.blob_dir, cfg.upload_dir, cfg.cache_dir):
        p.mkdir(parents=True, exist_ok=True)
    return Host(cfg)


def test_a_job_is_refused_while_its_plugin_is_missing_what_it_declared():
    from quorum.jobs import Blocked
    with tempfile.TemporaryDirectory() as d:
        host = _host_with_real_plugins(d)
        if "voxel_carve" not in host.plugins:
            return                                  # not installed here; nothing to check
        cid = host.db.insert("captures", key="k", name="n", provider="t", config="{}",
                             layout="{}", n_frames=1, fps=1, created=0)
        assert host.readiness(cid)["voxel_carve"]["ok"] is False
        try:
            host.jobs.submit("voxel_carve", "suggest", {"capture_id": cid}, cid, "test")
        except Blocked as e:
            assert "calibration" in str(e), e
        else:
            raise AssertionError("a job ran without the data its plugin requires")

        # …and with the file present and valid, the gate opens
        host.db.insert("assets", capture_id=cid, kind="voxel_carve.calibration",
                       name="cal.json", path=str(Path(d) / "cal.json"), size=1, received=1,
                       sha256="x", content_type="", state="ready",
                       meta=json.dumps({"calibration": {"ok": True}}), actor="t",
                       created=0, updated=0)
        assert host.readiness(cid)["voxel_carve"]["ok"] is True
        assert host.blocked("voxel_carve", cid, job="suggest") == ""


def test_a_rendition_that_is_not_frame_aligned_is_never_offered():
    from quorum.api.routes import _streams
    with tempfile.TemporaryDirectory() as d:
        host = _host_with_real_plugins(d)
        cid = host.db.insert("captures", key="k", name="n", provider="t", config="{}",
                             layout="{}", n_frames=10, fps=10, created=0)
        media = {"path": "/x.mp4", "codec": "hevc", "playable": False, "renditions": {
            "grid": {"path": "/g.mp4", "w": 640, "h": 360, "codec": "h264", "aligned": True},
            "full": {"path": "/f.mp4", "w": 1920, "h": 1080, "codec": "h264",
                     "aligned": False, "why": "ffmpeg dropped a frame"}}}
        host.db.insert("streams", capture_id=cid, key="cam1", idx=0, name="cam1",
                       width=1920, height=1080, n_frames=10, media=json.dumps(media),
                       meta="{}")
        offered = [r["id"] for r in _streams(host, cid)[0]["renditions"]]
        assert offered == ["grid"], offered


if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as e:
            bad += 1
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - bad}/{len(fns)} passed")
    sys.exit(1 if bad else 0)
