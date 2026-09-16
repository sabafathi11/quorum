// The Edit workspace: what the model drew, and what you did about it.
//
// Editing is per-track by default — the opposite of Identity, where a click
// means "this person". Getting that backwards is how the desktop tool let one
// keystroke delete a track in six cameras at once.

import { decodeRLE } from '../../mask_layer/web/mask.js';

const NEEDS_REBUILD = new Set(['split', 'unsplit', 'join', 'unjoin', 'keyframe', 'keyframes',
                               'create', 'purge', 'unpurge']);

export default {
  id: 'masks',

  activate(ctx) {
    const app = () => window.quorum;
    const S = {
      deleted: {}, purged: {}, splits: {}, joins: {}, shadow: {}, edited: {}, created: {},
      sourceLayer: null, derivedLayer: null,
      showDeleted: true,
      loaded: false, busy: false, localOps: new Set(), localRefs: new Set(),
      // A paint session remains local until Save. Its bitmap is in native
      // stream pixels, so zoom and layout never affect a one-pixel change.
      paint: null, brush: 5, label: 'mask', pointer: null,
    };
    const setOf = (m, s) => new Set(m[s] || []);
    const streamOf = (key) => (ctx.store.get('capture')?.streams || []).find((s) => s.key === key);
    const cellOf = (key) => (ctx.store.get('capture')?.layout?.cells || [])
      .find((c) => c.stream === key);
    const streamFrame = (object) => {
      const data = object?.layer_id != null
        ? app().layerData((l) => l.id === object.layer_id) : app().primaryData();
      return data ? data.mapFrame(object.stream, ctx.store.get('frame')) : ctx.store.get('frame');
    };
    const paintCursor = () => {
      const stage = ctx.viewport?.stage;
      if (!stage) return;
      stage.classList.toggle('mask-brush-cursor', !!S.paint && S.paint.mode === 'paint');
      stage.classList.toggle('mask-eraser-cursor', !!S.paint && S.paint.mode === 'erase');
    };

    // A tool that writes through the masks endpoint can reserve its request
    // token before the request leaves the browser. WebSocket delivery is not
    // ordered against the HTTP response, so this also covers an early echo.
    ctx.on('masks.local-ref', (ref) => { if (ref) S.localRefs.add(ref); });
    ctx.on('masks.cancel-local-ref', (ref) => S.localRefs.delete(ref));

    // ---------------------------------------------------------- pixel paint
    const activeEntry = (object) => ctx.entriesAt(ctx.store.get('frame'))
      .find((entry) => entry.object.id === object.id) || null;
    const refreshPaint = (p, rect = null) => {
      const x0 = rect ? Math.max(0, rect.x0) : 0;
      const y0 = rect ? Math.max(0, rect.y0) : 0;
      const x1 = rect ? Math.min(p.width, rect.x1) : p.width;
      const y1 = rect ? Math.min(p.height, rect.y1) : p.height;
      if (x1 <= x0 || y1 <= y0) return;
      const image = new ImageData(x1 - x0, y1 - y0);
      const px = image.data;
      for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) {
        if (!p.pixels[y * p.width + x]) continue;
        const i = ((y - y0) * (x1 - x0) + x - x0) * 4;
        px[i] = 77; px[i + 1] = 204; px[i + 2] = 255; px[i + 3] = 230;
      }
      const g = p.canvas.getContext('2d');
      g.clearRect(x0, y0, x1 - x0, y1 - y0);
      g.putImageData(image, x0, y0);
    };

    const startPaint = (mode, fresh = false) => {
      if (S.paint) {
        if (S.paint.fresh === fresh) {
          S.paint.mode = mode; paintCursor(); ctx.invalidate(); app().renderInspector(); return true;
        }
        discardPaint(true);
      }
      let target = null, entry = null;
      if (!fresh) {
        const sel = selection();
        if (sel.length !== 1) {
          ctx.toast('Select one mask', 'Click the mask in this frame, then choose Brush or Eraser.', 'warn');
          return false;
        }
        target = sel[0]; entry = activeEntry(target);
        if (!entry) {
          ctx.toast('Mask is not in this frame', 'Select a mask visible on the current frame.', 'warn');
          return false;
        }
      }
      const st = target ? streamOf(target.stream) : null;
      const init = (stream, frame, payload = null) => {
        const pixels = new Uint8Array(stream.width * stream.height);
        if (payload) {
          const [l, t, w, h] = payload.box;
          const alpha = decodeRLE(payload.rle, w, h);
          for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
            const xx = l + x, yy = t + y;
            if (alpha[y * w + x] && xx >= 0 && yy >= 0 && xx < stream.width && yy < stream.height)
              pixels[yy * stream.width + xx] = 1;
          }
        }
        const canvas = document.createElement('canvas');
        canvas.width = stream.width; canvas.height = stream.height;
        S.paint = { mode, fresh, target, stream: stream.key, frame, width: stream.width,
                    height: stream.height, pixels, canvas, dirty: false, last: null };
        refreshPaint(S.paint);
        paintCursor(); ctx.display.changed();
        ctx.invalidate(); app().renderInspector();
      };
      if (target) init(st, streamFrame(target), entry.payload);
      else { S.paint = { mode, fresh: true, target: null, pending: true }; paintCursor(); }
      return true;
    };

    const discardPaint = (quiet = false) => {
      if (!S.paint) return;
      S.paint = null; S.pointer = null;
      paintCursor(); ctx.display.changed(); ctx.invalidate(); app().renderInspector();
      if (!quiet) ctx.toast('Brush changes discarded', 'Nothing was written.');
    };

    const pointInStream = (world, cell, p) => {
      if (!cell || cell.stream !== p.stream) return null;
      const x = Math.floor((world.x - cell.x) * p.width / cell.w);
      const y = Math.floor((world.y - cell.y) * p.height / cell.h);
      return x >= 0 && y >= 0 && x < p.width && y < p.height ? { x, y } : null;
    };
    const stamp = (p, point) => {
      const radius = Math.max(0.5, S.brush / 2);
      const x0 = Math.max(0, Math.floor(point.x - radius));
      const y0 = Math.max(0, Math.floor(point.y - radius));
      const x1 = Math.min(p.width, Math.ceil(point.x + radius + 1));
      const y1 = Math.min(p.height, Math.ceil(point.y + radius + 1));
      let changed = false;
      for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) {
        const dx = x - point.x, dy = y - point.y;
        if (dx * dx + dy * dy > radius * radius) continue;
        const i = y * p.width + x, next = p.mode === 'erase' ? 0 : 1;
        if (p.pixels[i] !== next) { p.pixels[i] = next; changed = true; }
      }
      if (changed) { p.dirty = true; refreshPaint(p, { x0, y0, x1, y1 }); }
    };
    const paintTo = (world, cell) => {
      let p = S.paint;
      if (!p) return;
      if (p.pending) {
        if (!cell) return;
        const stream = streamOf(cell.stream);
        const canvas = document.createElement('canvas');
        canvas.width = stream.width; canvas.height = stream.height;
        p = S.paint = { mode: p.mode, fresh: true, target: null, stream: cell.stream,
                        frame: streamFrame({ stream: cell.stream, layer_id: null }), width: stream.width,
                        height: stream.height, pixels: new Uint8Array(stream.width * stream.height),
                        canvas, dirty: false, last: null };
        paintCursor();
      }
      const now = pointInStream(world, cell, p);
      if (!now) return;
      const last = p.last || now;
      const steps = Math.max(1, Math.ceil(Math.hypot(now.x - last.x, now.y - last.y) / 0.5));
      for (let i = 0; i <= steps; i++) stamp(p, {
        x: Math.round(last.x + (now.x - last.x) * i / steps),
        y: Math.round(last.y + (now.y - last.y) * i / steps),
      });
      p.last = now; S.pointer = { world, cell };
      ctx.invalidate();
    };

    const payloadFromPaint = (p) => {
      let x0 = p.width, y0 = p.height, x1 = -1, y1 = -1;
      for (let y = 0; y < p.height; y++) for (let x = 0; x < p.width; x++) if (p.pixels[y * p.width + x]) {
        x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y);
      }
      if (x1 < x0) return null;
      const w = x1 - x0 + 1, h = y1 - y0 + 1, runs = [];
      let on = false, count = 0;
      for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) {
        const next = !!p.pixels[y * p.width + x];
        if (next !== on) { runs.push(count); count = 0; on = next; }
        count++;
      }
      runs.push(count);
      return { box: [x0, y0, w, h], rle: runs.join(',') };
    };

    const savePaint = async () => {
      const p = S.paint;
      if (!p) return;
      if (!p.dirty) return discardPaint(true);
      const payload = payloadFromPaint(p);
      if (!payload && p.fresh) {
        ctx.toast('Nothing painted', 'Paint at least one pixel before creating a mask.', 'warn'); return;
      }
      const cap = ctx.store.get('capture');
      const empty = { box: [0, 0, 1, 1], rle: '1' };
      const ref = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      ctx.emit('masks.local-ref', ref); S.busy = true;
      try {
        let key = p.target?.key;
        let kind, body;
        if (p.fresh) {
          ({ key } = await ctx.call(`/${cap.id}/newkey?stream=${encodeURIComponent(p.stream)}`, { quiet: true }));
          kind = 'create';
          body = { stream: p.stream, key, label: S.label || 'mask', frames: [
            { frame: p.frame, outside: 0, payload },
            { frame: p.frame + 1, outside: 1, payload },
          ] };
        } else {
          kind = 'keyframe';
          body = { stream: p.stream, key, frame: p.frame,
                   payload: { ...(payload || empty), outside: payload ? 0 : 1 } };
        }
        const r = await ctx.api.plugin('masks', `/${cap.id}/op`, {
          method: 'POST', body: { kind, payload: body, client_ref: ref }, quiet: true });
        S.localOps.add(r.op.id); Object.assign(S, r.state); ctx.display.changed();
        app().bus.emit('op', { ...r.op, _source: 'local' });
        discardPaint(true);
        await ctx.reloadLayer(r.materialised?.layer_id);
        const made = app().findObject(p.stream, key);
        if (made) { app().select([made.id], 'set'); app().pulse([made.id], { ms: 1200 }); }
        ctx.toast(p.fresh ? 'Mask created' : 'Mask saved', `${p.stream}/${key} at frame ${p.frame}. Ctrl+Z takes it back.`);
      } catch (e) {
        ctx.emit('masks.cancel-local-ref', ref);
        ctx.toast('Could not save brush changes', e.message || String(e), e.status === 409 ? 'warn' : 'err');
      } finally { S.busy = false; app().renderInspector(); }
    };

    ctx.registerFilter({ id: 'paint-preview', title: 'brush preview', structural: true,
      test: (entry) => !!S.paint?.target && entry.object.id === S.paint.target.id });
    ctx.setPointerHandler('edit', ({ phase, world, cell }) => {
      if (!S.paint || S.busy) return false;
      if (phase === 'down') { paintTo(world, cell); return true; }
      if (phase === 'move') paintTo(world, cell);
      if (phase === 'up' || phase === 'cancel') { S.pointer = null; S.paint.last = null; ctx.invalidate(); }
      return true;
    });
    ctx.store.on(['frame'], () => {
      if (!S.paint) return;
      discardPaint(true);
      ctx.toast('Brush preview discarded', 'It belonged to the frame you left.', 'warn');
    });
    ctx.on('tool', (tool) => {
      if (S.paint && tool?.id !== 'edit') discardPaint(true);
    });

    // ------------------------------------------------------------- state
    const load = async () => {
      const cap = ctx.store.get('capture');
      if (!cap) return;
      Object.assign(S, await ctx.call(`/${cap.id}/state`), { loaded: true });
      S.showDeleted = showingDeleted();
      ctx.display.changed();      // the filters above now have state to test against
      ctx.emit('masks.visibility', S.showDeleted);
      app().renderInspector();
      ctx.invalidate();
    };
    ctx.on('capture', () => { discardPaint(true); load(); });
    if (ctx.store.get('capture')) load();

    // Another person's edit, or an undo, arrives as an op. Rebuild what it
    // touched rather than guessing — the projection is cheap and the alternative
    // is two clients disagreeing about which tracks exist.
    ctx.onOp(async (op) => {
      if (!op.kind?.startsWith('masks.')) return;
      const cap = ctx.store.get('capture');
      if (!cap || S.busy) return;
      const verb = op.kind.split('.')[1];
      // A structural edit can originate from the Identity workspace too.  A
      // local delivery has already been materialised by `/masks/.../op`; keep
      // its id until the WebSocket echo arrives so that echo does not rebuild
      // the layer a second time.
      if (op._source === 'remote' && (S.localOps.has(op.id) || S.localRefs.has(op.client_ref))) {
        S.localOps.delete(op.id);
        S.localRefs.delete(op.client_ref);
        return;
      }
      const mine = (op._source === 'local' || S.localOps.has(op.id)) && !op.undone;
      if (op._source === 'local') S.localOps.add(op.id);
      S.busy = true;
      try {
        if (!mine && NEEDS_REBUILD.has(verb)) await ctx.call(`/${cap.id}/materialise`, { method: 'POST' });
        await load();
        if (NEEDS_REBUILD.has(verb)) await ctx.reloadLayers();
        else ctx.invalidate();
      } finally { S.busy = false; }
    });

    // ------------------------------------------------------- what is on screen
    // These used to be a styler returning `hidden: true`, which meant every
    // feature had to remember to ask. They are filters now, registered with
    // the display authority, so the core enforces them at all four doors —
    // drawing, hit testing, selection and navigation.
    //
    // `structural` says which kind each is. A superseded or purged track is
    // not a preference: showing it would put two copies of the same track on
    // screen, so there is no toggle and there should not be one. "Deleted" is
    // a preference, and it is the same `H` toggle as before — it simply lives
    // where every other one lives now, and shows up in the Display panel.
    ctx.registerFilter({
      id: 'superseded', title: 'superseded copies', structural: true,
      description: 'The imported copy of a track that a cut or a join replaced.',
      test: (entry) => S.loaded && entry.object.layer_id === S.sourceLayer
        && setOf(S.shadow, entry.stream.key).has(entry.object.key),
    });
    ctx.registerFilter({
      id: 'purged', title: 'purged tracks', structural: true,
      description: 'Taken out of the working set entirely. The op log still has them.',
      test: (entry) => S.loaded && setOf(S.purged, entry.stream.key).has(entry.object.key),
    });
    ctx.registerFilter({
      id: 'deleted', title: 'deleted tracks', structural: false, on: false,
      description: 'Hidden by a human, kept in the log. H toggles this.',
      test: (entry) => S.loaded && setOf(S.deleted, entry.stream.key).has(entry.object.key),
    });

    // Colour is a separate question from visibility, and a deleted track that
    // IS shown should look plainly dead.
    ctx.registerStyler((entry, base) => {
      if (!S.loaded) return base;
      const s = entry.stream.key, k = entry.object.key;
      if (setOf(S.deleted, s).has(k)) {
        return { ...base, color: 'rgb(150,45,45)', alpha: 0.3, label: `${k} DEL` };
      }
      return base;
    });

    // -------------------------------------------------------- click policy
    ctx.setPickHandler('edit', ({ hit, stack, alt, event, right }) => {
      const A = app();
      if (right) { editMenu(stack || [], event); return true; }
      if (!hit) { A.clearSelection(); return true; }
      A.select([hit.object.id], event.shiftKey ? 'toggle' : 'set');
      if (alt) A.pulse([hit.object.id], { ms: 700, rings: 2 });
      return true;
    });

    const editMenu = (stack, event) => {
      const A = app();
      if (!stack.length) { A.clearSelection(); return; }
      const items = stack.map((e, i) => ({
        label: `${e.stream.key} · ${e.object.key}`,
        hint: stateOf(e.stream.key, e.object.key),
        color: A.keyColor(`${e.stream.key}/${e.object.key}`),
        on: i === 0,
        onhover: () => A.pulse([e.object.id], { ms: 600, rings: 1 }),
        onclick: () => { A.select([e.object.id], 'set'); A.pulse([e.object.id], { ms: 700, rings: 2 }); },
      }));
      const top = stack[0];
      items.push({ label: '— cut here', hint: 'T', onclick: () => { A.select([top.object.id], 'set'); split(); } });
      items.push({ label: '— delete this track', hint: 'D', onclick: () => { A.select([top.object.id], 'set'); del(); } });
      ctx.ui.menu(event.clientX, event.clientY, items,
                  { title: stack.length > 1 ? `${stack.length} masks here` : 'mask' });
    };

    const stateOf = (stream, key) =>
      setOf(S.purged, stream).has(key) ? 'purged'
        : setOf(S.deleted, stream).has(key) ? 'deleted'
          : (S.joins[stream] || []).some((g) => g.includes(key)) ? 'joined'
            : key.includes('+') ? 'join' : key.includes('@') ? 'segment' : '';

    // --------------------------------------------------------- operations
    const selection = () => {
      const A = app();
      const out = [];
      for (const id of ctx.store.get('selection')) {
        const o = A.objectOf(id);
        if (o) out.push(o);
      }
      return out;
    };
    const need = (what) => {
      if (S.paint) {
        ctx.toast('Finish the brush first', 'Save with Enter or discard with Esc before another track edit.', 'warn');
        return null;
      }
      const sel = selection();
      if (!sel.length) { ctx.toast('Nothing selected', `Click a mask to ${what}.`, 'warn'); return null; }
      const streams = new Set(sel.map((o) => o.stream));
      if (streams.size > 1) {
        ctx.toast('One camera at a time', `${what} works within a camera; you have ${streams.size}.`, 'warn');
        return null;
      }
      return sel;
    };

    const edit = async (kind, payload) => {
      const cap = ctx.store.get('capture');
      const keys = (payload.keys || (payload.key != null ? [payload.key] : [])).map(String);
      // Delete/restore affect only the display policy, not mask geometry. Paint
      // that result immediately; the durable operation still follows and a
      // failed request is reconciled from the server below.
      if (kind === 'delete' || kind === 'restore') {
        const deleted = new Set(S.deleted[payload.stream] || []);
        for (const key of keys) {
          if (kind === 'delete') deleted.add(key); else deleted.delete(key);
        }
        S.deleted = { ...S.deleted, [payload.stream]: [...deleted] };
        ctx.display.changed();
        app().renderInspector();
        ctx.invalidate();
      }
      S.busy = true;
      try {
        const r = await ctx.call(`/${cap.id}/op`,
                                 { method: 'POST', body: { kind, payload }, quiet: true });
        S.localOps.add(r.op.id);
        Object.assign(S, r.state);
        // `visibleId` is memoised by the display authority. A delete, restore,
        // split or join changes exactly the predicates it caches; leaving the
        // old answers alive made Identity occasionally walk to a valid problem
        // and then find no drawable object until a page refresh cleared them.
        ctx.display.changed();
        app().bus?.emit?.('op', { ...r.op, _source: 'local' });
        if (NEEDS_REBUILD.has(kind.replace(/^masks\./, ''))) {
          await ctx.reloadLayer(r.materialised?.layer_id);
        }
        else { app().renderInspector(); ctx.invalidate(); }
        return r;
      } catch (e) {
        // A 409 is the server refusing a structural edit *and saying why* —
        // "3 is joined to 1+2+3, unjoin it first". Identity ops commute and
        // these do not, so the reason is the whole point; swallowing it leaves
        // the user pressing a key that silently does nothing.
        if (e.status === 409) ctx.toast('That edit cannot apply', e.message, 'warn');
        else ctx.toast('Edit failed', e.message || String(e), 'err');
        // An optimistic delete/restore is not durable after a failed request.
        // Re-read the projection so a red `DEL` label never lies.
        if (kind === 'delete' || kind === 'restore') await load();
        return null;
      } finally { S.busy = false; }
    };

    const del = async () => {
      const sel = need('delete'); if (!sel) return;
      const stream = sel[0].stream;
      const keys = sel.map((o) => o.key);
      const saved = edit('delete', { stream, keys });
      ctx.toast('Deleted', `${keys.map((k) => `${stream}/${k}`).join(', ')} marked DEL — saving now.`);
      await saved;
    };
    const restore = async () => {
      const sel = need('restore'); if (!sel) return;
      await edit('restore', { stream: sel[0].stream, keys: sel.map((o) => o.key) });
    };
    const purge = async () => {
      const sel = need('purge'); if (!sel) return;
      const ok = await ctx.ui.confirm(`Purge ${sel.length} track(s)?`,
        'A purged track leaves the working set entirely — it stops being drawn, stops being ' +
        'hit-tested, and stops being exported. The op log still has it, so this is reversible, ' +
        'but nothing on screen will remind you it existed.', 'Purge');
      if (!ok) return;
      await edit('purge', { stream: sel[0].stream, keys: sel.map((o) => o.key) });
    };

    const split = async () => {
      const sel = need('cut'); if (!sel) return;
      if (sel.length > 1) return ctx.toast('One track', 'A cut applies to one track.', 'warn');
      const o = sel[0];
      const A = app();
      const data = A.layerData((l) => l.id === o.layer_id);
      const f = data ? data.mapFrame(o.stream, ctx.store.get('frame')) : ctx.store.get('frame');
      if (f <= o.first_frame || f > o.last_frame) {
        return ctx.toast('Nothing to cut here',
          `${o.key} runs ${o.first_frame}–${o.last_frame} in ${o.stream}; you are at ${f}.`, 'warn');
      }
      const r = await edit('split', { stream: o.stream, key: o.key, frame: f });
      if (r) {
        ctx.toast('Cut', `${o.key} → ${o.key} and ${o.key}@${f}`);
        const made = A.findObject(o.stream, `${o.key}@${f}`);
        if (made) { A.select([made.id], 'set'); A.pulse([made.id], { ms: 1400 }); }
      }
    };

    const join = async () => {
      const sel = need('join'); if (!sel) return;
      if (sel.length < 2) return ctx.toast('Pick two', 'Shift+click a second track to join it.', 'warn');
      const keys = sel.map((o) => o.key);
      const r = await edit('join', { stream: sel[0].stream, keys });
      if (r) {
        const key = [...keys].sort().join('+');
        ctx.toast('Joined', `${keys.join(' + ')} now travel together as one track.`);
        const made = app().findObject(sel[0].stream, key);
        if (made) { app().select([made.id], 'set'); app().pulse([made.id], { ms: 1400 }); }
      }
    };

    const unjoin = async () => {
      const sel = need('unjoin'); if (!sel) return;
      const o = sel[0];
      const group = (S.joins[o.stream] || []).find((g) => g.includes(o.key)) ||
        (o.key.includes('+') ? o.key.split('+') : null);
      if (!group) return ctx.toast('Not joined', `${o.key} is not part of a join.`, 'warn');
      await edit('unjoin', { stream: o.stream, key: o.key, keys: group });
      ctx.toast('Unjoined', `${group.join(' + ')} are separate tracks again.`);
    };

    // One state, in one place. `H`, the chip in this panel and the row in the
    // Display panel are three views of the same registered filter — before,
    // they would have been three flags that could disagree.
    const showingDeleted = () => !ctx.display.filters.get('masks.deleted')?.on;
    const toggleDeleted = () => {
      const show = !showingDeleted();
      ctx.display.setFilter('masks.deleted', !show);
      S.showDeleted = show;
      ctx.emit('masks.visibility', show);            // others cache what is on screen
      app().renderInspector();
      ctx.toast('Deleted tracks', show ? 'shown, tagged DEL' : 'hidden');
    };

    // The active bitmap is rendered above the normal layers.  Its target is
    // filtered while painting, which is what makes erased pixels visibly gone
    // instead of revealing the old mask below the preview.
    ctx.on('drawn', ({ g, view }) => {
      const p = S.paint;
      if (ctx.store.get('tool') !== 'edit' || !p || p.pending) return;
      const cell = cellOf(p.stream);
      if (!cell) return;
      g.save();
      g.imageSmoothingEnabled = false;
      g.globalAlpha = 0.78;
      g.drawImage(p.canvas, cell.x, cell.y, cell.w, cell.h);
      g.globalAlpha = 1;
      g.strokeStyle = p.mode === 'erase' ? '#E0645C' : '#4DCCFF';
      g.lineWidth = 1.5 / view.scale;
      g.strokeRect(cell.x, cell.y, cell.w, cell.h);
      if (S.pointer?.cell?.stream === p.stream) {
        const q = pointInStream(S.pointer.world, cell, p);
        if (q) {
          const x = cell.x + q.x * cell.w / p.width, y = cell.y + q.y * cell.h / p.height;
          const rx = Math.max(0.5, S.brush / 2) * cell.w / p.width;
          const ry = Math.max(0.5, S.brush / 2) * cell.h / p.height;
          g.setLineDash([3 / view.scale, 2 / view.scale]);
          g.beginPath(); g.ellipse(x, y, rx, ry, 0, 0, Math.PI * 2); g.stroke();
          g.setLineDash([]);
        }
      }
      g.restore();
    });

    for (const [id, title, keys, run] of [
      ['delete', 'Delete the selected tracks', ['D', 'Del'], del],
      ['restore', 'Restore deleted tracks', ['Shift+D', 'Shift+Del'], restore],
      ['purge', 'Purge the selected tracks', ['P'], purge],
      ['split', 'Cut the selected track at this frame', ['T'], split],
      ['join', 'Join the selected tracks into one', ['J'], join],
      ['unjoin', 'Take a joined track apart', ['U'], unjoin],
      ['showdeleted', 'Show / hide deleted tracks', ['H'], toggleDeleted],
      // When a draft is already open, B/E are mode switches for that exact
      // bitmap — including a not-yet-created mask. They must never throw the
      // draft away and fall back to selecting an older mask.
      ['brush', 'Paint mask pixels with the selected mask', ['B'], () =>
        startPaint('paint', S.paint?.fresh || false)],
      ['eraser', 'Erase mask pixels with the selected mask', ['E'], () =>
        startPaint('erase', S.paint?.fresh || false)],
      ['newmask', 'Create a new mask with the brush', ['N'], () => startPaint('paint', true)],
      ['brush-smaller', 'Make the mask brush thinner', ['['], () => {
        S.brush = Math.max(1, S.brush - 1); ctx.invalidate(); app().renderInspector();
      }],
      ['brush-larger', 'Make the mask brush thicker', [']'], () => {
        S.brush = Math.min(256, S.brush + 1); ctx.invalidate(); app().renderInspector();
      }],
      ['save-brush', 'Save the painted mask', ['Enter'], savePaint],
      ['discard-brush', 'Discard the painted mask changes', ['Esc'], () => discardPaint()],
      ['protect-brush', 'Discard active brush preview before undoing history', ['Ctrl+Z'], () => {
        if (S.paint) discardPaint(); else app().undo();
      }],
    ]) ctx.registerCommand({ id, title, keys, tool: 'edit', group: 'Edit', run });

    // ------------------------------------------------------------- lanes
    ctx.registerLaneDecorator(({ g, stream, y, h, x0, w, cap, data }) => {
      if (ctx.store.get('tool') !== 'edit' || !data) return;
      const cuts = S.splits[stream.key] || {};
      g.fillStyle = 'var(--ochre)';
      g.fillStyle = '#DCA84A';
      for (const frames of Object.values(cuts)) {
        for (const f of frames) {
          const at = data.unmapFrame(stream.key, f) / cap.n_frames;
          g.fillRect(x0 + at * w - 0.5, y, 1.5, h);
        }
      }
    });

    // --------------------------------------------------------- inspector
    ctx.registerInspector('edit', (A) => {
      const h = ctx.h;
      const count = (m) => Object.values(m || {}).reduce((n, v) => n + v.length, 0);
      const nSplits = Object.values(S.splits || {})
        .reduce((n, per) => n + Object.values(per).reduce((m, f) => m + f.length, 0), 0);
      const nJoins = Object.values(S.joins || {}).reduce((n, g) => n + g.length, 0);
      const sel = selection();
      const one = sel.length === 1 ? sel[0] : null;

      const btn = (label, key, fn, cls = 'btn sm', guide = '') =>
        h('button', { class: cls, dataset: guide ? { guide } : {}, onclick: fn, title: key }, label, h('kbd', {}, key));

      const paint = S.paint;
      const brushInput = h('input', {
        class: 'mask-brush-size', type: 'range', min: 1, max: 256,
        oninput: (e) => { S.brush = Number(e.target.value); ctx.invalidate(); app().renderInspector(); },
      });
      brushInput.value = S.brush;

      return h('div', {},
        ctx.ui.panel('Pixel brush', [
          h('div', { class: 'hint' }, paint
            ? (paint.pending ? 'New mask: click and drag inside a camera to begin.'
              : `${paint.mode === 'erase' ? 'Erasing' : 'Painting'} ${paint.stream} at frame ${paint.frame}. ` +
                'The preview is in native image pixels; save creates one undoable edit.')
            : 'Select one visible mask, then paint or erase it. New mask starts from an empty frame.'),
          h('div', { class: 'row wrap' },
            btn('Brush', 'B', () => startPaint('paint', paint?.fresh || false), `btn sm${paint?.mode === 'paint' ? ' primary' : ''}`),
            btn('Eraser', 'E', () => startPaint('erase', paint?.fresh || false), `btn sm${paint?.mode === 'erase' ? ' danger' : ''}`),
            btn('New mask', 'N', () => startPaint('paint', true), 'btn sm')),
          h('div', { class: 'field' },
            h('label', {}, `Thickness · ${S.brush} px`),
            h('div', { class: 'row' }, brushInput,
              btn('−', '[', () => { S.brush = Math.max(1, S.brush - 1); ctx.invalidate(); app().renderInspector(); }, 'btn sm'),
              btn('+', ']', () => { S.brush = Math.min(256, S.brush + 1); ctx.invalidate(); app().renderInspector(); }, 'btn sm'))),
          paint?.fresh ? h('div', { class: 'field' }, h('label', {}, 'New mask label'),
            (() => { const input = h('input', { class: 'txt', oninput: (e) => { S.label = e.target.value; } }); input.value = S.label; return input; })()) : null,
          paint ? h('div', { class: 'row wrap' },
            btn('Save mask', 'Enter', savePaint, 'btn sm primary'),
            btn('Discard', 'Esc', () => discardPaint(), 'btn sm')) : null,
          h('div', { class: 'hint' }, 'B brush · E eraser · N new mask · [ / ] thickness · Enter save · Esc discard. Alt-drag and middle-drag still pan.'),
        ], { id: 'pixel-brush' }),
        ctx.ui.panel('Edit', [
          h('div', {},
            ctx.ui.stat('deleted', String(count(S.deleted))),
            ctx.ui.stat('purged', String(count(S.purged))),
            ctx.ui.stat('cuts', String(nSplits)),
            ctx.ui.stat('joins', String(nJoins))),
          one ? h('div', { class: 'hint' },
            `${one.stream} · track ${one.key} · frames ${one.first_frame}–${one.last_frame}` +
            (stateOf(one.stream, one.key) ? ` · ${stateOf(one.stream, one.key)}` : ''))
            : h('div', { class: 'hint' }, sel.length ? `${sel.length} tracks selected`
              : 'Click a mask. Editing selects one track at a time.'),
          h('div', { class: 'row wrap' },
            btn('Delete', 'D', del, 'btn sm danger', 'edit-delete'),
            btn('Restore', '⇧D', restore, 'btn sm', 'edit-restore'),
            btn('Purge', 'P', purge, 'btn sm danger')),
          h('div', { class: 'row wrap' },
            btn('Cut here', 'T', split, 'btn sm primary'),
            btn('Join', 'J', join, 'btn sm', 'edit-join'),
            btn('Unjoin', 'U', unjoin, 'btn sm', 'edit-unjoin')),
          h('div', { class: 'row' },
            h('button', { class: `chipbtn${showingDeleted() ? ' on' : ''}`, onclick: toggleDeleted },
              'show deleted'),
            h('span', { class: 'grow' }),
            h('button', { class: 'btn sm', onclick: () => A.undo('masks.') }, 'Undo edit',
              h('kbd', {}, '⌃Z'))),
          h('div', { class: 'hint' },
            'A cut lands on the frame you are on. Everything here is an op — Ctrl+Z, and the ' +
            'History panel, take any of it back.'),
        ], { id: 'edit' }),

        ctx.ui.panel('Replay', [
          h('div', { class: 'hint' },
            'The desktop tool kept its deletions, cuts and joins in the same file as the ' +
            'identities. Replaying them here makes identity keys like 86@2470 and 1+2+3 ' +
            'point at real tracks.'),
          h('button', {
            class: 'btn sm', onclick: async () => {
              const { job } = await ctx.job('import:consistent_ids',
                { capture_id: ctx.store.get('capture').id });
              A.watchJob(job, async (j) => {
                ctx.toast('Replayed', j.message || '');
                await load(); await ctx.reloadLayers();
              });
            },
          }, 'Replay edits from consistent_ids.json'),
          h('button', {
            class: 'btn sm', onclick: async () => {
              const { job } = await ctx.job('orphans', { capture_id: ctx.store.get('capture').id });
              A.watchJob(job, (j) => ctx.toast('Dangling identity keys', j.message || ''));
            },
          }, 'Count dangling identity keys'),
        ], { id: 'edit-replay', collapsed: true }));
    });
  },
};
