"""Tests for the Shift+Space walk — the scan that decides what needs a human.

This suite exists because the walk had none. Its only coverage was a browser
check that needs the six-camera fixture, and that fixture cannot express the
case that was actually broken: a capture with **no imported layer**, where
every track was created in place. That is what the SAM-first workflow produces,
it is what runs on the GPU box, and on it Shift+Space said "Nothing left" while
the capture was full of unidentified tracks.

The bug was one flattened dictionary. `hidden_filter` now answers per layer,
and these are the cases that pin it down.

Run with `.venv/bin/python tests/test_identity.py`, or with pytest.
"""
import importlib.util
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


M = _load("masks_plugin_i", "plugins/masks/plugin.py")


def _host(tmp):
    cfg = Config()
    cfg.data_dir = Path(tmp); cfg.db_path = Path(tmp) / "t.db"; cfg.blob_dir = Path(tmp) / "b"
    cfg.plugin_paths = [ROOT / "plugins"]
    return Host(cfg)


def _identity(host):
    """The plugin as the host loaded it, so it shares the host's module identity."""
    return sys.modules[host.plugins["identity"].module_name]


def _capture(host, tracks, layer_key="masks"):
    """One stream, one mask layer. `tracks` is {key: [(frame, outside)]}.

    `layer_key` decides which kind of layer it is, and that is the whole point
    of this helper: `masks.DERIVED_KEY` makes it the *edited* layer, which is
    what a capture whose tracks were all created in place has — and has only.
    """
    from quorum.sdk import Writer
    cid = host.db.insert("captures", key="k", name="n", provider="t", config="{}",
                         layout="{}", n_frames=100, fps=10, created=0)
    w = Writer(host, host.plugins["masks"])
    lay = w.layer(cid, key=layer_key, name="L", type="mask.rle", provenance="human")
    sid = w.stream(cid, key="cam1", idx=0, width=64, height=64, n_frames=100)
    w.objects(lay, sid, [
        {"key": k, "label": "person",
         "frames": [{"frame": f, "outside": o,
                     "payload": {"box": [0, 0, 2, 2], "rle": "0,4"}} for f, o in kfs]}
        for k, kfs in tracks.items()])
    return cid, lay


# ------------------------------------------------------------------ the bug
def _sam_capture(host, created, edited=()):
    """A capture built the way the SAM workspace builds one: no import, ever.

    This matters, and getting it wrong is why the first version of this test
    passed against the bug. `masks.materialise` bails out with "no imported
    mask layer to edit" unless something was *created by an op* — so writing
    objects straight into a derived layer produces an empty shadow doc, and the
    defect has nothing to bite on. Tracks have to arrive as `masks.create`, the
    way the interactor writes them, and the edit as `masks.keyframes`; only
    then does the edited key land in the shadow doc, which is the state the GPU
    box was actually in.
    """
    from quorum.sdk import Writer
    cid = host.db.insert("captures", key="k", name="n", provider="t", config="{}",
                         layout="{}", n_frames=100, fps=10, created=0)
    Writer(host, host.plugins["masks"]).stream(
        cid, key="cam1", idx=0, width=64, height=64, n_frames=100)
    for key, kfs in created.items():
        host.append_op(cid, "masks.create", {
            "stream": "cam1", "key": key, "label": "person",
            "frames": [{"frame": f, "outside": o,
                        "payload": {"box": [0, 0, 2, 2], "rle": "0,4"}} for f, o in kfs]},
            "tester")
    for key in edited:
        host.append_op(cid, "masks.keyframes", {
            "stream": "cam1", "key": key, "label": "person",
            "frames": [{"frame": 20, "outside": 0,
                        "payload": {"box": [1, 1, 2, 2], "rle": "0,4"}}]}, "tester")
    out = M.materialise(host, cid)
    assert out.get("layer_id"), f"materialise did not build a layer: {out}"
    return cid, out["layer_id"], out


def test_a_track_created_in_place_is_offered_to_the_walk():
    """The regression, in the state the GPU box was in.

    Every track made by the SAM interactor, one of them since edited. There is
    no imported layer, so nothing can possibly be superseded — and yet the
    edited key goes into the shadow doc, because that is how the derived layer
    tells the imported one to stop drawing it.

    Before the fix the walk removed that key from *every* layer, so the only
    copy — the one on screen, with no identity — vanished from the scan.
    The problem batch came back empty and Shift+Space reported "Nothing left".
    """
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        I = _identity(host)
        cid, lay, out = _sam_capture(host, {"sam1": [(10, 0), (70, 1)]}, edited=["sam1"])

        assert M.source_layer(host.db, cid) is None, "this fixture must have no imported layer"
        assert "sam1" in out["shadow"].get("cam1", []), \
            "the fixture must reproduce the shadow entry, or there is no bug to catch"

        hidden = I.hidden_filter(host, cid)
        assert not hidden("cam1", "sam1", lay), "the only copy of a track is never superseded"

        res = I.scan(host, cid, frame=0, direction=1, limit=1)
        assert res["problems"], f"the walk found nothing to do: {res}"
        p = res["problems"][0]
        assert p["kind"] == "unset", p
        assert "sam1" in p["why"], p


def test_an_edited_track_stays_linkable_on_a_capture_with_no_import():
    """The same root cause, one layer up: the op validator.

    `live_keys` is what refuses an identity op naming a track that is gone. It
    shared the flattened shadow, so on this capture an edited track was not
    "live" — and assigning it an identity was refused with "no longer there"
    while the person was looking at it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        I = _identity(host)
        cid, lay, _ = _sam_capture(host, {"sam1": [(10, 0), (70, 1)]}, edited=["sam1"])
        assert "sam1" in I.live_keys(host, cid).get("cam1", set()), \
            "an edited track on a capture with no import is still a track"
        assert I.still_applies(host, cid, {"items": [["cam1", "sam1"]]}) is None, \
            "the validator refused an identity op naming a track that is on screen"


def test_the_imported_copy_is_skipped_and_the_edited_one_is_offered():
    """The case that always worked, kept working — and the half that did not.

    A cut leaves `86` in the imported layer and `86` + `86@25` in the derived
    one. The imported `86` must not be offered; the derived `86` must be, and
    before the fix neither was.
    """
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        I = _identity(host)
        cid, src = _capture(host, {"86": [(10, 0), (40, 1)]}, layer_key="masks")
        host.append_op(cid, "masks.split", {"stream": "cam1", "key": "86", "frame": 25}, "tester")
        out = M.materialise(host, cid)
        derived = out["layer_id"]
        assert derived != src and "86" in out["shadow"]["cam1"]

        hidden = I.hidden_filter(host, cid)
        assert hidden("cam1", "86", src), "the imported copy was replaced; do not offer it"
        assert not hidden("cam1", "86", derived), "the edited copy is what is on screen"
        assert not hidden("cam1", "86@25", derived)

        offered = {o for p in I.scan(host, cid, limit=99)["problems"] for o in p["objects"]}
        in_src = {r["id"] for r in host.db.all(
            "SELECT id FROM objects WHERE layer_id=?", src)}
        assert offered and not (offered & in_src), \
            "the walk offered a track out of the layer it stopped drawing"


def test_a_deleted_track_is_hidden_in_every_layer():
    """`deleted` and `purged` are not layer-scoped: a decision, not a redraw."""
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        I = _identity(host)
        cid, lay = _capture(host, {"a": [(10, 0), (70, 1)], "b": [(10, 0), (70, 1)]},
                            layer_key=M.DERIVED_KEY)
        host.append_op(cid, "masks.delete", {"stream": "cam1", "keys": ["a"]}, "tester")
        hidden = I.hidden_filter(host, cid)
        assert hidden("cam1", "a", lay), "a deleted track is gone from every layer"
        assert not hidden("cam1", "b", lay)
        offered = {p["why"] for p in I.scan(host, cid, limit=99)["problems"]}
        assert offered and not any("track a" in w for w in offered), offered


# --------------------------------------------------------- what a problem is
def test_an_assigned_track_is_not_a_problem_and_a_repeated_id_is():
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        I = _identity(host)
        cid, lay = _capture(host, {"a": [(10, 0), (70, 1)], "b": [(10, 0), (70, 1)]},
                            layer_key=M.DERIVED_KEY)
        assert I.scan(host, cid, limit=99)["problems"], "two unset tracks is a problem"

        host.append_op(cid, "identity.link", {"items": [["cam1", "a"]]}, "tester")
        host.append_op(cid, "identity.link", {"items": [["cam1", "b"]]}, "tester")
        assert not I.scan(host, cid, limit=99)["problems"], "both are identified now"

        # …and the same identity on two masks in one camera is impossible
        cid2 = I.state_for(host, cid)["assignments"]["cam1"]["a"]
        host.append_op(cid, "identity.assign",
                       {"items": [["cam1", "b"]], "cid": cid2}, "tester")
        out = I.scan(host, cid, limit=99)
        assert out["problems"] and out["problems"][0]["kind"] == "duplicate", out


def test_the_walk_can_go_backwards():
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        I = _identity(host)
        cid, lay = _capture(host, {"a": [(10, 0), (30, 1)], "b": [(60, 0), (80, 1)]},
                            layer_key=M.DERIVED_KEY)
        fwd = I.scan(host, cid, frame=0, direction=1, limit=99)["problems"]
        back = I.scan(host, cid, frame=99, direction=-1, limit=99)["problems"]
        assert fwd and back, (fwd, back)
        assert back[0]["frame"] >= fwd[0]["frame"], "backwards must start from the far end"


def test_the_walk_returns_a_small_batch_without_counting_every_problem():
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        I = _identity(host)
        tracks = {f"t{i}": [(i * 10, 0), (i * 10 + 2, 1)] for i in range(8)}
        cid, _ = _capture(host, tracks, layer_key=M.DERIVED_KEY)
        out = I.scan(host, cid, frame=0, direction=1, limit=4)
        assert "total" not in out, out
        assert len(out["problems"]) == 4, out
        assert [p["frame"] for p in out["problems"]] == [0, 10, 20, 30], out


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    bad = 0
    for n, f in fns:
        try:
            f()
            print(f"  ok   {n}")
        except Exception as e:
            bad += 1
            print(f"  FAIL {n}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - bad}/{len(fns)} passed")
    raise SystemExit(1 if bad else 0)
