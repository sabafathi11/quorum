// The Edit workspace: what the model drew, and what you did about it.
//
// Editing is per-track by default — the opposite of Identity, where a click
// means "this person". Getting that backwards is how the desktop tool let one
// keystroke delete a track in six cameras at once.

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
    };
    const setOf = (m, s) => new Set(m[s] || []);

    // A tool that writes through the masks endpoint can reserve its request
    // token before the request leaves the browser. WebSocket delivery is not
    // ordered against the HTTP response, so this also covers an early echo.
    ctx.on('masks.local-ref', (ref) => { if (ref) S.localRefs.add(ref); });
    ctx.on('masks.cancel-local-ref', (ref) => S.localRefs.delete(ref));

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
    ctx.on('capture', load);
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
      S.busy = true;
      try {
        const r = await ctx.call(`/${cap.id}/op`,
                                 { method: 'POST', body: { kind, payload }, quiet: true });
        S.localOps.add(r.op.id);
        Object.assign(S, r.state);
        app().bus?.emit?.('op', { ...r.op, _source: 'local' });
        if (NEEDS_REBUILD.has(kind.replace(/^masks\./, ''))) await ctx.reloadLayers();
        else { app().renderInspector(); ctx.invalidate(); }
        return r;
      } catch (e) {
        // A 409 is the server refusing a structural edit *and saying why* —
        // "3 is joined to 1+2+3, unjoin it first". Identity ops commute and
        // these do not, so the reason is the whole point; swallowing it leaves
        // the user pressing a key that silently does nothing.
        if (e.status === 409) ctx.toast('That edit cannot apply', e.message, 'warn');
        else ctx.toast('Edit failed', e.message || String(e), 'err');
        return null;
      } finally { S.busy = false; }
    };

    const del = async () => {
      const sel = need('delete'); if (!sel) return;
      await edit('delete', { stream: sel[0].stream, keys: sel.map((o) => o.key) });
      ctx.toast('Deleted', `${sel.length} track(s) hidden — H shows them, Ctrl+Z takes it back.`);
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

    for (const [id, title, keys, run] of [
      ['delete', 'Delete the selected tracks', ['D'], del],
      ['restore', 'Restore deleted tracks', ['Shift+D'], restore],
      ['purge', 'Purge the selected tracks', ['P'], purge],
      ['split', 'Cut the selected track at this frame', ['T'], split],
      ['join', 'Join the selected tracks into one', ['J'], join],
      ['unjoin', 'Take a joined track apart', ['U'], unjoin],
      ['showdeleted', 'Show / hide deleted tracks', ['H'], toggleDeleted],
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

      const btn = (label, key, fn, cls = 'btn sm') =>
        h('button', { class: cls, onclick: fn, title: key }, label, h('kbd', {}, key));

      return h('div', {},
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
            btn('Delete', 'D', del, 'btn sm danger'),
            btn('Restore', '⇧D', restore),
            btn('Purge', 'P', purge, 'btn sm danger')),
          h('div', { class: 'row wrap' },
            btn('Cut here', 'T', split, 'btn sm primary'),
            btn('Join', 'J', join),
            btn('Unjoin', 'U', unjoin)),
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
