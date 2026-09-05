"""Property tests: random edit sequences, invariants that must hold after each.

The example tests say "this case works". These say "no case breaks", which is
the difference that matters here — every bug found in mask editing so far came
from a combination nobody thought to write down (a cut whose tail was then
joined to another track; a key that ends up in two layers at once; an
assignment naming a track a later edit replaced).

Deterministic: each case is driven by a seeded RNG, and a failure prints the
seed and the exact op sequence so it can be replayed.
"""
import importlib.util
import json
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quorum.config import Config          # noqa: E402
from quorum.host import Host              # noqa: E402
from quorum.sdk import Writer             # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load("masks_plugin", "plugins/masks/plugin.py")

STREAMS = ["cam1", "cam2"]
TRACKS = ["1", "2", "3", "4", "5"]


def _host(tmp):
    cfg = Config()
    cfg.data_dir = Path(tmp); cfg.db_path = Path(tmp) / "t.db"; cfg.blob_dir = Path(tmp) / "b"
    cfg.plugin_paths = [ROOT / "plugins"]
    return Host(cfg)


def _seed_capture(host):
    cid = host.db.insert("captures", key="k", name="n", provider="t", config="{}",
                         layout="{}", n_frames=200, fps=10, created=0)
    w = Writer(host, host.plugins["masks"])
    lay = w.layer(cid, key="masks", name="Masks", type="mask.rle", provenance="model")
    for i, skey in enumerate(STREAMS):
        sid = w.stream(cid, key=skey, idx=i, width=64, height=64, n_frames=200)
        w.objects(lay, sid, [
            {"key": k, "label": "person", "frames": [
                {"frame": 10, "outside": 0, "payload": {"box": [0, 0, 2, 2], "rle": "0,4"}},
                {"frame": 60, "outside": 0, "payload": {"box": [1, 1, 2, 2], "rle": "0,4"}},
                {"frame": 120, "outside": 1, "payload": {"box": [1, 1, 2, 2], "rle": "0,4"}}]}
            for k in TRACKS])
    return cid


def _current_keys(host, cid):
    """Keys that exist in the derived layer, per stream."""
    out = {}
    der = M.derived_layer(host.db, cid)
    if der is None:
        return out
    for r in host.db.all(
            "SELECT s.key AS stream, o.key AS okey FROM objects o "
            "JOIN streams s ON s.id=o.stream_id WHERE o.layer_id=?", der["id"]):
        out.setdefault(r["stream"], set()).add(str(r["okey"]))
    return out


def random_ops(rng, host, cid, n):
    """A plausible edit session: cuts, joins and deletions aimed at whatever
    exists at the time, so later ops act on earlier ones' output."""
    made = []
    for _ in range(n):
        stream = rng.choice(STREAMS)
        proj = M.fold(host.ops_since(cid, 0))
        derived = _current_keys(host, cid).get(stream, set())
        pool = sorted(set(TRACKS) | derived)
        verb = rng.choice(["delete", "restore", "purge", "split", "join", "unjoin"])
        if verb in ("delete", "restore", "purge"):
            payload = {"stream": stream, "keys": rng.sample(pool, k=min(2, len(pool)))}
        elif verb == "split":
            payload = {"stream": stream, "key": rng.choice(pool),
                       "frame": rng.choice([20, 40, 70, 90, 110])}
        elif verb == "join":
            if len(pool) < 2:
                continue
            payload = {"stream": stream, "keys": rng.sample(pool, k=rng.choice([2, 2, 3]))}
        else:
            groups = proj.joins.get(stream) or []
            if not groups:
                continue
            payload = {"stream": stream, "key": sorted(rng.choice(groups))[0]}
        op = host.append_op(cid, f"masks.{verb}", payload, "fuzz")
        made.append((verb, payload))
        M.materialise(host, cid)
    return made


def check_invariants(host, cid):
    """Everything that must be true after any sequence of edits."""
    proj = M.fold(host.ops_since(cid, 0))
    eff = M.effective_objects(host, cid)
    problems = []

    # 1. one track per name — the bug that made group select pick invisible copies
    seen = {}
    for r in eff:
        k = (r["stream"], r["key"])
        if k in seen:
            problems.append(f"{k} is effective twice (layers {seen[k]} and {r['layer_id']})")
        seen[k] = r["layer_id"]

    # 2. nothing a human removed comes back
    for r in eff:
        if r["key"] in proj.purged.get(r["stream"], set()):
            problems.append(f"purged {r['stream']}/{r['key']} is still effective")
        if r["key"] in proj.deleted.get(r["stream"], set()):
            problems.append(f"deleted {r['stream']}/{r['key']} is still effective")

    # 3. a joined track replaces its ingredients, it does not sit beside them
    live = {(r["stream"], r["key"]) for r in eff}
    for stream, groups in proj.joins.items():
        for g in groups:
            jk = M.join_key(g)
            for member in g:
                if (stream, member) in live and (stream, jk) in live:
                    problems.append(f"{stream}/{member} is drawn beside its own join {jk}")

    # 4. join groups stay disjoint
    for stream, groups in proj.joins.items():
        flat = [k for g in groups for k in g]
        if len(flat) != len(set(flat)):
            problems.append(f"{stream}: a track is in two join groups at once")

    # 5. every derived track has at least one keyframe
    der = M.derived_layer(host.db, cid)
    if der:
        for r in host.db.all(
                "SELECT o.id, o.key, (SELECT COUNT(*) FROM shapes sh WHERE sh.object_id=o.id) n "
                "FROM objects o WHERE o.layer_id=?", der["id"]):
            if not r["n"]:
                problems.append(f"derived track {r['key']} has no keyframes")

    # 6. materialise is idempotent — a rebuild must not drift
    before = sorted((r["stream"], r["key"], r["layer_id"]) for r in eff)
    M.materialise(host, cid)
    after = sorted((r["stream"], r["key"], r["layer_id"])
                   for r in M.effective_objects(host, cid))
    if before != after:
        problems.append("rebuilding changed the result — materialise is not idempotent")

    return problems


def test_random_edit_sessions_keep_every_invariant():
    failures = []
    for seed in range(25):
        rng = random.Random(seed)
        with tempfile.TemporaryDirectory() as tmp:
            host = _host(tmp)
            cid = _seed_capture(host)
            ops = random_ops(rng, host, cid, n=rng.choice([3, 5, 8]))
            bad = check_invariants(host, cid)
            if bad:
                failures.append(f"seed {seed}: {bad[0]}\n     ops: " +
                                "; ".join(f"{v} {json.dumps(p)}" for v, p in ops))
    assert not failures, "\n  " + "\n  ".join(failures[:3])


def test_undoing_everything_returns_to_the_imported_state():
    """The strongest claim the op log makes: nothing is destroyed. Undo every
    edit and the capture must look exactly as it was imported."""
    failures = []
    for seed in range(12):
        rng = random.Random(1000 + seed)
        with tempfile.TemporaryDirectory() as tmp:
            host = _host(tmp)
            cid = _seed_capture(host)
            src = M.source_layer(host.db, cid)
            before = sorted((r["stream"], r["key"]) for r in M.effective_objects(host, cid))
            random_ops(rng, host, cid, n=rng.choice([3, 6]))
            for op in host.ops_since(cid, 0, include_undone=True):
                host.set_undone(cid, op["id"], True, "fuzz")
            out = M.materialise(host, cid)
            after = sorted((r["stream"], r["key"]) for r in M.effective_objects(host, cid))
            if after != before:
                failures.append(f"seed {seed}: {len(before)} tracks became {len(after)}")
            if any(out["shadow"].values()):
                failures.append(f"seed {seed}: still hiding {out['shadow']} with no edits left")
    assert not failures, "\n  " + "\n  ".join(failures[:3])


def test_a_cut_never_loses_or_duplicates_a_keyframe():
    """Splitting is the one edit that rewrites keyframes. Every keyframe of the
    original must land in exactly one segment — plus the two synthetic ones the
    cut itself introduces."""
    rng = random.Random(7)
    for _ in range(40):
        n = rng.randint(2, 8)
        frames = sorted(rng.sample(range(1, 200), n))
        outs = [0] * (n - 1) + [1]
        track = {"key": "t", "label": "", "frames": frames, "outside": outs,
                 "payload": [{"box": [0, 0, 2, 2], "rle": "0,4"}] * n}
        cuts = sorted(rng.sample(range(1, 200), rng.randint(1, 3)))
        segs = M.build_segments(track, cuts)
        got = [k["frame"] for s in segs for k in s["frames"]]
        for f in frames:
            assert f in got, f"keyframe {f} vanished when cutting at {cuts}"
        for s in segs:
            fs = [k["frame"] for k in s["frames"]]
            assert fs == sorted(fs), f"segment {s['key']} is out of order"
            assert len(fs) == len(set(fs)), f"segment {s['key']} has a duplicate keyframe"
        keys = [s["key"] for s in segs]
        assert len(keys) == len(set(keys)), "two segments share a key"


def test_validator_refuses_an_op_naming_a_track_that_is_gone():
    """The hook the proposal queue leans on. It must notice a track that a later
    edit replaced, and stop noticing once that edit is undone."""
    ident = _load("identity_plugin_props", "plugins/identity/plugin.py")
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        cid = _seed_capture(host)
        payload = {"items": [["cam1", "1"], ["cam1", "2"]]}
        assert host.validate_op(cid, "identity.link", payload) is None, \
            "both tracks exist, so the op applies"

        op = host.append_op(cid, "masks.join", {"stream": "cam1", "keys": ["1", "2"]}, "t")
        M.materialise(host, cid)
        ident._LIVE.clear()
        reason = host.validate_op(cid, "identity.link", payload)
        assert reason, "joining 1 and 2 must invalidate a suggestion to link them"
        assert "cam1/1" in reason or "cam1/2" in reason, reason

        host.set_undone(cid, op["id"], True, "t")
        M.materialise(host, cid)
        ident._LIVE.clear()
        assert host.validate_op(cid, "identity.link", payload) is None, \
            "undoing the join must make the suggestion applicable again"


def test_validator_is_silent_for_kinds_nobody_owns():
    with tempfile.TemporaryDirectory() as tmp:
        host = _host(tmp)
        cid = _seed_capture(host)
        assert host.validate_op(cid, "nosuchplugin.thing", {"items": [["cam9", "x"]]}) is None
        assert host.validate_op(cid, "masks.delete", {"stream": "cam1", "keys": ["1"]}) is None


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
