// Review. One proposal at a time, with the frame it rests on already on screen
// and the objects it talks about already selected — so the decision is made by
// looking at the video, not by reading a confidence number.

export default {
  id: 'proposals',

  activate(ctx) {
    const app = () => window.quorum;
    const S = { items: [], counts: {}, cursor: 0, filter: 'open' };

    const load = async () => {
      const cap = ctx.store.get('capture');
      if (!cap) return;
      const f = ctx.display.query;
      const r = await ctx.call(`/${cap.id}${f ? `?${f}` : ''}`);
      S.items = r.items; S.counts = r.counts; S.withheld = r.withheld || 0;
      S.cursor = Math.min(S.cursor, Math.max(0, open().length - 1));
      app().renderInspector();
      app().renderRail();
    };
    const open = () => S.items.filter((p) => S.filter === 'all' || p.state === S.filter);
    const current = () => open()[S.cursor] || null;

    ctx.on('capture', load);
    ctx.on('proposals', load);
    ctx.on('display', load);        // hiding a class changes what may be offered
    ctx.onOp((op) => { if (op.kind === 'proposals.seeded') load(); });
    if (ctx.store.get('capture')) load();

    // socket events the core forwards verbatim
    const sock = () => app().socket;
    const patchSocket = () => {
      const s = sock();
      if (!s || s._proposals) return;
      s._proposals = true;
      s.handlers.proposals = () => load();
    };
    setInterval(patchSocket, 1000);

    // ------------------------------------------------------------- focus
    // Jump-and-point goes through the core's `reveal` — door 4 of the display
    // authority. A proposal about a track the reviewer is not being shown must
    // not silently select nothing; it says so, and the queue can move on.
    const focus = async (p) => {
      if (!p) return;
      const A = app();
      const data = A.primaryData();
      const f = p.focus || {};
      let frame = null;
      if (data && f.stream && f.stream_frame != null) frame = data.unmapFrame(f.stream, f.stream_frame);
      else if (f.frame != null) frame = f.frame;
      const r = await A.reveal(f.objects || [], { frame });
      if ((f.objects || []).length && !r.shown.length) {
        ctx.toast('Not shown', 'This proposal names tracks you are currently hiding.', 'warn');
      }
    };

    const decide = async (p, decision) => {
      if (!p) return;
      try {
        await ctx.call(`/${ctx.store.get('capture').id}/${p.id}/${decision}`, { method: 'POST', body: {} });
      } catch { return; }
      await load();
      const next = current();
      if (next) focus(next);
    };

    const move = (d) => {
      const list = open();
      if (!list.length) return;
      S.cursor = Math.max(0, Math.min(list.length - 1, S.cursor + d));
      app().renderInspector();
      focus(current());
    };

    for (const [id, title, keys, run] of [
      ['accept', 'Accept this proposal', ['Enter'], () => decide(current(), 'accept')],
      ['reject', 'Reject this proposal', ['Backspace'], () => decide(current(), 'reject')],
      ['next', 'Next proposal', ['Down'], () => move(1)],
      ['prev', 'Previous proposal', ['Up'], () => move(-1)],
      // deliberately not Space: play/pause means the same thing in every tool
      ['focus', 'Show this proposal in the viewport', ['G'], () => focus(current())],
    ]) ctx.registerCommand({ id, title, keys, tool: 'review', group: 'Review', run });

    // --------------------------------------------------------- inspector
    ctx.registerInspector('review', () => {
      const h = ctx.h;
      const list = open();
      const p = current();
      const pct = (v) => `${Math.round(v * 100)}%`;

      const card = p ? h('div', {},
        h('div', { class: 'row', style: { marginBottom: '6px' } },
          h('span', { class: 'tag mute mono' }, `${S.cursor + 1} / ${list.length}`),
          h('span', { class: 'grow' }),
          h('span', { class: 'hint mono' }, p.source)),
        h('div', { style: { fontWeight: 600, marginBottom: '6px' } }, p.title),
        h('div', { class: 'bar' }, h('i', {
          style: { width: pct(p.confidence), background: p.confidence > 0.7 ? 'var(--accent)' : 'var(--ochre)' } })),
        h('div', { class: 'hint', style: { margin: '4px 0 8px' } },
          `confidence ${pct(p.confidence)} · ${p.note || ''}`),
        p.stale_reason ? h('div', { class: 'tag warn', style: { marginBottom: '8px' } },
          `out of date — ${p.stale_reason}`) : null,
        h('div', {}, ...Object.entries(p.support || {}).map(([k, v]) =>
          ctx.ui.stat(k.replace(/_/g, ' '), String(v)))),
        h('div', { class: 'row', style: { marginTop: '10px' } },
          h('button', { class: 'btn sm primary grow', onclick: () => decide(p, 'accept') },
            'Accept', h('kbd', {}, '⏎')),
          h('button', { class: 'btn sm danger', onclick: () => decide(p, 'reject') },
            'Reject', h('kbd', {}, '⌫'))),
        h('div', { class: 'row', style: { marginTop: '6px' } },
          h('button', { class: 'btn sm', onclick: () => move(-1) }, '↑'),
          h('button', { class: 'btn sm', onclick: () => move(1) }, '↓'),
          h('button', { class: 'btn sm grow', onclick: () => focus(p) }, 'Show me', h('kbd', {}, 'G')))
      ) : h('div', { class: 'hint' },
        S.filter === 'open' ? 'Nothing waiting. Generators put their suggestions here — nothing else may write an annotation on a machine’s say-so.'
          : S.filter === 'stale' ? 'Nothing has gone out of date. A suggestion goes stale when an edit removes a track it named — it is kept, not deleted, so the generator can still be scored on it.'
            : 'Nothing here.');

      const tabs = ['open', 'accepted', 'rejected', 'stale', 'all'].map((f) => h('button', {
        class: `chipbtn${S.filter === f ? ' on' : ''}`,
        onclick: () => { S.filter = f; S.cursor = 0; app().renderInspector(); },
      }, `${f} ${f === 'all' ? S.items.length : (S.counts[f] || 0)}`));

      return h('div', {},
        ctx.ui.panel('Review queue', [
          h('div', { class: 'row wrap' }, ...tabs),
          S.withheld ? h('div', { class: 'hint' },
            `${S.withheld} suggestion(s) held back — they are about classes you are hiding. ` +
            'They are not lost; show the class and they come back.') : null,
          card], { id: 'review' }),
        list.length > 1 ? ctx.ui.panel('Up next', h('div', { class: 'list' },
          list.slice(0, 40).map((q, i) => h('div', {
            class: `item${i === S.cursor ? ' on' : ''}`,
            onclick: () => { S.cursor = i; app().renderInspector(); focus(q); },
          },
            h('span', { class: 'swatch', style: { background: q.confidence > 0.7 ? 'var(--accent)' : 'var(--ochre)' } }),
            h('span', { class: 'k grow' }, q.title),
            h('span', { class: 'hint mono' }, pct(q.confidence))))), { id: 'review-list' }) : null);
    });
  },
};
