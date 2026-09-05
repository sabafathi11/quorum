"""Tests for mask editing: the fold, the cut, the union, and undo.

These are the parts where being subtly wrong is expensive and invisible — a
join key that sorts differently from the desktop tool's orphans every identity
ever assigned to a join, and a cut that lands a frame early silently rewrites
what a person was doing.

Run with `.venv/bin/python tests/test_masks.py`, or with pytest.
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quorum.config import Config          # noqa: E402
from quorum.host import Host              # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load("masks_plugin", "plugins/masks/plugin.py")


def op(kind, **payload):
    return {"kind": kind, "payload": payload}


def kf(frame, outside=0, box=(0, 0, 2, 2), rle="0,4"):
    return {"frame": frame, "outside": outside, "payload": {"box": list(box), "rle": rle}}


# --------------------------------------------------------------------- keys
def test_join_key_matches_the_desktop_tools_ordering():
    # plain lexicographic, not numeric: this exact string is on disk in every
    # consistent_ids_<stamp>.json the lab has made
    assert M.join_key(["6", "7", "11", "12", "21", "23"]) == "11+12+21+23+6+7"
    assert M.join_key(["9", "22", "33"]) == "22+33+9"
    assert M.join_key(["49", "112@4126"]) == "112@4126+49"
    assert M.join_key(["3", "1", "2"]) == M.join_key(["2", "3", "1"]), "order in must not matter"


def test_orig_of_reads_a_segment_key():
    assert M.orig_of("86@2470") == "86"
    assert M.orig_of("86") == "86"


# --------------------------------------------------------------------- fold
def test_fold_delete_restore_and_purge():
    p = M.fold([op("masks.delete", stream="cam1", keys=["3", "4"]),
                op("masks.restore", stream="cam1", keys=["4"])])
    assert p.deleted["cam1"] == {"3"}
    p = M.fold([op("masks.delete", stream="cam1", keys=["3"]),
                op("masks.purge", stream="cam1", keys=["3"])])
    assert p.purged["cam1"] == {"3"}
    assert p.deleted["cam1"] == set(), "purging supersedes deleting, it does not stack"


def test_fold_keeps_join_groups_disjoint():
    p = M.fold([op("masks.join", stream="cam1", keys=["1", "2"]),
                op("masks.join", stream="cam1", keys=["2", "3"])])
    assert p.joins["cam1"] == [{"1", "2", "3"}], \
        "joining onto a member absorbs the whole group, it does not make two"
    p = M.fold([op("masks.join", stream="cam1", keys=["1", "2"]),
                op("masks.join", stream="cam1", keys=["7", "8"])])
    assert sorted(sorted(g) for g in p.joins["cam1"]) == [["1", "2"], ["7", "8"]]


def test_fold_unjoin_takes_the_whole_group_apart():
    p = M.fold([op("masks.join", stream="cam1", keys=["1", "2", "3"]),
                op("masks.unjoin", stream="cam1", keys=["2"])])
    assert p.joins.get("cam1") == []


def test_fold_split_collects_cuts_under_the_original():
    p = M.fold([op("masks.split", stream="cam1", key="86", frame=2470),
                op("masks.split", stream="cam1", key="86@2470", frame=2500)])
    assert p.splits["cam1"]["86"] == {2470, 2500}, \
        "cutting a segment is another cut in the same original track"


def test_fold_ignores_other_plugins_ops():
    p = M.fold([op("identity.link", items=[["cam1", "1"]]),
                op("masks.delete", stream="cam1", keys=["1"])])
    assert p.deleted["cam1"] == {"1"}


# ----------------------------------------------------------------- segments
def test_build_segments_cuts_where_you_asked():
    track = {"key": "86", "label": "person",
             "frames": [10, 20, 30, 40], "outside": [0, 0, 0, 1],
             "payload": [{"box": [0, 0, 2, 2], "rle": "0,4"}] * 4}
    segs = M.build_segments(track, [25])
    assert [s["key"] for s in segs] == ["86", "86@25"]
    first, second = segs
    assert [k["frame"] for k in first["frames"]] == [10, 20, 25]
    assert first["frames"][-1]["outside"] == 1, "the first half must stop at the cut"
    assert [k["frame"] for k in second["frames"]] == [25, 30, 40]
    assert second["frames"][0]["outside"] == 0, \
        "the second half starts at the cut holding the mask that was showing"


def test_build_segments_with_no_cuts_is_the_track_itself():
    track = {"key": "5", "label": "", "frames": [1, 2], "outside": [0, 1],
             "payload": [{"box": [0, 0, 1, 1], "rle": "0,1"}] * 2}
    segs = M.build_segments(track, [])
    assert len(segs) == 1 and segs[0]["key"] == "5"


def test_build_segments_does_not_resurrect_a_track_that_has_left():
    # a cut after the track went outside must not invent a mask there
    track = {"key": "9", "label": "", "frames": [1, 5], "outside": [0, 1],
             "payload": [{"box": [0, 0, 1, 1], "rle": "0,1"}] * 2}
    segs = M.build_segments(track, [8])
    later = [s for s in segs if s["key"] == "9@8"]
    assert not later or all(k["outside"] for k in later[0]["frames"])


# ------------------------------------------------------------------- union
def test_combine_unions_two_masks_into_one_box():
    import numpy as np
    C = M.codec()
    a = np.zeros((4, 4), dtype=bool); a[0:2, 0:2] = True
    b = np.zeros((4, 4), dtype=bool); b[2:4, 2:4] = True
    left = {"key": "1", "label": "p", "frames": [
        {"frame": 0, "outside": 0, "payload": {"box": [0, 0, 4, 4], "rle": C.encode_rle(a)}}]}
    right = {"key": "2", "label": "p", "frames": [
        {"frame": 0, "outside": 0, "payload": {"box": [0, 0, 4, 4], "rle": C.encode_rle(b)}}]}
    j = M.combine([left, right])
    assert j["key"] == "1+2"
    pay = j["frames"][0]["payload"]
    box = pay["box"]
    got = C.decode_rle(pay["rle"], box[2], box[3])
    assert got.sum() == 8, "the union keeps every pixel of both"
    assert box == [0, 0, 4, 4], "and is cropped to what is actually filled"


def test_combine_marks_frames_where_nobody_is_present_as_outside():
    C = M.codec()
    import numpy as np
    m = np.ones((2, 2), dtype=bool)
    a = {"key": "1", "label": "", "frames": [
        {"frame": 0, "outside": 0, "payload": {"box": [0, 0, 2, 2], "rle": C.encode_rle(m)}},
        {"frame": 5, "outside": 1, "payload": {"box": [0, 0, 2, 2], "rle": C.encode_rle(m)}}]}
    b = {"key": "2", "label": "", "frames": [
        {"frame": 9, "outside": 0, "payload": {"box": [4, 4, 2, 2], "rle": C.encode_rle(m)}}]}
    j = M.combine([a, b])
    at = {k["frame"]: k["outside"] for k in j["frames"]}
    assert at[0] == 0 and at[5] == 1 and at[9] == 0, \
        "the join is absent exactly while both members are"


# ------------------------------------------------------ end to end, real db
def _host(tmp):
    cfg = Config()
    cfg.data_dir = Path(tmp); cfg.db_path = Path(tmp) / "t.db"; cfg.blob_dir = Path(tmp) / "b"
    cfg.plugin_paths = [ROOT / "plugins"]
    return Host(cfg)


def _capture_with_tracks(host, tracks):
    """tracks: {stream key: {track key: [(frame, outside)]}}"""
    from quorum.sdk import Writer
    cid = host.db.insert("captures", key="k", name="n", provider="t", config="{}",
                         layout="{}", n_frames=100, fps=10, created=0)
    w = Writer(host, host.plugins["masks"])
    lay = w.layer(cid, key="masks", name="Masks", type="mask.rle", provenance="model")
    for skey, objs in tracks.items():
        sid = w.stream(cid, key=skey, idx=0, width=64, height=64, n_frames=100)
        w.objects(lay, sid, [
            {"key": k, "label": "person",
             "frames": [{"frame": f, "outside": o,
                         "payload": {"box": [0, 0, 2, 2], "rle": "0,4"}} for f, o in kfs]}
            for k, kfs in objs.items()])
    return cid, lay


def test_materialise_makes_a_cut_visible_as_two_tracks():
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        cid, lay = _capture_with_tracks(host, {"cam1": {"86": [(10, 0), (20, 0), (30, 0), (40, 1)]}})
        host.append_op(cid, "masks.split", {"stream": "cam1", "key": "86", "frame": 25}, "tester")
        out = M.materialise(host, cid)
        keys = {r["key"] for r in host.db.all(
            "SELECT key FROM objects WHERE layer_id=?", out["layer_id"])}
        assert keys == {"86", "86@25"}, keys
        assert "86" in out["shadow"]["cam1"], "the imported track must stop being drawn"


def test_materialise_then_undo_puts_the_track_back():
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        cid, lay = _capture_with_tracks(host, {"cam1": {"86": [(10, 0), (30, 0), (40, 1)]}})
        opr = host.append_op(cid, "masks.split", {"stream": "cam1", "key": "86", "frame": 25}, "tester")
        M.materialise(host, cid)
        host.set_undone(cid, opr["id"], True, "tester")
        out = M.materialise(host, cid)
        keys = {r["key"] for r in host.db.all(
            "SELECT key FROM objects WHERE layer_id=?", out["layer_id"])}
        assert keys == set(), "undoing the cut leaves nothing derived"
        assert out["shadow"]["cam1"] == [], "and stops hiding the original"


def test_materialise_joins_a_segment_with_another_track():
    """The lineage that broke the importer: cut 86 at 2470, then join the tail
    to 91. The result must be keyed `86@2470+91`, exactly as on disk."""
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        cid, lay = _capture_with_tracks(host, {"cam1": {
            "86": [(2180, 0), (2400, 0), (2470, 0), (2480, 1)],
            "91": [(2400, 0), (2600, 0), (2616, 1)]}})
        host.append_op(cid, "masks.split", {"stream": "cam1", "key": "86", "frame": 2470}, "t")
        host.append_op(cid, "masks.join", {"stream": "cam1", "keys": ["86@2470", "91"]}, "t")
        out = M.materialise(host, cid)
        keys = {r["key"] for r in host.db.all(
            "SELECT key FROM objects WHERE layer_id=?", out["layer_id"])}
        assert keys == {"86", "86@2470+91"}, keys
        shadow = set(out["shadow"]["cam1"])
        assert {"86", "91"} <= shadow, "both ingredients stop being drawn on their own"


def test_purged_tracks_are_not_materialised():
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        cid, lay = _capture_with_tracks(host, {"cam1": {"5": [(1, 0), (9, 1)], "6": [(1, 0), (9, 1)]}})
        host.append_op(cid, "masks.join", {"stream": "cam1", "keys": ["5", "6"]}, "t")
        host.append_op(cid, "masks.purge", {"stream": "cam1", "keys": ["5+6"]}, "t")
        out = M.materialise(host, cid)
        keys = {r["key"] for r in host.db.all(
            "SELECT key FROM objects WHERE layer_id=?", out["layer_id"])}
        assert keys == set(), "a purged join is not written at all"


def test_purge_removes_a_plain_track_from_the_derived_layer_itself():
    """Not just from the exported set — from the layer.

    `effective_objects` filters purged keys a second time, so a purge that
    failed here would still look right in every export. The layer is the thing
    that gets drawn and hit-tested, so it is the thing to check.
    """
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        cid, lay = _capture_with_tracks(host, {"cam1": {"86": [(10, 0), (30, 0), (40, 1)]}})
        host.append_op(cid, "masks.split", {"stream": "cam1", "key": "86", "frame": 25}, "t")
        out = M.materialise(host, cid)
        assert {r["key"] for r in host.db.all(
            "SELECT key FROM objects WHERE layer_id=?", out["layer_id"])} == {"86", "86@25"}
        host.append_op(cid, "masks.purge", {"stream": "cam1", "keys": ["86@25"]}, "t")
        out = M.materialise(host, cid)
        keys = {r["key"] for r in host.db.all(
            "SELECT key FROM objects WHERE layer_id=?", out["layer_id"])}
        assert keys == {"86"}, f"the purged segment is still in the layer: {keys}"


def test_effective_objects_drops_what_the_edits_replaced():
    """What leaves this server must be the edited set, not one layer's rows.

    The trap: a cut track keeps its original key, so `86` exists in *both*
    layers at once. Exporting "the mask layer" would ship the uncut original.
    """
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        cid, lay = _capture_with_tracks(host, {"cam1": {
            "86": [(10, 0), (30, 0), (40, 1)],
            "91": [(10, 0), (40, 1)],
            "7": [(1, 0), (9, 1)],
            "8": [(1, 0), (9, 1)]}})
        host.append_op(cid, "masks.split", {"stream": "cam1", "key": "86", "frame": 25}, "t")
        host.append_op(cid, "masks.delete", {"stream": "cam1", "keys": ["7"]}, "t")
        host.append_op(cid, "masks.purge", {"stream": "cam1", "keys": ["8"]}, "t")
        M.materialise(host, cid)

        both = [r["key"] for r in host.db.all(
            "SELECT o.key FROM objects o JOIN layers l ON l.id=o.layer_id "
            "WHERE l.capture_id=? AND o.key='86'", cid)]
        assert len(both) == 2, "the premise: the cut track's key is in both layers"

        eff = M.effective_objects(host, cid)
        keys = sorted(r["key"] for r in eff)
        assert keys == ["86", "86@25", "91"], keys
        assert "7" not in keys, "a deleted track is not exported"
        assert "8" not in keys, "a purged track is not exported"
        derived = M.derived_layer(host.db, cid)
        by_key = {r["key"]: r["layer_id"] for r in eff}
        assert by_key["86"] == derived["id"], \
            "the surviving `86` must be the cut one, not the imported original"
        assert by_key["91"] != derived["id"], "an untouched track is still served from the import"

        withdel = M.effective_objects(host, cid, include_deleted=True)
        assert "7" in {r["key"] for r in withdel}, "deleted tracks are recoverable, not gone"
        assert "8" not in {r["key"] for r in withdel}, "purged is purged"


def test_effective_objects_prefers_the_join_over_its_ingredients():
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        cid, lay = _capture_with_tracks(host, {"cam1": {
            "1": [(1, 0), (9, 1)], "2": [(1, 0), (9, 1)], "3": [(1, 0), (9, 1)]}})
        host.append_op(cid, "masks.join", {"stream": "cam1", "keys": ["1", "2", "3"]}, "t")
        M.materialise(host, cid)
        keys = sorted(r["key"] for r in M.effective_objects(host, cid))
        assert keys == ["1+2+3"], keys


def test_undone_ops_leave_the_fold():
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        cid = host.db.insert("captures", key="k", name="n", provider="t", config="{}",
                             layout="{}", n_frames=10, fps=1, created=0)
        a = host.append_op(cid, "masks.delete", {"stream": "cam1", "keys": ["1"]}, "tester")
        assert M.fold(host.ops_since(cid, 0)).deleted["cam1"] == {"1"}
        host.set_undone(cid, a["id"], True, "tester")
        assert M.fold(host.ops_since(cid, 0)).deleted.get("cam1", set()) == set()
        assert len(host.ops_since(cid, 0, include_undone=True)) == 1, "the op is kept, not deleted"
        host.set_undone(cid, a["id"], False, "tester")
        assert M.fold(host.ops_since(cid, 0)).deleted["cam1"] == {"1"}, "redo puts it back"


def test_undo_only_reaches_your_own_ops():
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        cid = host.db.insert("captures", key="k", name="n", provider="t", config="{}",
                             layout="{}", n_frames=10, fps=1, created=0)
        host.append_op(cid, "masks.delete", {"stream": "cam1", "keys": ["1"]}, "alice")
        mine = host.append_op(cid, "masks.delete", {"stream": "cam1", "keys": ["2"]}, "bob")
        host.append_op(cid, "masks.delete", {"stream": "cam1", "keys": ["3"]}, "alice")
        assert host.last_op_of(cid, "bob")["id"] == mine["id"], \
            "Ctrl+Z must never take back someone else's more recent edit"
        assert host.last_op_of(cid, "carol") is None


if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as e:
            bad += 1
            import traceback
            traceback.print_exc()
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - bad}/{len(fns)} passed")
    sys.exit(1 if bad else 0)
