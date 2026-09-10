// The identity workspace. Colour by identity, hover to see the whole group
// across cameras, link with M — the same gestures the desktop tool trained you
// on, minus the part where only one person could do it at a time.
//
// Two things the desktop tool got wrong, fixed here:
//   · clicking always took the whole identity, so deleting "a track" could
//     delete it in six cameras. Ctrl+click takes exactly one, a scope switch
//     changes the default, and every destructive button names its scope.
//   · a mask underneath another was nearly unreachable. Alt+click walks down
//     the stack; right-click lists everything under the cursor.

const OUTSIDE = 200;

export default {
  id: 'identity',

  activate(ctx) {
    const app = () => window.quorum;
    const S = { assignments: {}, cids: [], nextCid: 1, counts: {}, loaded: false,
                scope: localStorage.getItem('quorum.identity.scope') || 'identity',
                localOps: new Set() };

    const cidOf = (entry) => {
      const m = S.assignments[entry.stream.key];
      const v = m && m[entry.object.key];
      return v === undefined ? null : v;
    };
    const cidColor = (cid) => cid === OUTSIDE ? 'hsl(0 0% 62%)'
      : `hsl(${((cid * 137.508) % 360).toFixed(1)} 72% 58%)`;

    // ------------------------------------------------------------- state
    const maskLayer = () => (ctx.store.get('capture')?.layers || []).find((l) => l.type === 'mask.rle');

    let pendingWrites = 0;
    let loadTimer = null;
    // A direction/filter-specific batch of nearby problem runs.  Navigation
    // never asks the server to count every problem in the capture.
    const problemCache = new Map();

    const recount = () => {
      const cids = [...new Set(Object.values(S.assignments)
        .flatMap((m) => Object.values(m))
        .filter((c) => c !== OUTSIDE))].sort((a, b) => a - b);
      S.cids = cids;
      S.nextCid = cids.length ? Math.max(...cids) + 1 : 1;
      S.counts = {};
      for (const m of Object.values(S.assignments)) {
        for (const c of Object.values(m)) S.counts[c] = (S.counts[c] || 0) + 1;
      }
    };

    const updateOutsideProblems = (items) => {
      const ids = new Set(items.map(([stream, key]) => app().findObject(stream, key)?.id)
        .filter((id) => id != null));
      if (!ids.size) return;
      for (const batch of problemCache.values()) {
        batch.problems = batch.problems.flatMap((p) => {
          const objects = (p.objects || []).filter((id) => !ids.has(id));
          if (p.kind === 'duplicate') {
            if (objects.length < 2) return [];
            return [{ ...p, objects,
              why: `id ${p.cid} is on ${objects.length} masks in ${p.stream}` }];
          }
          return objects.length ? [{ ...p, objects }] : [];
        });
      }
      app().renderInspector();
    };

    // Identity folding is deliberately small and deterministic.  Keep the
    // visible assignment map responsive while the durable op is travelling to
    // the server; the next remote edit or an explicit reload reconciles it.
    const applyLocal = (kind, payload) => {
      const items = (payload?.items || []).map(([s, k]) => [String(s), String(k)]);
      const outside = kind === 'identity.outside'
        || (kind === 'identity.assign' && Number(payload?.cid) === OUTSIDE);
      if (outside) updateOutsideProblems(items);
      else problemCache.clear();
      if (kind === 'identity.clear') {
        for (const [s, k] of items) delete (S.assignments[s] || {})[k];
      } else if (kind === 'identity.assign') {
        const cid = Number(payload.cid);
        for (const [s, k] of items) (S.assignments[s] ||= {})[k] = cid;
      } else if (kind === 'identity.outside') {
        for (const [s, k] of items) (S.assignments[s] ||= {})[k] = OUTSIDE;
      } else if (kind === 'identity.link') {
        const now = items.map(([s, k]) => S.assignments[s]?.[k]);
        const have = [...new Set(now.filter((c) => c != null && c !== OUTSIDE))]
          .sort((a, b) => a - b);
        const cid = have.length ? have[0]
          : (now.length && now.every((c) => c === OUTSIDE) ? OUTSIDE : S.nextCid);
        const merge = new Set(have);
        for (const m of Object.values(S.assignments)) {
          for (const k of Object.keys(m)) if (merge.has(m[k])) m[k] = cid;
        }
        for (const [s, k] of items) (S.assignments[s] ||= {})[k] = cid;
      }
      S.assignments = Object.fromEntries(
        Object.entries(S.assignments).filter(([, m]) => Object.keys(m).length));
      recount();
      app().renderInspector();
      ctx.invalidate();
    };

    const load = async () => {
      if (pendingWrites) return scheduleLoad(250);
      const cap = ctx.store.get('capture');
      if (!cap) return;
      const st = await ctx.call(`/${cap.id}/state`);
      Object.assign(S, st, { loaded: true });
      problemCache.clear();
      app().renderInspector();
      ctx.invalidate();
    };

    const scheduleLoad = (delay = 150) => {
      clearTimeout(loadTimer);
      loadTimer = setTimeout(() => { loadTimer = null; load(); }, delay);
    };

    const submit = (kind, payload) => {
      pendingWrites += 1;
      applyLocal(kind, payload);
      return app().postOp(kind, payload, { source: 'local' })
        .then((op) => {
          S.localOps.add(op.id);
          setTimeout(() => S.localOps.delete(op.id), 30000);
          const outside = op.kind === 'identity.outside'
            || (op.kind === 'identity.assign' && Number(op.payload?.cid) === OUTSIDE);
          if (outside) app().renderInspector();
          // `identity.link` allocates on the server from every identity that
          // has ever existed, including one later cleared.  The compact state
          // endpoint intentionally does not carry that historical set, so a
          // brand-new local link is reconciled after its instant optimistic
          // paint.  This is debounced and never holds up the edit itself.
          if (op.kind === 'identity.link') scheduleLoad(500);
          return op;
        })
        .catch(() => {
          // The optimistic value is not durable if the write failed.  Re-read
          // the authoritative state; the normal API error handler reports why.
          load();
          return null;
        })
        .finally(() => { pendingWrites = Math.max(0, pendingWrites - 1); });
    };
    ctx.on('capture', load);
    ctx.onOp((op) => {
      if (!op.kind?.startsWith('identity.') && !op.kind?.startsWith('masks.')) return;
      if (op._source === 'local' && op.kind.startsWith('identity.')) return;
      if (op._source === 'remote' && S.localOps.has(op.id)) {
        S.localOps.delete(op.id);  // the WebSocket echo of our own confirmed op
        return;
      }
      problemCache.clear();
      scheduleLoad();
    });
    if (ctx.store.get('capture')) load();

    // ------------------------------------------------------------- colour
    // A colour *mode*, not a side effect of this tab being open. Before, these
    // colours existed only while the Identity workspace was active and could
    // not be asked for anywhere else or refused while it was — which is the
    // same "whoever draws last decides" mistake as hiding.
    ctx.registerColorMode({
      id: 'identity', title: 'by identity', order: 5,
      colorOf: (entry) => {
        const cid = cidOf(entry);
        return cid == null ? 'hsl(200 8% 72%)' : cidColor(cid);
      },
      labelOf: (entry) => {
        const cid = cidOf(entry);
        return cid == null ? `${entry.object.key} ·`
          : cid === OUTSIDE ? `${entry.object.key} OUT` : `${entry.object.key} → ${cid}`;
      },
    });

    // Emphasis is a different job from colour: it says "this is the group you
    // are pointing at", and it only makes sense while this tool is open.
    ctx.registerStyler((entry, base) => {
      if (ctx.store.get('tool') !== 'identity') return base;
      const cid = cidOf(entry);
      if (cid == null) return { ...base, alpha: 0.22 };
      const hoverCid = hovered();
      const grouped = hoverCid != null && cid === hoverCid;
      return { ...base, alpha: grouped ? 0.8 : cid === OUTSIDE ? 0.2 : 0.5 };
    });

    // Opening this workspace suggests its colouring; a mode the user chose on
    // purpose always wins.
    ctx.on('tool', (t) => { if (t?.id === 'identity') ctx.display.suggestMode('identity'); });

    let hoverEntry = null;
    const hovered = () => (hoverEntry ? cidOf(hoverEntry) : null);
    ctx.on('hover', (hit) => { hoverEntry = hit; ctx.invalidate(); });

    // ------------------------------------------------------- click policy
    // Plain click follows the scope switch (whole identity by default, which is
    // what you almost always mean); Ctrl always means "just this one".
    ctx.setPickHandler('identity', ({ hit, stack, alt, event, right }) => {
      const A = app();
      if (right) { stackMenu(stack || [], event); return true; }
      if (!hit) { A.clearSelection(); return true; }
      const single = alt || event.ctrlKey || event.metaKey || S.scope === 'track';
      const cid = cidOf(hit);
      // an identity whose every other member has been superseded still selects
      // the track you clicked, rather than nothing at all
      const group = cid == null ? [] : membersOf(cid);
      const ids = single || !group.length ? [hit.object.id] : group;
      A.select(ids, event.shiftKey ? 'add' : 'set');
      if (alt) {
        A.pulse([hit.object.id], { ms: 700, rings: 2 });
        ctx.toast('Under the cursor', `${hit.stream.key} track ${hit.object.key}` +
          (stack.length > 1 ? ` — ${stack.indexOf(hit) + 1} of ${stack.length}` : ''));
      }
      return true;
    });

    // Everything under the cursor, with what you can do to it. This replaces
    // the old right-click-clears-the-identity, which was destructive, silent,
    // and impossible to aim at the mask you actually meant.
    const stackMenu = (stack, event) => {
      const A = app();
      if (!stack.length) { A.clearSelection(); return; }
      const items = stack.map((e, i) => {
        const cid = cidOf(e);
        return {
          label: `${e.stream.key} · track ${e.object.key}`,
          hint: cid == null ? 'no id' : cid === OUTSIDE ? 'OUT' : `id ${cid}`,
          color: cid == null ? 'hsl(200 8% 72%)' : cidColor(cid),
          on: i === 0,
          onhover: () => A.pulse([e.object.id], { ms: 600, rings: 1 }),
          onclick: () => { A.select([e.object.id], 'set'); A.pulse([e.object.id], { ms: 700, rings: 2 }); },
        };
      });
      const top = stack[0];
      const topCid = cidOf(top);
      items.push({ label: '— select whole identity', hint: topCid == null ? '—' : `id ${topCid}`,
                   onclick: () => topCid != null && A.select(membersOf(topCid), 'set') });
      items.push({ label: '— clear identity from this track', hint: 'C',
                   onclick: () => submit('identity.clear',
                     { items: [[top.stream.key, top.object.key]] }) });
      ctx.ui.menu(event.clientX, event.clientY, items,
                  { title: stack.length > 1 ? `${stack.length} masks here` : 'mask' });
    };

    // An identity's tracks may live in either mask layer: an untouched track is
    // still the imported one, while a cut or joined track has been replaced by
    // a derived copy. Looking in only one layer is how `1+2+3` became
    // unselectable — the key exists, the object was just somewhere else.
    //
    // The assignment map also outlives the tracks it names. Joining 1, 2 and 3
    // leaves the entries for `1`, `2` and `3` beside the new one for `1+2+3`,
    // all on the same identity, so a naive lookup hands back the superseded
    // originals as well and you end up selecting things nobody can see. Only
    // tracks that are actually on screen count as members.
    let index = null;
    const memberIndex = () => {
      if (index) return index;
      index = new Map();
      for (const o of app().visibleObjects((l) => l.type === 'mask.rle')) {
        index.set(`${o.stream}/${o.key}`, o.id);
      }
      return index;
    };
    const forgetIndex = () => { index = null; };
    ctx.on('data', forgetIndex);
    ctx.on('capture', forgetIndex);
    ctx.on('masks.visibility', forgetIndex);
    ctx.on('display', forgetIndex);   // hiding a class changes who is on screen
    ctx.onOp(forgetIndex);

    const membersOf = (cid) => {
      const idx = memberIndex();
      const out = [];
      for (const [stream, m] of Object.entries(S.assignments)) {
        for (const [key, c] of Object.entries(m)) {
          if (c !== cid) continue;
          const id = idx.get(`${stream}/${key}`);
          if (id !== undefined) out.push(id);
        }
      }
      return out;
    };

    const selectedItems = () => {
      const A = app();
      const out = [];
      for (const id of ctx.store.get('selection')) {
        const o = A.objectOf(id);
        if (o) out.push([o.stream, o.key]);
      }
      return out;
    };

    // How wide is the blast radius of the next destructive key? The desktop
    // tool never said, and that is exactly how you lose six cameras of work.
    const scopeOf = (items) => {
      const cids = new Set();
      for (const [s, k] of items) {
        const c = (S.assignments[s] || {})[k];
        if (c != null && c !== OUTSIDE) cids.add(c);
      }
      const streams = new Set(items.map((i) => i[0]));
      return { tracks: items.length, cids: [...cids], streams: [...streams] };
    };
    const scopeText = (items) => {
      const sc = scopeOf(items);
      const bits = [`${sc.tracks} track${sc.tracks === 1 ? '' : 's'}`];
      if (sc.streams.length > 1) bits.push(`${sc.streams.length} cameras`);
      if (sc.cids.length) bits.push(`id ${sc.cids.join(', ')}`);
      return bits.join(' · ');
    };

    // --------------------------------------------------------- operations
    const need = () => {
      const items = selectedItems();
      if (!items.length) { ctx.toast('Nothing selected', 'Click a mask first.', 'warn'); return null; }
      return items;
    };
    // A destructive op that reaches past one track has to be agreed to.
    const guarded = async (verb, items) => {
      if (items.length <= 1) return true;
      return !!await ctx.ui.confirm(`${verb} ${items.length} tracks?`,
        `This will ${verb.toLowerCase()} ${scopeText(items)}. ` +
        'Ctrl+click selects a single track if that is what you meant.', verb);
    };

    const link = () => { const i = need(); if (i) submit('identity.link', { items: i }); };
    const clear = async () => {
      const i = need();
      if (i && await guarded('Clear', i)) submit('identity.clear', { items: i });
    };
    const outside = async () => {
      const i = need();
      if (i && await guarded('Flag outside', i)) submit('identity.outside', { items: i });
    };
    const assignTo = async () => {
      const items = need();
      if (!items) return;
      const input = ctx.h('input', { class: 'txt', value: String(S.nextCid) });
      const ok = await ctx.ui.modal({
        title: 'Assign to identity', ok: 'Assign',
        content: ctx.h('div', { class: 'field' },
          ctx.h('label', {}, 'Identity number'), input,
          ctx.h('div', { class: 'hint' }, `${scopeText(items)} · ${OUTSIDE} means "outside"`)),
      });
      if (!ok) return;
      const cid = parseInt(input.value, 10);
      if (Number.isFinite(cid)) submit('identity.assign', { items, cid });
    };

    const split = async () => {
      const A = app();
      const selected = [...ctx.store.get('selection')].map((id) => A.objectOf(id)).filter(Boolean);
      if (!selected.length) return ctx.toast('Nothing selected', 'Select one track to split.', 'warn');
      if (selected.length !== 1) {
        return ctx.toast('One track', 'Split one track at a time. Ctrl+click selects a single track.', 'warn');
      }
      const o = selected[0];
      const data = A.layerData((l) => l.id === o.layer_id);
      const frame = data ? data.mapFrame(o.stream, ctx.store.get('frame')) : ctx.store.get('frame');
      if (frame <= o.first_frame || frame > o.last_frame) {
        return ctx.toast('Nothing to split here',
          `${o.key} runs ${o.first_frame}–${o.last_frame} in ${o.stream}; you are at ${frame}.`, 'warn');
      }
      const cap = ctx.store.get('capture');
      try {
        const r = await ctx.call(`/${cap.id}/op`, {
          method: 'POST', body: { kind: 'split', payload: { stream: o.stream, key: o.key, frame } },
          quiet: true,
        });
        // Let the masks plugin refresh its derived layer and let every other
        // workspace observe the same confirmed op, without a second rebuild
        // when the server echoes it through the socket.
        ctx.emit('op', { ...r.op, _source: 'local' });
        ctx.toast('Split', `${o.key} → ${o.key} and ${o.key}@${frame}`);
      } catch (e) {
        ctx.toast(e.status === 409 ? 'That split cannot apply' : 'Split failed',
                  e.message || String(e), e.status === 409 ? 'warn' : 'err');
      }
    };

    const setScope = (v) => {
      S.scope = v;
      localStorage.setItem('quorum.identity.scope', v);
      app().renderInspector();
      ctx.toast('Selection scope', v === 'track'
        ? 'Clicking selects one track. Ctrl+click still does too.'
        : 'Clicking selects the whole identity. Ctrl+click selects one track.');
    };

    // ------------------------------------------------------------- walk
    // Shift+Space: play forward to the next frame that needs a human — a track
    // with no identity, or one identity on two masks in one camera — select the
    // offender and ring it. `N` is kept as an alias.
    let walking = false;
    const problemId = (p) => `${p.frame}|${p.kind}|${p.cid ?? ''}|${(p.objects || []).join(',')}`;
    const walk = async (direction = 1) => {
      const A = app();
      const cap = ctx.store.get('capture');
      const layer = maskLayer();
      if (!cap || !layer) return ctx.toast('No mask layer', 'Import one first.', 'warn');
      if (walking) return;
      walking = true;
      A.pause();
      const from = ctx.store.get('frame') + (direction >= 0 ? 1 : -1);
      try {
        // The server is told what the user is hiding, so it steps over a
        // hidden class rather than offering it and being refused. The refusal
        // still exists — `reveal` below is door 4 — but a walk that lands on a
        // frame and then declines to point at anything reads as a bug.
        const f = ctx.display.query || '';
        const cacheKey = `${cap.id}|${f}|${direction >= 0 ? 'next' : 'prev'}`;
        let batch = problemCache.get(cacheKey);
        let p = batch?.problems.find((x) => direction >= 0 ? x.frame >= from : x.frame <= from);
        if (!p && batch?.pending) {
          // A background refill may already be carrying the next runs. Prefer
          // it to starting an overlapping scan when the user walks quickly.
          await batch.pending;
          p = batch.problems.find((x) => direction >= 0 ? x.frame >= from : x.frame <= from);
        }
        if (!p) {
          // Fetch only a few nearest runs.  The server stops the forward scan
          // as soon as this batch is full; later clicks consume it locally.
          const r = await ctx.call(
            `/${cap.id}/problems?frame=${Math.max(0, from)}&direction=${direction}&limit=4` +
            (f ? `&${f}` : ''));
          batch = { problems: r.problems || [], pending: null };
          problemCache.set(cacheKey, batch);
          p = batch.problems[0] || null;
        }
        if (!p) {
          ctx.toast('Nothing left', direction >= 0
            ? 'No unset track and no repeated identity after this frame.'
            : 'Nothing before this frame either.');
          return;
        }
        const shown = await ctx.reveal(p.objects, {
          frame: p.frame,
          pulse: { ms: 2000, rings: 3, color: p.kind === 'duplicate' ? '#E27C99' : '#F5C542' },
        });
        if (!shown.shown.length) {
          ctx.toast('Skipped', 'That problem is on a track you are not being shown.', 'warn');
          return;
        }
        ctx.toast(p.kind === 'duplicate' ? 'Impossible identity' : 'No identity yet', p.why,
                  p.kind === 'duplicate' ? 'warn' : '');
        // The problem just visited can never be the next destination. Keep
        // only future runs, then refill below four in the background. This
        // leaves the following clicks instant while one small request carries
        // enough more runs to replenish the queue.
        batch.problems = batch.problems.filter((x) => direction >= 0 ? x.frame > p.frame : x.frame < p.frame);
        if (batch.problems.length < 4 && !batch.pending) {
          const edge = batch.problems.length
            ? batch.problems[batch.problems.length - 1].frame
            : p.frame;
          const start = Math.max(0, edge + (direction >= 0 ? 1 : -1));
          batch.pending = ctx.call(
            `/${cap.id}/problems?frame=${start}&direction=${direction}&limit=4` + (f ? `&${f}` : ''))
            .then((r) => {
              const seen = new Set(batch.problems.map(problemId));
              for (const next of r.problems || []) {
                if (!seen.has(problemId(next))) { batch.problems.push(next); seen.add(problemId(next)); }
              }
            })
            .catch(() => {}) // the current problem was shown; a later click retries the refill
            .finally(() => { batch.pending = null; });
        }
        app().renderInspector();
      } finally { walking = false; }
    };

    for (const [id, title, keys, run] of [
      ['link', 'Link selected tracks into one identity', ['M'], link],
      ['assign', 'Assign selected tracks to an identity…', ['A'], assignTo],
      ['clear', 'Clear identity from selection', ['C'], clear],
      ['outside', 'Flag selection as outside', ['O'], outside],
      ['split', 'Split the selected track at this frame', ['T'], split],
      ['next', 'Walk to the next frame that needs a human', ['Shift+Space', 'N'], () => walk(1)],
      ['prev', 'Walk back to the previous one', ['Shift+P'], () => walk(-1)],
      ['scope', 'Selection scope: identity / single track', ['Q'],
        () => setScope(S.scope === 'identity' ? 'track' : 'identity')],
    ]) ctx.registerCommand({ id, title, keys, tool: 'identity', group: 'Identity', run });

    // ------------------------------------------------------------ lanes
    ctx.registerLaneDecorator(({ g, stream, y, h, x0, w, cap, data, color }) => {
      if (ctx.store.get('tool') !== 'identity' || !data) return;
      const m = S.assignments[stream.key];
      if (!m) return;
      g.globalAlpha = 0.75;
      const idx = memberIndex();
      for (const [key, cid] of Object.entries(m)) {
        if (cid === OUTSIDE) continue;
        // the same rule as selection: a superseded track has no lane bar, and a
        // joined one is found in whichever layer now serves it
        const id = idx.get(`${stream.key}/${key}`);
        const o = id === undefined ? null : app().objectOf(id);
        if (!o) continue;
        const a = data.unmapFrame(stream.key, o.first_frame) / cap.n_frames;
        const b = data.unmapFrame(stream.key, o.last_frame) / cap.n_frames;
        g.fillStyle = cidColor(cid);
        g.fillRect(x0 + a * w, y + h - 3, Math.max(1.5, (b - a) * w), 3);
      }
      g.globalAlpha = 1;
    });

    // -------------------------------------------------------- inspector
    ctx.registerInspector('identity', (A) => {
      const h = ctx.h;
      // count what is really on screen: a superseded track is not a track
      const total = A.visibleObjects((l) => l.type === 'mask.rle').length;
      const assigned = Object.values(S.assignments).reduce((n, m) => n + Object.keys(m).length, 0);
      const sel = ctx.store.get('selection');
      const selItems = selectedItems();

      const rows = S.cids.map((cid) => h('div', {
        class: `item${[...sel].some((id) => membersOf(cid).includes(id)) ? ' on' : ''}`,
        onclick: () => A.select(membersOf(cid), 'set'),
      },
        h('span', { class: 'swatch', style: { background: cidColor(cid) } }),
        h('span', { class: 'k grow' }, `id ${cid}`),
        h('span', { class: 'hint mono' }, `${S.counts[cid] || 0} tracks`)));

      const scopeSwitch = h('div', { class: 'row' },
        h('span', { class: 'hint grow' }, 'a click selects'),
        ...['identity', 'track'].map((v) => h('button', {
          class: `chipbtn${S.scope === v ? ' on' : ''}`, onclick: () => setScope(v),
          title: 'Q switches this',
        }, v === 'identity' ? 'whole identity' : 'one track')));

      return h('div', {},
        ctx.ui.panel('Identity', [
          h('div', {},
            ctx.ui.stat('tracks', String(total)),
            ctx.ui.stat('with an identity', `${assigned}`, assigned === total ? '' : 'dim'),
            ctx.ui.stat('identities', String(S.cids.length)),
            ctx.ui.stat('flagged outside', String(S.counts[OUTSIDE] || 0))),
          h('div', { class: 'bar' }, h('i', { style: { width: `${total ? assigned / total * 100 : 0}%` } })),
          scopeSwitch,
          selItems.length ? h('div', { class: 'hint' }, `selected: ${scopeText(selItems)}`) : null,
          h('div', { class: 'row wrap' },
            h('button', { class: 'btn sm primary', onclick: link, title: 'M' }, 'Link', h('kbd', {}, 'M')),
            h('button', { class: 'btn sm', onclick: assignTo, title: 'A' }, 'Assign…'),
            h('button', { class: 'btn sm', onclick: outside, title: 'O' }, 'Outside'),
            h('button', { class: 'btn sm danger', onclick: clear, title: 'C' }, 'Clear'),
            h('button', { class: 'btn sm', onclick: split, title: 'T' }, 'Split here', h('kbd', {}, 'T'))),
          h('div', { class: 'row' },
            h('button', { class: 'btn sm grow', onclick: () => walk(1), title: 'Shift+Space' },
              'Next problem', h('kbd', {}, '⇧␣')),
            h('button', { class: 'btn sm', onclick: () => walk(-1), title: 'Shift+P' }, '↑')),
          h('div', { class: 'hint' },
            'Next problem loads a small nearby batch of unset or repeated identities.'),
          h('div', { class: 'row' },
            h('button', {
              class: 'btn sm grow', title: 'Suggest re-links after a gap (proposals)',
              onclick: async () => {
                const layer = maskLayer();
                if (!layer) return ctx.toast('No mask layer', 'Import one first.', 'warn');
                const { job } = await ctx.job('link_gaps', {
                  capture_id: ctx.store.get('capture').id, layer_id: layer.id });
                A.watchJob(job, (j) => ctx.toast('Suggestions', j.message || ''));
              },
            }, 'Suggest links…')),
          h('div', { class: 'hint' },
            'Alt+click reaches the mask underneath. Right-click lists everything under the cursor.'),
        ], { id: 'identity' }),
        ctx.ui.panel(`Identities (${S.cids.length})`,
          rows.length ? h('div', { class: 'list' }, rows) : h('div', { class: 'hint' }, 'Nothing assigned yet.'),
          { id: 'identity-list' }));
    });
  },
};
