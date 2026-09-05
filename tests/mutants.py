"""Do the tests actually have teeth?

A green suite proves nothing on its own — the suite that shipped mask editing
was green while group select was selecting tracks nobody could see. This breaks
load-bearing lines on purpose, one at a time, and fails if the tests *pass*
anyway. A mutant that survives is a hole, and it names itself.

    .venv/bin/python tests/mutants.py            # all of them
    .venv/bin/python tests/mutants.py join_key   # one, by name
    .venv/bin/python tests/mutants.py --js       # client only (needs the server up)
    .venv/bin/python tests/mutants.py --verify   # is the tree clean? (a second)

Each mutation is a real bug that has either happened here or is one edit away.
Keep this list honest: a mutation nobody would ever write teaches nothing.
"""
import os
import shutil
import pathlib
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv/bin/python")

# name, file, find, replace, why it matters, which suites can catch it
#
# "py" mutations run the python suites; "js" ones run the browser suite, which
# re-reads the client from disk on every run and so needs no restart. The client
# is where the group-select bug lived, so leaving it unmutated would repeat the
# mistake this file exists to prevent.
MUTATIONS = [
    ("join_key", "plugins/masks/plugin.py",
     '    return "+".join(sorted(set(str(k) for k in keys)))',
     '    return "+".join(sorted(set(str(k) for k in keys), key=_sortkey))',
     "sorting join keys numerically orphans every identity ever assigned to a join "
     "(this one shipped, and cost 4 of the 44 dangling keys)", "py"),

    ("touched_joins", "plugins/masks/plugin.py",
     "    for g in proj.joins.get(stream, []):\n        for k in g:\n            out.add(orig_of(k))",
     "    pass",
     "forgetting that a join touches its members leaves the originals drawn "
     "beside the join — the same person on screen twice", "py"),

    ("effective_shadow", "plugins/masks/plugin.py",
     '        if src and r["layer_id"] == src["id"] and key in shadow.get(stream, set()):\n            continue',
     "        pass",
     "exporting the imported copy of a track that was cut ships the unedited masks", "py"),

    ("effective_deleted", "plugins/masks/plugin.py",
     '        if not include_deleted and key in proj.deleted.get(stream, set()):\n            continue',
     "        pass",
     "exporting deleted tracks undoes every deletion a human made", "py"),

    ("purge_materialise", "plugins/masks/plugin.py",
     "            if key in consumed or key in purged:\n                continue",
     "            if key in consumed:\n                continue",
     "a purged track coming back is the one edit that is supposed to be absolute", "py"),

    ("split_terminator", "plugins/masks/plugin.py",
     '        if hi is not None and sf and not sf[-1]["outside"]:\n'
     '            sf.append({"frame": hi, "outside": 1, "payload": sf[-1]["payload"]})',
     "        pass",
     "a cut whose first half never ends leaves both halves drawn after the cut", "py"),

    ("undone_ops", "quorum/host.py",
     '        if not include_undone:\n            sql += " AND undone=0"',
     "        pass",
     "if folds keep seeing undone ops, undo silently does nothing", "py"),

    ("validator_dispatch", "quorum/sdk.py",
     "                self.validators[k if k.startswith(self.id + \".\") else f\"{self.id}.{k}\"] = fn",
     "                pass",
     "without validators the proposal queue offers suggestions about tracks that are gone", "py"),

    ("fold_purge_beats_delete", "plugins/masks/plugin.py",
     '            p.purged.setdefault(s, set()).update(keys)\n'
     '            p.deleted.setdefault(s, set()).difference_update(keys)',
     "            p.purged.setdefault(s, set()).update(keys)",
     "a track that is both deleted and purged must resolve to one state, not two", "py"),

    ("identity_outside_sentinel", "plugins/identity/plugin.py",
     "            cid = int(p.get(\"cid\"))\n            if cid != OUTSIDE:\n                used.add(cid)",
     "            cid = int(p.get(\"cid\"))\n            used.add(cid)",
     "letting 200 seed id allocation is a bug this codebase has already had once", "py"),

    # ---- the client, where the bug that prompted all this actually lived ----
    ("client_swallows_refusals", "plugins/masks/web/masks.js",
     "        if (e.status === 409) ctx.toast('That edit cannot apply', e.message, 'warn');\n"
     "        else ctx.toast('Edit failed', e.message || String(e), 'err');",
     "        void e;",
     "a refused edit that says nothing looks like a broken key, so the user presses it again",
     "js"),

    ("client_findobject_ignores_visibility", "web/core/app.js",
     "        if (this.display.visible(entry)) return o;",
     "        return o;",
     "resolving a track to its superseded copy selects something invisible — this is "
     "exactly the group-select bug",
     "js"),

    # ---- the display authority: one owner, four doors ----------------------
    # The following line is carried along only to tell this door apart from
    # door 4, which filters identically one screen further down. Without it
    # `.replace(…, 1)` picks whichever comes first in the file, and a harmless
    # reordering would silently move this mutation to the wrong function —
    # still green, still "caught", testing something else.
    ("door_select_lets_hidden_through", "web/core/app.js",
     "    const ok = want.filter((id) => this.display.visibleId(id));\n"
     "    const dropped = want.length - ok.length;",
     "    const ok = want;\n    const dropped = want.length - ok.length;",
     "a tool could act on a track the user is not being shown — the exact family of "
     "bug the display authority exists to close, and it would be invisible until "
     "somebody deleted six cameras of work",
     "js"),

    ("door_reveal_points_at_hidden", "web/core/app.js",
     "    const ok = want.filter((id) => this.display.visibleId(id));\n"
     "    if (select && ok.length) this.select(ok, 'set');",
     "    const ok = want;\n    if (select && ok.length) this.select(ok, 'set');",
     "the walk would jump to a frame and ring a mask that is not on screen",
     "js"),

    ("entries_unfiltered", "web/core/app.js",
     "      for (const e of d.activeAtRaw(frame)) if (this.display.visible(e)) out.push(e);",
     "      for (const e of d.activeAtRaw(frame)) out.push(e);",
     "hidden classes, deleted tracks and superseded copies would all be painted again",
     "js"),

    # ---- time: the contract between the pixels and the annotations ---------
    ("frame_map_wrong_side", "quorum/probe.py",
     'idx = np.searchsorted(stamps, t, side="right") - 1',
     'idx = np.searchsorted(stamps, t, side="left") - 1',
     "every mask lands one source frame early on any camera whose frame times fall "
     "exactly on a capture frame boundary",
     "py"),

    ("frame_map_float_error", "quorum/probe.py",
     't = np.rint((np.arange(int(n_frames), dtype=np.float64) / float(fps)\n'
     '                 - float(offset)) * 1e6).astype("int64")',
     't = ((np.arange(int(n_frames), dtype=np.float64) / float(fps)\n'
     '                 - float(offset)) * 1e6)',
     "float error puts scattered frames on the wrong picture — 3/5.0 - 0.2 is "
     "0.39999999999999997, and this actually happened",
     "py"),

    ("cell_seeks_by_capture_time", "web/core/media.js",
     "    return stamps[j] + gap * 0.25 + t0;",
     "    return this.comp.timeOf(captureFrame) + t0;",
     "a cell shows a neighbouring frame whenever a camera's rate does not divide the "
     "capture's, so the masks sit on the wrong picture and nothing says so",
     "js"),

    ("rendition_alignment_ignored", "quorum/api/routes.py",
     '            if rr.get("aligned") is False:\n                continue',
     '            if False:\n                continue',
     "an ffmpeg run that dropped a frame would be offered as a rendition, putting "
     "every mask one frame out in that camera",
     "py"),

    # ---- requirements ------------------------------------------------------
    ("requirement_gate_open", "quorum/jobs.py",
     "        blocked = self.host.blocked(plugin_id, capture_id, job=kind)",
     "        blocked = \"\"",
     "a plugin runs without the data it declared it cannot work without — the voxel "
     "carve would return confident nonsense instead of refusing",
     "py"),

    # ---- playback: a frame budget overrun is a correctness bug -------------
    ("stills_during_playback", "web/core/media.js",
     "      if (this.comp.playing && behind) return null;",
     "      if (false) return null;",
     "every zoomed cell asks the server for an ffmpeg still several times a second "
     "during playback — six of them measured at 858 ms — so the mask windows queue "
     "behind them and the masks visibly trail the video",
     "js"),

    ("timeline_repaints_lanes_every_frame", "web/core/timeline.js",
     "      this.store.on(['frame'], () => this.paint()),",
     "      this.store.on(['frame'], () => { this.lanesDirty = true; this.paint(); }),",
     "six streams of activity and every plugin's lane decorator are redrawn 25 times "
     "a second, which is most of the frame budget, and a late frame is a mask on the "
     "wrong picture",
     "js"),

    ("timeline_outlives_its_capture", "web/core/app.js",
     "    this.timeline?.destroy(); this.timeline = null;",
     "    this.timeline = null;",
     "every capture you open leaves the previous timeline subscribed, so the second "
     "one repaints everything twice a frame and the third three times — a stutter "
     "nobody can trace back to having opened a capture earlier",
     "js"),

    # ---- SAM ----------------------------------------------------------------
    # The first of these is the one that would ship. A frame fetched by index
    # instead of by timestamp segments a different picture, writes a mask that
    # looks entirely reasonable, and says nothing.
    ("sam_frame_by_index", "plugins/sam/frames.py",
     "        out.append(int(stamps[f]) / 1e6)",
     "        out.append(f / 25.0)",
     "addressing a frame by index on variable-rate footage lands on a different "
     "picture, and the wrong mask looks exactly like a right one", "py"),

    ("sam_frame_tolerance", "plugins/sam/frames.py",
     "TOL = 0.002",
     "TOL = 0.2",
     "a select tolerance wider than a frame interval matches several frames per "
     "request, and the run of frames stops meaning what it says", "py"),

    ("sam_seed_overwritten", "plugins/sam/plugin.py",
     '    items = [{"frame": f, "payload": {**p, "outside": 0}}\n'
     '             for f, p in zip(want[1:], got[1:]) if p is not None]',
     '    items = [{"frame": f, "payload": {**p, "outside": 0}}\n'
     '             for f, p in zip(want, got) if p is not None]',
     "SAM re-segments its seed frame; writing that back silently replaces the mask "
     "a human looked at and accepted", "py"),

    ("sam_box_off_by_one", "plugins/sam/client.py",
     '    return {"box": [xmin, ymin, xmax - xmin + 1, ymax - ymin + 1], "rle": runs}',
     '    return {"box": [xmin, ymin, xmax - xmin, ymax - ymin], "rle": runs}',
     "SAM's trailing box is inclusive; dropping the +1 makes every mask one pixel "
     "short in each direction and the RLE no longer fits it", "py"),

    ("masks_keyframes_bulk", "plugins/masks/plugin.py",
     '                for item in (d.get("frames") or []):\n'
     '                    into[int(item["frame"])] = item.get("payload")',
     '                item = (d.get("frames") or [None])[0]\n'
     '                if item:\n'
     '                    into[int(item["frame"])] = item.get("payload")',
     "a propagation that folds to one keyframe leaves 59 frames of a track that the "
     "op log says exist", "py"),

    ("sam_duplicate_labels", "plugins/sam/auto.py",
     '    if len(set(ls)) != len(ls):',
     '    if False:',
     "two prompts sharing a label compete for one identity pool and are "
     "indistinguishable afterwards — found 40 minutes into a GPU run, or now", "py"),

    ("sam_prompts_sent_twice", "plugins/sam/auto.py",
     "    out: list[str] = []\n    if prompts:",
     "    out: list[str] = []\n    if True:",
     "the service takes the categories as fields and builds those flags itself; "
     "sending them in the flags as well gives the pipeline each prompt twice, and "
     "an hour of GPU goes by before anybody sees four categories", "py"),

    ("sam_points_stack_instead_of_toggling", "plugins/sam/web/sam.js",
     "      const found = pointUnder(world, cell);",
     "      const found = null;",
     "clicking a prompt point you already placed stacks a second one on top instead "
     "of taking it back, so a misplaced negative can only be undone by throwing the "
     "whole prompt away and starting again",
     "js"),

    ("client_members_unfiltered", "plugins/identity/web/identity.js",
     "          const id = idx.get(`${stream}/${key}`);\n          if (id !== undefined) out.push(id);",
     "          const o = app().findObject(stream, key);\n          if (o) out.push(o.id);",
     "identity membership built from names rather than from what is on screen hands back "
     "tracks that no longer exist",
     "js"),
]

PY_SUITES = ["tests/test_core.py", "tests/test_masks.py", "tests/test_props.py",
             "tests/test_sam.py", "tests/test_service.py"]


def run_suites(kind: str = "py") -> tuple[bool, str]:
    """True if everything passed. `kind` is "py", "js" or "all"."""
    failed = []
    suites = list(PY_SUITES) if kind in ("py", "all") else []
    for suite in suites:
        p = subprocess.run([PY, str(ROOT / suite)], capture_output=True, text=True,
                           cwd=ROOT, timeout=300)
        if p.returncode != 0:
            names = [l.strip()[5:].split(":")[0]
                     for l in p.stdout.splitlines() if l.strip().startswith("FAIL")]
            failed.append(f"{Path(suite).stem}({', '.join(names[:3]) or 'error'})")
    if kind in ("js", "all"):
        p = subprocess.run(["node", "tests/smoke.mjs"], capture_output=True, text=True,
                           cwd=ROOT, timeout=600,
                           env={**os.environ, "QUORUM_URL": QUORUM_URL})
        if p.returncode != 0:
            names = [l.strip()[5:].split(":")[0].strip()
                     for l in p.stdout.splitlines() if l.strip().startswith("FAIL")]
            failed.append(f"smoke({', '.join(names[:2]) or 'error'})")
    return (not failed), "; ".join(failed)


# A mutation run edits real source files. `try/finally` puts them back — but a
# SIGTERM (a CI timeout, a `timeout 120`, somebody's Ctrl+C reaching the wrong
# process) kills Python without unwinding, and then a *load-bearing line is
# still broken on disk*. That happened; it silently disabled a client error
# path and cost an afternoon of debugging a "bug" that was this file's litter.
#
# So the original is written to a sidecar before the edit and removed after,
# and any sidecar found at startup is restored first. That survives SIGKILL,
# which try/finally cannot.
SCARS = ROOT / "tests" / ".mutants-in-flight"


def park(path: pathlib.Path, original: str) -> pathlib.Path:
    SCARS.mkdir(exist_ok=True)
    scar = SCARS / (path.name + ".original")
    scar.write_text(original)
    (SCARS / (path.name + ".where")).write_text(str(path))
    return scar


def unpark(path: pathlib.Path) -> None:
    for suffix in (".original", ".where"):
        (SCARS / (path.name + suffix)).unlink(missing_ok=True)
    if SCARS.is_dir() and not any(SCARS.iterdir()):
        SCARS.rmdir()


def heal() -> list[str]:
    """Put back anything a previous run was killed in the middle of."""
    healed = []
    if not SCARS.is_dir():
        return healed
    for where in sorted(SCARS.glob("*.where")):
        target = pathlib.Path(where.read_text().strip())
        original = SCARS / (target.name + ".original")
        if original.exists() and target.exists():
            if target.read_text() != original.read_text():
                target.write_text(original.read_text())
                healed.append(str(target))
        unpark(target)
    return healed


def verify() -> list[str]:
    """Is every load-bearing line on disk the one this file expects?

    `heal()` restores from the sidecar, which is the right first answer but not
    a complete one: the sidecar can itself be lost — a second run that healed
    and cleaned it up, a directory somebody deleted, a checkout that never had
    it — and then a mutated line simply *stays* on disk. It reads as an
    unrelated failing test somewhere else, which is exactly how far this can be
    from the file that caused it. This check needs no sidecar: it asserts every
    original is present and no replacement is, and it is cheap enough to run
    before every suite.
    """
    problems = []
    for name, path, find, repl, _why, _kind in MUTATIONS:
        text = (ROOT / path).read_text()
        n = text.count(find)
        # The original being present *is* the proof it is not mutated: applying
        # one replaces it. Testing for the replacement instead gives a false
        # alarm whenever a mutation shortens a line rather than changing it —
        # `fold_purge_beats_delete` deletes the second of two lines, so its
        # "replacement" is a prefix of its original and is always found.
        if n == 1:
            continue
        if n == 0 and repl in text:
            problems.append(f"{path}: `{name}` is still MUTATED on disk — put it back")
        elif n == 0:
            problems.append(f"{path}: the line `{name}` mutates is gone — the code moved, "
                            f"and this mutation now checks nothing")
        else:
            problems.append(f"{path}: `{name}` matches {n} places, so mutating it would "
                            f"change more than one — narrow the pattern")
    return problems


QUORUM_URL = os.environ.get("QUORUM_URL", "http://127.0.0.1:8600")


def server_up() -> bool:
    """Is there a server for the browser mutations — and is it the one you
    meant? The browser suite makes edits, so `QUORUM_URL` is honoured here for
    the same reason `tests/run.sh` honours it: 8600 is not always the dev
    server."""
    try:
        import urllib.request
        urllib.request.urlopen(QUORUM_URL + "/health", timeout=3).read()
        return True
    except Exception:
        return False


def main(argv):
    # First, before anything can be confused by it: undo the damage of a run
    # that was killed rather than finished.
    for f in heal():
        print(f"  !!   put back {f} — a previous run was killed while it was mutated")
    problems = verify()
    if "--verify" in argv:
        for p in problems:
            print(f"  !!   {p}")
        print("every mutation's line is intact" if not problems
              else f"{len(problems)} source file(s) are not what this file expects")
        return 1 if problems else 0
    for p in problems:
        print(f"  !!   {p}")
    if problems:
        return 2
    want = set(a for a in argv[1:] if not a.startswith("-"))
    only = "js" if "--js" in argv else "py" if "--py" in argv else None
    muts = [m for m in MUTATIONS
            if (not want or m[0] in want) and (not only or m[5] == only)]
    if not muts:
        print(f"no mutation named {', '.join(want)}")
        return 2

    needs_js = any(m[5] == "js" for m in muts)
    if needs_js and not server_up():
        print(f"the browser mutations need a server at {QUORUM_URL}: ./dev.sh start")
        return 2

    ok, why = run_suites("all" if needs_js else "py")
    if not ok:
        print(f"the suite is already red ({why}) — fix that before checking for holes")
        return 2

    survivors, killed = [], []
    for name, rel, find, replace, why, kind in muts:
        path = ROOT / rel
        original = path.read_text()
        if find not in original:
            print(f"  ??   {name}: the line it mutates has moved — update tests/mutants.py")
            survivors.append((name, "stale mutation"))
            continue
        park(path, original)
        try:
            path.write_text(original.replace(find, replace, 1))
            passed, failing = run_suites(kind)
        finally:
            path.write_text(original)
            unpark(path)
        if passed:
            print(f"  HOLE {name}: broke {rel} and every test still passed")
            print(f"       what goes wrong in the real world: {why}")
            survivors.append((name, why))
        else:
            killed.append(name)
            print(f"  ok   {name}  → caught by {failing}")

    print(f"\n{len(killed)}/{len(muts)} mutations caught")
    if survivors:
        print("\nholes:")
        for n, w in survivors:
            print(f"  · {n} — {w}")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
