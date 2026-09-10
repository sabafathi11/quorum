// The SAM workspace: click a person, get a mask; or set a whole camera going.
//
// Three things happen here and they are deliberately kept apart on screen,
// because they cost wildly different amounts and only one of them is free to
// get wrong:
//
//   Prompt      clicks accumulate locally. Send them when ready to ask SAM
//               for a preview; nothing is written until a later commit.
//   Commit      the preview becomes a mask edit — through `masks`, as you, on
//               one track — which means Ctrl+Z and the History panel work on
//               it exactly like a cut or a join.
//   Auto        hours of GPU over whole cameras, from text prompts. Its own
//               panel, its own progress, its own stop button.
//
// The decode comes from `mask_layer`, by relative path, which is the same
// borrow `masks/plugin.py` makes on the server for the same reason: there is
// one run convention here and a second copy of it would drift.
import { decodeRLE } from '../../mask_layer/web/mask.js';

// The preview is redrawn every frame while it exists; decoding a 200×400 mask
// sixty times a second for a picture that has not changed is silly, so the
// tinted canvas is kept until the payload actually changes.
let baked = null;
function bakePreview(payload, color) {
  const sig = `${payload.box.join(',')}|${payload.rle.length}|${color}`;
  if (baked?.sig === sig) return baked;
  const [, , w, h] = payload.box;
  if (!w || !h) return null;
  const alpha = decodeRLE(payload.rle, w, h);
  const img = new ImageData(w, h);
  const px = img.data;
  const [r, g, b] = color;
  for (let i = 0; i < alpha.length; i++) {
    if (!alpha[i]) continue;
    px[i * 4] = r; px[i * 4 + 1] = g; px[i * 4 + 2] = b; px[i * 4 + 3] = 255;
  }
  const cv = document.createElement('canvas');
  cv.width = w; cv.height = h;
  cv.getContext('2d').putImageData(img, 0, 0);
  baked = { sig, canvas: cv, w, h };
  return baked;
}

const PREVIEW = [86, 200, 255];        // the "not committed yet" colour

export default {
  id: 'sam',

  activate(ctx) {
    const app = () => window.quorum;
    const S = {
      stream: null,          // the cell the prompt lives in; one at a time
      pos: [], neg: [],      // prompt points, in that stream's own pixels
      preview: null,         // {box, rle} — a suggestion, never a record
      streamFrame: null,     // the frame it was segmented on
      captureFrame: null,
      roi: null,             // what was cropped, so the panel can say so
      timing: null,
      busy: false,
      useRoi: true,
      useBox: true,          // seed the model with the selected track's box
      label: 'person',       // what a track created from scratch is called
      status: null,
      runs: null,
      autoDefaults: {},
      request: 0,            // invalidates an old response after prompt changes
    };

    const capture = () => ctx.store.get('capture');
    const streamOf = (key) => (capture()?.streams || []).find((s) => s.key === key);

    // ------------------------------------------------------------- geometry
    // Capture space -> that camera's own pixels. The server is never told how
    // big the cell was drawn, because it would be wrong the moment somebody
    // resized a window; it is told a frame number and a pixel coordinate, both
    // of which mean the same thing at every zoom level.
    const toStream = (world, cell) => {
      const st = streamOf(cell.stream);
      const sx = (st?.width || cell.w) / cell.w;
      const sy = (st?.height || cell.h) / cell.h;
      return [Math.round((world.x - cell.x) * sx), Math.round((world.y - cell.y) * sy)];
    };
    const toWorld = (x, y, cell) => {
      const st = streamOf(cell.stream);
      const sx = cell.w / (st?.width || cell.w);
      const sy = cell.h / (st?.height || cell.h);
      return { x: cell.x + x * sx, y: cell.y + y * sy, sx, sy };
    };
    const cellOf = (key) => (capture()?.layout?.cells || []).find((c) => c.stream === key);

    // Is there already a prompt point where you just clicked? Measured in
    // *screen* pixels, because the dots are drawn at a fixed screen size — a
    // radius in capture space would be an easy target zoomed in and an
    // impossible one zoomed out.
    const HIT_PX = 9;
    const pointUnder = (world, cell) => {
      if (!S.stream || S.stream !== cell.stream) return null;
      const r = HIT_PX / (app().viewport?.scale || 1);
      for (const list of [S.pos, S.neg]) {
        for (let i = 0; i < list.length; i++) {
          const p = toWorld(list[i][0], list[i][1], cell);
          if (Math.hypot(p.x - world.x, p.y - world.y) <= r) return { list, i };
        }
      }
      return null;
    };

    // The track a commit would land on: the selection, narrowed to the camera
    // being prompted. Editing is per-track here for the same reason it is in
    // the Edit workspace — a mask belongs to one camera, and "this person in
    // six cameras" is the Identity tool's question, not this one's.
    const target = () => {
      const A = app();
      for (const id of ctx.store.get('selection')) {
        const o = A.objectOf(id);
        if (o && (!S.stream || o.stream === S.stream)) return o;
      }
      return null;
    };

    const streamFrameNow = (skey) => {
      const A = app();
      const data = A.primaryData?.();
      const f = ctx.store.get('frame');
      return data ? data.mapFrame(skey, f) : f;
    };

    // ------------------------------------------------------------- prompting
    const reset = (quiet = false) => {
      S.request++;
      S.pos = []; S.neg = []; S.preview = null; S.roi = null; S.timing = null;
      S.stream = null; S.streamFrame = null; S.captureFrame = null;
      baked = null;
      ctx.invalidate();
      app().renderInspector();
      if (!quiet) ctx.toast('Prompt cleared', 'Nothing was written.');
    };

    // Points and segmentation settings are one prompt. Once either changes,
    // a preview made from the previous prompt must not look committable.
    const promptChanged = () => {
      S.request++;
      S.preview = null; S.roi = null; S.timing = null; baked = null;
      ctx.invalidate();
      app().renderInspector();
    };

    // A prompt is tied to the frame it was made on. Stepping away from that
    // frame and pressing Enter would write a mask that was never looked at on
    // the picture it is being written onto — so the prompt is dropped instead,
    // out loud.
    ctx.store.on(['frame'], () => {
      if (S.captureFrame != null && ctx.store.get('frame') !== S.captureFrame
          && (S.pos.length || S.neg.length)) {
        reset(true);
        ctx.toast('Prompt dropped', 'It belonged to the frame you left.', 'warn');
      }
    });

    const segment = async () => {
      if (!S.pos.length) { S.preview = null; ctx.invalidate(); return; }
      if (S.busy) return;
      const cap = capture();
      const t = target();
      const request = ++S.request;
      const pos = S.pos.map(([x, y]) => [x, y]);
      const neg = S.neg.map(([x, y]) => [x, y]);
      let bbox = null;
      if (S.useBox && t) {
        const e = ctx.entriesAt(ctx.store.get('frame'))
          .find((x) => x.object.id === t.id);
        if (e?.payload?.box) {
          const [l, tp, w, h] = e.payload.box;
          bbox = [l, tp, l + w, tp + h];
        }
      }
      S.busy = true;
      app().renderInspector();
      try {
        const r = await ctx.call(`/${cap.id}/interact`, {
          method: 'POST', quiet: true,
          body: { stream: S.stream, frame: S.captureFrame, stream_frame: S.streamFrame,
                  pos, neg, bbox, roi: S.useRoi },
        });
        if (request !== S.request) return;
        S.preview = r.payload;
        S.roi = r.roi;
        S.timing = r.ms;
        S.streamFrame = r.stream_frame;
        if (r.empty) {
          ctx.toast('SAM found nothing there',
                    'It answered “no object”, which is an answer. Try another point, or '
                    + 'turn the crop off if the thing you want is large.', 'warn');
        }
      } catch (e) {
        if (request !== S.request) return;
        S.preview = null;
        ctx.toast('SAM could not segment that', e.message || String(e), 'err');
      } finally {
        S.busy = false;
        baked = null;
        ctx.invalidate();
        app().renderInspector();
      }
    };

    // A plain click here is a *prompt point*, not a selection — this is the one
    // tool where clicking a mask must not change what is selected, because the
    // selection is the track a commit will land on and it has to survive the
    // four clicks it takes to get a good mask. Selecting is alt+click, which
    // means the same thing it means everywhere else (reach the one underneath,
    // and cycle). `tests/invariants.mjs` knows about this and checks the alt
    // path instead of waiving the rule.
    ctx.setPickHandler('sam', ({ hit, stack, alt, event, world, cell, right }) => {
      const A = app();
      if (alt) {
        if (hit) { A.select([hit.object.id], 'set'); A.pulse([hit.object.id], { ms: 700, rings: 2 }); }
        else A.clearSelection();
        A.renderInspector();
        return true;
      }
      if (!cell) { reset(true); return true; }
      if (S.stream && S.stream !== cell.stream) {
        ctx.toast('One camera at a time',
                  `The prompt is on ${S.stream}. Esc first to start one on ${cell.stream}.`, 'warn');
        return true;
      }
      // Clicking a point you have already placed removes it. Stacking a
      // second point on the same spot is the alternative and it is never what
      // anybody means — the points *are* the prompt, and a prompt you cannot
      // take one piece out of is one you have to throw away and rebuild.
      // `Backspace` still drops the most recent point; this is how you drop a
      // particular one, including a negative you put in the wrong place.
      const found = pointUnder(world, cell);
      if (found) {
        found.list.splice(found.i, 1);
        promptChanged();
        return true;
      }

      S.stream = cell.stream;
      S.captureFrame = ctx.store.get('frame');
      S.streamFrame = streamFrameNow(cell.stream);
      const p = toStream(world, cell);
      (right || event.shiftKey ? S.neg : S.pos).push(p);
      promptChanged();
      return true;
    });

    // ---------------------------------------------------------------- commit
    const commit = async () => {
      if (!S.preview) {
        ctx.toast('Nothing to commit', 'Click on something first — Enter writes the preview.', 'warn');
        return;
      }
      const cap = capture();
      const t = target();
      // Held before `reset` clears them: the pulse afterwards has to know which
      // camera and which key it is pointing at, and the prompt state is gone
      // by then.
      const stream = S.stream;
      const frame = S.streamFrame;
      const payload = { ...S.preview, outside: 0 };
      // The server echoes every op through the WebSocket.  Mark this request
      // before sending it so the masks workspace recognises that echo even if
      // it arrives before the HTTP response, rather than materialising and
      // reloading the just-saved track a second time.
      const clientRef = globalThis.crypto?.randomUUID?.()
        || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      ctx.emit('masks.local-ref', clientRef);
      const post = (kind, body) => ctx.api.plugin('masks', `/${cap.id}/op`,
                                                  { method: 'POST', body: { kind, payload: body, client_ref: clientRef },
                                                    quiet: true });
      try {
        let key = t?.key;
        if (t) {
          await post('keyframe', { stream, key, frame, payload });
          ctx.toast('Mask replaced', `${stream}/${key} at frame ${frame}. Ctrl+Z takes it back.`);
        } else {
          const { key: made } = await ctx.call(
            `/${cap.id}/newkey?stream=${encodeURIComponent(stream)}`);
          key = made;
          await post('create', {
            stream, key, label: S.label || 'person',
            // Two keyframes, the second `outside`: a lone mask with no end
            // would linger to the end of the video as a track nobody drew.
            frames: [{ frame, outside: 0, payload },
                     { frame: frame + 1, outside: 1, payload }],
          });
          ctx.toast('Track created', `${stream}/${key} — press R to carry it forward.`);
        }
        reset(true);
        await ctx.reloadLayers();
        const made = app().findObject(stream, key);
        if (made) { app().select([made.id], 'set'); app().pulse([made.id], { ms: 1200 }); }
      } catch (e) {
        ctx.emit('masks.cancel-local-ref', clientRef);
        if (e.status === 409) ctx.toast('That edit cannot apply', e.message, 'warn');
        else ctx.toast('Could not write the mask', e.message || String(e), 'err');
      }
    };

    // ------------------------------------------------------------- propagate
    const propagate = async (backward = false) => {
      const t = target();
      if (!t) {
        ctx.toast('Nothing selected',
                  'Alt+click a mask (or commit one) — propagation carries an existing track.',
                  'warn');
        return;
      }
      const cap = capture();
      const from = streamFrameNow(t.stream);
      const { job } = await ctx.job('track', {
        capture_id: cap.id, stream: t.stream, key: t.key, frame: from,
        count: ctx.store.get('samCount') || 60,
        stride: ctx.store.get('samStride') || 1,
        backward, roi: S.useRoi,
      });
      app().watchJob(job, async (j) => {
        if (j.state === 'done') {
          ctx.toast('Propagated', j.message || '');
          await ctx.reloadLayers();
          const o = app().findObject(t.stream, t.key);
          if (o) app().pulse([o.id], { ms: 1400 });
        } else if (j.state === 'failed') {
          ctx.toast('Propagation failed', j.message || '', 'err');
        }
      });
    };

    // ------------------------------------------------------------- the paint
    // Drawn on the host's `drawn` event rather than as a layer renderer,
    // because a preview is not a layer: it has no object, no id and no place
    // in the op log, and giving it one would make it selectable, hideable and
    // exportable — three things it must not be until somebody presses Enter.
    ctx.on('drawn', ({ g, view }) => {
      if (ctx.store.get('tool') !== 'sam' || !S.stream) return;
      const cell = cellOf(S.stream);
      if (!cell) return;
      g.save();
      if (S.roi) {
        const a = toWorld(S.roi[0], S.roi[1], cell);
        const b = toWorld(S.roi[0] + S.roi[2], S.roi[1] + S.roi[3], cell);
        g.strokeStyle = 'rgba(86,200,255,.45)';
        g.setLineDash([6 / view.scale, 4 / view.scale]);
        g.lineWidth = 1 / view.scale;
        g.strokeRect(a.x, a.y, b.x - a.x, b.y - a.y);
        g.setLineDash([]);
      }
      if (S.preview) {
        const img = bakePreview(S.preview, PREVIEW);
        if (img) {
          const [l, t, w, h] = S.preview.box;
          const a = toWorld(l, t, cell);
          g.imageSmoothingEnabled = false;
          g.globalAlpha = 0.55;
          g.drawImage(img.canvas, a.x, a.y, w * a.sx, h * a.sy);
          g.globalAlpha = 1;
          g.strokeStyle = `rgb(${PREVIEW.join(',')})`;
          g.lineWidth = 1.5 / view.scale;
          g.strokeRect(a.x, a.y, w * a.sx, h * a.sy);
        }
      }
      const r = 4 / view.scale;
      for (const [pts, fill] of [[S.pos, '#5CE07A'], [S.neg, '#E0645C']]) {
        for (const [x, y] of pts) {
          const p = toWorld(x, y, cell);
          g.beginPath();
          g.arc(p.x, p.y, r, 0, Math.PI * 2);
          g.fillStyle = fill;
          g.fill();
          g.lineWidth = 1.5 / view.scale;
          g.strokeStyle = 'rgba(0,0,0,.7)';
          g.stroke();
        }
      }
      g.restore();
    });

    // --------------------------------------------------------------- keymap
    // `Space` is play/pause in every tool, so it is not touched. `Enter` and
    // `Esc` are the pair the desktop tool used and are worth keeping: one
    // accepts what is on screen, the other throws it away, and neither ever
    // means anything else here.
    for (const [id, title, keys, run] of [
      ['preview', 'Send prompt dots to SAM and update the preview', ['Ctrl+Enter'], segment],
      ['commit', 'Write the previewed mask', ['Enter'], commit],
      ['clear', 'Throw the prompt away', ['Esc'], () => reset()],
      ['undo-point', 'Take back the last point', ['Backspace'], () => {
        if (S.neg.length > S.pos.length) S.neg.pop(); else S.pos.pop();
        promptChanged();
      }],
      ['propagate', 'Carry the selected mask forward', ['R'], () => propagate(false)],
      ['propagate-back', 'Carry the selected mask backward', ['Shift+R'], () => propagate(true)],
      ['roi', 'Crop to the object before segmenting', ['C'], () => {
        S.useRoi = !S.useRoi;
        ctx.toast('Crop', S.useRoi ? 'on — small objects arrive full size' : 'off — SAM sees the whole frame');
        promptChanged();
      }],
    ]) ctx.registerCommand({ id, title, keys, tool: 'sam', group: 'SAM', run });

    // --------------------------------------------------------------- status
    // The server caches its answer, so this is cheap enough to ask on every
    // capture open and every switch into this tool. `fresh` is the recheck
    // button: the one moment the cache is wrong is the minute after somebody
    // deploys the function, and that is exactly when somebody is looking.
    const loadStatus = async (fresh = false) => {
      try { S.status = await ctx.call(`/status${fresh ? '?fresh=1' : ''}`, { quiet: true }); }
      catch (e) { S.status = { endpoint: { ok: false, why: e.message || String(e) } }; }
      app().renderInspector();
    };
    const loadRuns = async () => {
      const cap = capture();
      if (!cap) return;
      try { S.runs = await ctx.call(`/${cap.id}/runs`, { quiet: true }); }
      catch { S.runs = null; }
      app().renderInspector();
    };
    ctx.on('capture', () => { reset(true); loadStatus(); loadRuns(); });
    ctx.on('tool', (t) => { if (t?.id === 'sam') { loadStatus(); loadRuns(); } });

    // ---------------------------------------------------------- auto dialog
    const autoSpec = () =>
      (ctx.manifest.provides?.jobs || []).find((j) => j.kind === 'auto')?.params || {};

    const runAuto = async () => {
      const spec = { ...autoSpec() };
      delete spec.capture_id;                   // it is the capture you are in
      const f = ctx.ui.form(spec, S.autoDefaults);
      const go = await ctx.ui.modal({
        title: 'Auto-annotate from text prompts', ok: 'Start', wide: true,
        content: [
          ctx.h('div', { class: 'hint' },
            'One SAM 3.1 session per prompt per window — so N prompts cost about N times the '
            + 'GPU time, but the video is decoded and freeze-analysed once and every label '
            + 'lands in one layer. It runs inside the same process that holds the model, so '
            + 'clicking stays available — it just waits for the current window.'),
          f.el,
        ],
      });
      if (!go) return;
      S.autoDefaults = { ...f.values };
      const { job } = await ctx.job('auto', { ...f.values, capture_id: capture().id });
      ctx.toast('Auto-annotating', 'Progress is in this panel and in Jobs; Stop kills the container.');
      app().watchJob(job, async (j) => {
        await loadRuns();
        if (j.state === 'done') { ctx.toast('Auto-annotate finished', j.message || ''); await ctx.reloadLayers(); }
        else if (j.state === 'failed') ctx.toast('Auto-annotate failed', j.message || '', 'err');
      });
    };

    // ------------------------------------------------------------ inspector
    ctx.registerInspector('sam', (A) => {
      const h = ctx.h;
      const t = target();
      const st = S.status || {};
      const live = (ctx.store.get('jobs') || [])
        .filter((j) => j.plugin === 'sam' && ['queued', 'running'].includes(j.state));

      const chip = (label, ok, why) => h('span', {
        class: `tag ${ok ? 'ok' : 'bad'}`, title: why || '',
      }, label);

      const num = (label, storeKey, def, min, max) => {
        const inp = h('input', {
          class: 'txt', type: 'number', min, max,
          oninput: (e) => ctx.store.set({ [storeKey]: parseInt(e.target.value || def, 10) }),
        });
        inp.value = ctx.store.get(storeKey) ?? def;
        return h('div', { class: 'field' }, h('label', {}, label), inp);
      };

      const prompt = ctx.ui.panel('Prompt', [
        h('div', {},
          chip(st.endpoint?.ok ? 'endpoint up' : 'endpoint down',
               st.endpoint?.ok, st.endpoint?.why || st.url),
          st.ffmpeg === false ? chip('no ffmpeg', false, 'a frame cannot be read out of a video') : null,
          S.timing ? h('span', { class: 'tag' }, `${S.timing.frame}+${S.timing.model} ms`) : null),
        h('div', { class: 'hint' },
          S.stream
            ? `${S.stream} · frame ${S.streamFrame} · ${S.pos.length} positive, ${S.neg.length} negative`
            : 'Click a person: left adds a point on them, right adds one on what to leave out. '
              + 'Click a point again to take it back. Alt+click picks which track a commit '
              + 'replaces.'),
        !st.endpoint?.ok && st.endpoint?.why
          ? h('div', { class: 'hint' }, st.endpoint.why) : null,
        h('div', {},
          ctx.ui.stat('preview', S.busy ? '…' : S.preview ? `${S.preview.box[2]}×${S.preview.box[3]}` : '—'),
          ctx.ui.stat('target', t ? `${t.stream}/${t.key}` : 'new track'),
          ctx.ui.stat('crop', S.roi ? `${S.roi[2]}px` : S.useRoi ? 'auto' : 'off')),
        h('div', { class: 'row wrap' },
          h('button', { class: 'btn sm primary', onclick: segment, disabled: !S.pos.length || S.busy },
            S.busy ? 'Sending prompts...' : 'Send prompts', h('kbd', {}, 'Ctrl+Enter')),
          h('button', { class: 'btn sm primary', onclick: commit, disabled: !S.preview || S.busy },
            t ? 'Replace this mask' : 'Create a track', h('kbd', {}, '⏎')),
          h('button', { class: 'btn sm', onclick: () => reset() }, 'Clear', h('kbd', {}, 'Esc'))),
        t ? null : h('div', { class: 'field' }, h('label', {}, 'Label for the new track'),
          (() => {
            const i = h('input', { class: 'txt', oninput: (e) => { S.label = e.target.value; } });
            i.value = S.label;
            return i;
          })()),
        h('div', { class: 'row' },
          h('button', { class: `chipbtn${S.useRoi ? ' on' : ''}`, onclick: () => {
            S.useRoi = !S.useRoi; promptChanged();
          } }, 'crop to object'),
          h('button', { class: `chipbtn${S.useBox ? ' on' : ''}`, onclick: () => {
            S.useBox = !S.useBox; promptChanged();
          } }, 'use track box')),
        h('div', { class: 'hint' },
          'Dots stay in this browser until Send prompts (Ctrl+Enter). Nothing is written until you commit. A committed mask is a `masks` edit like any '
          + 'other — it is yours, it is in History, and Ctrl+Z takes it back.'),
      ], { id: 'sam-prompt' });

      const propagatePanel = ctx.ui.panel('Propagate', [
        h('div', { class: 'hint' },
          t ? `Carry ${t.stream}/${t.key} across its own camera from this frame. The whole `
              + 'propagation is one op.'
            : 'Select a mask (alt+click) or commit one, then carry it forward.'),
        h('div', { class: 'row' }, num('Frames', 'samCount', 60, 1, 600),
          num('Every Nth', 'samStride', 1, 1, 10)),
        h('div', { class: 'row wrap' },
          h('button', { class: 'btn sm', onclick: () => propagate(false), disabled: !t },
            'Forward', h('kbd', {}, 'R')),
          h('button', { class: 'btn sm', onclick: () => propagate(true), disabled: !t },
            'Backward', h('kbd', {}, '⇧R'))),
      ], { id: 'sam-propagate' });

      const runs = S.runs || {};
      const freezes = runs.freezes || [];
      const files = runs.files || [];
      const autoPanel = ctx.ui.panel('Auto-annotate', [
        // The same endpoint as the Prompt panel, because it is the same model.
        // What is worth saying twice is what it is *holding*: on a shared card
        // that is the number somebody wants before starting an hours-long run.
        h('div', {},
          chip(st.endpoint?.ok ? 'service up' : 'service down', st.endpoint?.ok, st.endpoint?.why),
          st.vram?.total ? h('span', { class: 'tag' },
            `${(st.vram.free / 1024).toFixed(1)} GB GPU free`) : null,
          st.busy ? h('span', { class: 'tag warn', title: st.job || '' }, 'busy') : null),
        st.vram?.total ? h('div', { class: 'hint' },
          `${(st.vram.used / 1024).toFixed(1)} of ${(st.vram.total / 1024).toFixed(1)} GB in use. `
          + 'A run needs about 10 GB on top of the model, and shares the card with the '
          + 'interactor — one thing at a time.') : null,
        h('div', { class: 'row' },
          h('button', { class: 'btn sm primary', onclick: runAuto },
            'Run the auto-annotator…'),
          h('span', { class: 'grow' }),
          h('button', {
            class: 'btn sm',
            title: 'Ask the server again — after deploying the function, or to pick up '
                   + 'a run somebody else started',
            onclick: async () => { await Promise.all([loadStatus(true), loadRuns()]); },
          }, 'recheck')),
        live.length ? h('div', {}, ...live.map((j) => h('div', { style: { marginTop: '6px' } },
          h('div', { class: 'row' },
            h('span', { class: 'k grow mono' }, j.kind),
            h('span', { class: 'tag warn' }, `${(j.progress * 100).toFixed(0)}%`),
            h('button', { class: 'btn sm danger', onclick: async () => {
              await ctx.api.cancelJob(j.id);
              ctx.toast('Stopping', 'The container is being killed; the job will say cancelled.');
            } }, 'Stop')),
          h('div', { class: 'bar' }, h('i', { style: { width: `${j.progress * 100}%` } })),
          h('div', { class: 'hint' }, j.message || '')))) : null,
        files.length ? h('div', {},
          h('div', { class: 'hint', style: { margin: '8px 0 4px' } }, 'Produced'),
          h('div', { class: 'list' }, ...files.map((f) => h('div', { class: 'item' },
            h('span', { class: 'k grow mono' }, f.name),
            f.partial ? h('span', { class: 'tag bad', title: 'a run that exited nonzero' }, 'partial') : null,
            h('span', { class: 'hint' }, `${(f.size / 1e6).toFixed(1)} MB`))))) : null,
        freezes.length ? h('div', {},
          h('div', { class: 'hint', style: { margin: '8px 0 4px' } },
            'Discontinuities — no track identity is carried across one'),
          h('div', { class: 'list' }, ...freezes.map((f) => h('div', { class: 'item' },
            h('span', { class: 'k grow mono' }, f.stream),
            h('span', { class: 'hint' },
              `${f.seconds_lost}s lost · ${f.dropped} holes · ${f.repeated} stalls · `
              + `${f.segments} segments`))))) : null,
        h('div', { class: 'hint' },
          'The result arrives as its own model layer, labelled per prompt. Nothing here '
          + 'touches masks you edited.'),
      ], { id: 'sam-auto' });

      return h('div', {}, prompt, propagatePanel, autoPanel);
    });
  },
};
