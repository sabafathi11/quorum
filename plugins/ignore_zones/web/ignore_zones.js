import { decodeRLE } from '../../mask_layer/web/mask.js';

// A zone is a full-resolution local bitmap until Save.  This is intentionally
// the same pixel model as mask repair, but it writes a separate ignore record.
export default {
  id: 'ignore_zones',
  activate(ctx) {
    const app = () => window.quorum;
    // Inspect is deliberately the safe default: opening this tool or clicking
    // a frame must never create or alter an ignore zone.
    const S = { zones: {}, paint: null, brush: 18, mode: 'inspect' };
    const paintCursor = () => {
      const stage = ctx.viewport?.stage;
      if (!stage) return;
      stage.classList.toggle('ignore-brush-cursor', !!S.paint && S.mode === 'paint');
      stage.classList.toggle('ignore-eraser-cursor', !!S.paint && S.mode === 'erase');
      stage.classList.toggle('mask-pan-cursor', S.mode === 'inspect');
    };
    const streamOf = (k) => ctx.store.get('capture')?.streams.find((s) => s.key === k);
    const frameOf = (stream) => app().primaryData()?.mapFrame(stream, ctx.store.get('frame')) ?? ctx.store.get('frame');
    // Zones are keyframes, not one-frame flashes: the most recent edit in a
    // camera remains active until another edit changes or clears it.
    const zone = (stream, frame) => {
      const frames = S.zones?.[stream] || {};
      const prior = Object.keys(frames).map(Number).filter((f) => f <= frame);
      return prior.length ? frames[String(Math.max(...prior))] : null;
    };
    const load = async () => {
      const cap = ctx.store.get('capture'); if (!cap) return;
      Object.assign(S, await ctx.call(`/${cap.id}/state`)); ctx.display.changed(); ctx.invalidate(); app().renderInspector();
    };
    ctx.on('capture', load); if (ctx.store.get('capture')) load();
    ctx.onOp((op) => { if (op.kind?.startsWith('ignore_zones.')) load(); });

    const contains = (z, m) => {
      if (!z || !m) return false;
      const [zl, zt, zw, zh] = z.box, [ml, mt, mw, mh] = m.box;
      if (ml < zl || mt < zt || ml + mw > zl + zw || mt + mh > zt + zh) return false;
      const a = decodeRLE(m.rle, mw, mh), b = decodeRLE(z.rle, zw, zh);
      for (let y = 0; y < mh; y++) for (let x = 0; x < mw; x++)
        if (a[y * mw + x] && !b[(mt - zt + y) * zw + ml - zl + x]) return false;
      return a.some(Boolean);
    };
    const baked = new Map();
    const zoneCanvas = (z) => {
      const key = `${z.box.join(',')}|${z.rle}`;
      if (baked.has(key)) return baked.get(key);
      const [, , w, h] = z.box, a = decodeRLE(z.rle, w, h), im = new ImageData(w, h);
      for (let i = 0; i < a.length; i++) if (a[i]) { im.data[i * 4] = 30; im.data[i * 4 + 1] = 35; im.data[i * 4 + 2] = 42; im.data[i * 4 + 3] = 145; }
      const cv = document.createElement('canvas'); cv.width = w; cv.height = h; cv.getContext('2d').putImageData(im, 0, 0);
      baked.set(key, cv); if (baked.size > 80) baked.delete(baked.keys().next().value); return cv;
    };
    // This is a frame-sensitive filter. entriesAt supplies the payload, so it
    // gates drawing and hit testing without hiding a track on other frames.
    ctx.registerFilter({ id: 'contained', title: 'detections inside ignore zones', structural: true,
      test: (entry) => contains(zone(entry.stream.key, frameOf(entry.stream.key)), entry.payload) });

    const make = (stream) => {
      const st = streamOf(stream), frame = frameOf(stream), old = zone(stream, frame);
      const px = new Uint8Array(st.width * st.height);
      if (old) { const [l, t, w, h] = old.box, a = decodeRLE(old.rle, w, h);
        for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) if (a[y * w + x]) px[(t + y) * st.width + l + x] = 1; }
      const cv = document.createElement('canvas'); cv.width = st.width; cv.height = st.height;
      S.paint = { stream, frame, w: st.width, h: st.height, px, cv, dirty: false, last: null };
      // The Save / Discard controls depend on `S.paint`. Creating a draft from
      // the canvas must therefore render the inspector immediately, rather
      // than waiting for an unrelated selection or frame update.
      redraw(); app().renderInspector();
    };
    const redraw = () => { const p = S.paint; if (!p) return; const im = new ImageData(p.w, p.h);
      for (let i = 0; i < p.px.length; i++) if (p.px[i]) { im.data[i * 4] = 30; im.data[i * 4 + 1] = 35; im.data[i * 4 + 2] = 42; im.data[i * 4 + 3] = 185; }
      p.cv.getContext('2d').putImageData(im, 0, 0); ctx.invalidate(); };
    const point = (world, cell, p) => !cell || cell.stream !== p.stream ? null : {
      x: Math.floor((world.x - cell.x) * p.w / cell.w), y: Math.floor((world.y - cell.y) * p.h / cell.h) };
    const stamp = (q) => { const p = S.paint, r = Math.max(.5, S.brush / 2); if (!q || q.x < 0 || q.y < 0 || q.x >= p.w || q.y >= p.h) return;
      for (let y = Math.max(0, Math.floor(q.y - r)); y <= Math.min(p.h - 1, Math.ceil(q.y + r)); y++) for (let x = Math.max(0, Math.floor(q.x - r)); x <= Math.min(p.w - 1, Math.ceil(q.x + r)); x++)
        if ((x - q.x) ** 2 + (y - q.y) ** 2 <= r ** 2) p.px[y * p.w + x] = S.mode === 'erase' ? 0 : 1;
      p.dirty = true; redraw(); };
    const payload = () => { const p = S.paint; let x0 = p.w, y0 = p.h, x1 = -1, y1 = -1;
      for (let y = 0; y < p.h; y++) for (let x = 0; x < p.w; x++) if (p.px[y * p.w + x]) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
      if (x1 < 0) return null; const w = x1 - x0 + 1, h = y1 - y0 + 1, runs = []; let on = false, n = 0;
      for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) { const next = !!p.px[y * p.w + x]; if (next !== on) { runs.push(n); n = 0; on = next; } n++; } runs.push(n);
      return { box: [x0, y0, w, h], rle: runs.join(',') }; };
    const save = async () => { const p = S.paint; if (!p || !p.dirty) return; const cap = ctx.store.get('capture');
      await ctx.call(`/${cap.id}/set`, { method: 'POST', body: { stream: p.stream, frame: p.frame, payload: payload() } }); S.paint = null; S.mode = 'inspect'; paintCursor(); await load(); };
    const discard = () => { S.paint = null; S.mode = 'inspect'; paintCursor(); ctx.invalidate(); app().renderInspector(); };
    ctx.setPointerHandler('ignore-zones', ({ phase, world, cell }) => {
      if (S.mode !== 'paint' && S.mode !== 'erase') return false;
      if (phase === 'down' && !S.paint) { if (!cell) return false; make(cell.stream); }
      if (!S.paint) return false; const q = point(world, cell, S.paint); if (phase === 'down' || phase === 'move') stamp(q); if (phase === 'up') S.paint.last = null; return true; });
    ctx.on('drawn', ({ g }) => { const p = S.paint, cap = ctx.store.get('capture'); if (!cap) return; g.save(); g.imageSmoothingEnabled = false;
      for (const c of cap.layout?.cells || []) { const z = zone(c.stream, frameOf(c.stream)); if (!z || (p && p.stream === c.stream)) continue; const [l, t, w, h] = z.box, st = streamOf(c.stream); g.drawImage(zoneCanvas(z), c.x + l * c.w / st.width, c.y + t * c.h / st.height, w * c.w / st.width, h * c.h / st.height); }
      if (p) { const c = cap.layout?.cells.find((x) => x.stream === p.stream); if (c) g.drawImage(p.cv, c.x, c.y, c.w, c.h); } g.restore(); });
    ctx.registerCommand({ id: 'paint', title: 'Paint ignore zone', keys: ['B'], tool: 'ignore-zones', run: () => { S.mode = 'paint'; paintCursor(); app().renderInspector(); } });
    ctx.registerCommand({ id: 'erase', title: 'Erase ignore zone pixels', keys: ['E'], tool: 'ignore-zones', run: () => { S.mode = 'erase'; paintCursor(); app().renderInspector(); } });
    ctx.registerCommand({ id: 'inspect', title: 'Inspect or pan without editing ignore zones', keys: ['V'], tool: 'ignore-zones', run: () => { S.mode = 'inspect'; paintCursor(); app().renderInspector(); } });
    ctx.registerCommand({ id: 'save', title: 'Save ignore zone', keys: ['Enter'], tool: 'ignore-zones', run: save });
    ctx.registerCommand({ id: 'discard', title: 'Discard ignore-zone changes', keys: ['Esc'], tool: 'ignore-zones', run: discard });
    ctx.registerInspector('ignore-zones', () => { const h = ctx.h, p = S.paint;
      const range = h('input', { type: 'range', min: 1, max: 256, oninput: (e) => { S.brush = +e.target.value; app().renderInspector(); } }); range.value = S.brush;
      return ctx.ui.panel('Ignore zones', [h('div', { class: 'hint' }, p ? `Editing ${p.stream}, source frame ${p.frame}.` : 'Drag in a camera to paint an ignore zone. It remains active until a later edit in that camera.'),
        h('div', { class: 'row' }, h('button', { class: `btn sm${S.mode === 'paint' ? ' primary' : ''}`, onclick: () => { S.mode = 'paint'; paintCursor(); app().renderInspector(); } }, 'Brush', h('kbd', {}, 'B')), h('button', { class: `btn sm${S.mode === 'erase' ? ' danger' : ''}`, onclick: () => { S.mode = 'erase'; paintCursor(); app().renderInspector(); } }, 'Eraser', h('kbd', {}, 'E')), h('button', { class: `btn sm${S.mode === 'inspect' ? ' primary' : ''}`, onclick: () => { S.mode = 'inspect'; paintCursor(); app().renderInspector(); } }, 'Inspect', h('kbd', {}, 'V'))),
        h('div', { class: 'field' }, h('label', {}, `Thickness · ${S.brush}px`), range), p ? h('div', { class: 'row' }, h('button', { class: 'btn sm primary', onclick: save }, 'Save', h('kbd', {}, 'Enter')), h('button', { class: 'btn sm', onclick: discard }, 'Discard', h('kbd', {}, 'Esc'))) : null,
        h('div', { class: 'hint' }, 'V inspect/pan · B brush · E eraser. Only a fully contained detection is ignored; source video is unchanged.')], { id: 'ignore-zones' }); });
  },
};
