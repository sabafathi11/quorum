// The Capture workspace: everything about a capture that you can change.
//
// It is core, not a plugin, because every noun on it is a core noun — a
// capture, a stream, a clock, a layer, an uploaded file. It contains no word
// that a plugin owns.
//
// It lives in the inspector beside the viewport rather than on a settings page
// of its own, and that is deliberate for one panel in particular: aligning a
// camera's clock is something you do *while watching the cameras*. Nudge the
// offset, see the forklift cross the seam at the same instant in both cells.
// The tools this replaces made you edit a command-line flag and re-encode a
// 200 MB mosaic to find out whether you had guessed right.
import { h, mount, bytes, ago, clamp } from './util.js';
import * as ui from './ui.js';
import { uploadDialog } from './upload.js';

export const CAPTURE_TOOL = {
  id: 'capture', title: 'Capture', icon: '⚙', order: 5,
  description: 'Name, frame rate, camera clocks, uploaded files, layers.',
};

// ---------------------------------------------------------------- overview
function overview(app) {
  const cap = app.store.get('capture');
  const name = h('input', { class: 'txt', value: cap.name });
  const fps = h('input', { class: 'txt', type: 'number', step: '0.001', value: cap.fps || 0 });

  const save = async () => {
    const body = {};
    if (name.value !== cap.name) body.name = name.value;
    const f = parseFloat(fps.value);
    if (Number.isFinite(f) && f > 0 && f !== cap.fps) body.fps = f;
    if (!Object.keys(body).length) return;
    await app.api.patchCapture(cap.id, body);
    ui.toast('Capture updated', body.fps
      ? 'Frame rate changed — every frame map was rebuilt from the timestamps.'
      : 'Saved.');
    await app.refreshCapture();
  };

  const canBuild = !!cap.provider_kind;
  return ui.panel('Capture', [
    h('div', { class: 'field' }, h('label', {}, 'Name'), name),
    h('div', { class: 'field' }, h('label', {}, 'Frame rate'), fps,
      h('div', { class: 'hint' },
        'The capture\'s own timeline. Every stream is read at this rate through its own ' +
        'timestamps, so changing it rebuilds the frame maps rather than moving any pixels.')),
    h('div', { class: 'row' },
      h('span', { class: 'hint mono grow' },
        `${cap.key} · ${cap.n_frames.toLocaleString()} frames · ${cap.provider || 'no provider'}`),
      cap.state === 'draft' ? h('span', { class: 'tag warn' }, 'draft') : null),
    h('div', { class: 'row' },
      h('button', { class: 'btn sm primary', onclick: save }, 'Save'),
      canBuild ? h('button', {
        class: 'btn sm', title: 'Run this capture\'s provider again over its files and settings',
        onclick: async () => {
          try {
            const { job } = await app.api.buildCapture(cap.id);
            app.watchJob(job, async (j) => {
              if (j.state === 'done') { ui.toast('Built', j.message || ''); await app.reopen(); }
            });
          } catch (e) { ui.toast('Cannot build', e.message, 'warn'); }
        },
      }, cap.state === 'draft' ? 'Build' : 'Rebuild') : null,
      h('span', { class: 'grow' })),
    canBuild ? h('div', { class: 'hint' },
      'Rebuilding is safe and is how every change to the files takes effect — the provider ' +
      'upserts, so you get the same capture back rather than a second one.') : null,
  ], { id: 'cap-overview' });
}

// ----------------------------------------------------------------- streams
// The clock offset panel. `+` means this camera runs ahead of the others and
// is delayed to match — the same sign convention `prepare.py --offset` used,
// kept on purpose so a number written in a notebook last month still means
// what it said.
function streams(app) {
  const cap = app.store.get('capture');
  const rows = (cap.streams || []).map((st) => {
    const off = st.time_offset || 0;
    const set = async (v) => {
      try {
        await app.api.patchStream(cap.id, st.key, { offset: Math.round(v * 1000) / 1000 });
        await app.refreshCapture();
        ui.toast(`${st.key} clock`, `${v > 0 ? '+' : ''}${v.toFixed(2)}s — masks and pixels moved together.`);
      } catch (e) {
        ui.toast('Cannot set that offset', e.message, 'warn');
      }
    };
    const box = h('input', { class: 'txt', type: 'number', step: '0.05', value: off.toFixed(2),
                             style: { width: '84px' },
                             onchange: (e) => set(parseFloat(e.target.value) || 0) });
    const nudge = (d) => h('button', {
      class: 'btn sm', title: `${d > 0 ? 'delay' : 'advance'} ${Math.abs(d)}s`,
      onclick: () => set(Math.round((off + d) * 1000) / 1000),
    }, d > 0 ? `+${d}` : `${d}`);

    const rends = (st.renditions || []).map((r) => r.id);
    const media = st.media || {};
    return h('div', { class: 'stream-row' },
      h('div', { class: 'row' },
        h('span', { class: 'swatch', style: { background: app.keyColor(st.key) } }),
        h('span', { class: 'k grow mono' }, st.key),
        h('span', { class: 'hint' }, `${st.width}×${st.height}`),
        h('button', {
          class: `chipbtn${st.enabled === 0 ? '' : ' on'}`, title: 'Show this view in the mosaic',
          onclick: async () => {
            await app.api.patchStream(cap.id, st.key, { enabled: st.enabled === 0 });
            await app.reopen();
          },
        }, st.enabled === 0 ? 'hidden' : 'shown')),
      h('div', { class: 'row' },
        h('span', { class: 'hint', style: { width: '42px' } }, 'clock'),
        nudge(-1), nudge(-0.1), box, nudge(0.1), nudge(1),
        off ? h('button', { class: 'btn sm', onclick: () => set(0) }, 'reset') : null),
      h('div', { class: 'row' },
        h('span', { class: 'hint grow mono' },
          [media.codec || '?',
           media.playable ? 'browser-playable' : 'needs a rendition',
           rends.length ? `renditions: ${rends.join(', ')}` : 'no renditions',
           st.has_timestamps ? '' : 'no timestamps'].filter(Boolean).join(' · ')),
        st.has_timestamps ? null : h('button', {
          class: 'btn sm', title: 'Read every frame\'s timestamp (~0.2 s)',
          onclick: async () => {
            try {
              const r = await app.api.probeStream(cap.id, st.key);
              ui.toast(`${st.key} probed`, `${r.frames} frames, ${r.codec}, ${r.duration.toFixed(1)}s`);
              await app.refreshCapture();
            } catch (e) { ui.toast('Probe failed', e.message, 'err'); }
          },
        }, 'probe')));
  });

  return ui.panel(`Streams (${(cap.streams || []).length})`, [
    ...rows,
    h('div', { class: 'hint' },
      '+ delays a camera whose clock runs ahead of the rest. The frame map is recomputed ' +
      'from that camera\'s own frame timestamps, so the masks move with the picture — and ' +
      'it is an op, so History and Ctrl+Z take it back.'),
    h('div', { class: 'row' },
      h('button', {
        class: 'btn sm', title: 'Build browser-playable copies of every stream',
        onclick: async () => {
          const f = ui.form({
            which: { type: 'string', default: 'grid,full', label: 'Renditions',
                     description: 'grid = small, for the whole mosaic. full = the camera\'s ' +
                                  'own resolution, for zooming in.' },
            force: { type: 'boolean', default: false, label: 'Rebuild existing' },
          });
          const go = await ui.modal({
            title: 'Prepare video for the browser', ok: 'Run',
            content: [h('div', { class: 'hint' },
              'One transcode per camera, at full geometry, with timestamps passed through. ' +
              'Nothing is stacked into a mosaic and no clock offset is baked in — the ' +
              'mosaic is composed here, in the browser, from whichever copy each cell needs. ' +
              'Minutes per camera.'), f.el] });
          if (!go) return;
          const { job } = await app.api.submit({ plugin: 'media', kind: 'prepare',
            params: { capture_id: cap.id, ...f.values }, capture_id: cap.id });
          app.watchJob(job, async (j) => {
            ui.toast('Media', j.message || j.state);
            await app.reopen();
          });
        },
      }, 'Prepare video…')),
  ], { id: 'cap-streams' });
}

// -------------------------------------------------------------------- data
function data(app) {
  const cap = app.store.get('capture');
  const state = app._assets || { assets: [], kinds: [] };
  const uploads = app.uploads.items;

  const kindPicker = (a) => h('select', {
    class: 'sel', title: 'What kind of file is this?',
    onchange: async (e) => {
      try {
        await app.api.patchAsset(a.id, { kind: e.target.value });
        ui.toast('Re-filed', `${a.name} is now ${e.target.selectedOptions[0].text}.`);
        await app.loadAssets();
      } catch (err) { ui.toast('Cannot re-file', err.message, 'warn'); }
    },
  }, h('option', { value: '' }, 'unsorted'),
     ...state.kinds.map((k) => {
       const o = h('option', { value: k.id }, k.title);
       if (k.id === a.kind) o.selected = true;
       return o;
     }));

  const rows = state.assets.map((a) => h('div', { class: 'item asset' },
    h('span', { class: 'k grow mono', title: a.linked ? a.source_path : a.name }, a.name),
    h('span', { class: 'hint' }, bytes(a.size)),
    // A linked file is not ours. Saying so on the row is what stops "delete"
    // from reading like it means the same thing for both kinds.
    a.linked ? h('span', {
      class: `tag ${a.present ? 'mute' : 'bad'}`,
      title: a.present ? `On the server at ${a.source_path} — not uploaded, not copied`
        : `${a.source_path} is no longer there. Re-link it, or check the mount.`,
    }, a.present ? 'linked' : 'missing') : null,
    kindPicker(a),
    a.state !== 'ready'
      ? h('span', { class: 'tag warn' }, a.state)
      : a.known ? null : h('span', { class: 'tag mute', title: 'No installed plugin claims this kind' }, '?'),
    h('button', { class: 'btn sm danger', title: a.linked ? 'Forget this file' : 'Delete this file',
      onclick: async () => {
        if (!await ui.confirm(a.linked ? `Forget ${a.name}?` : `Delete ${a.name}?`,
          a.linked
            ? 'Only the link is removed. The file itself stays where it is on the server — ' +
              'Quorum never wrote it and will not delete it.'
            : 'The file is removed from the server. Anything built from it stays as it is until ' +
              'the next rebuild.', a.linked ? 'Forget' : 'Delete')) return;
        await app.api.deleteAsset(a.id);
        await app.loadAssets();
      } }, '×')));

  const progress = uploads.length ? h('div', {},
    ...uploads.map((u) => h('div', {},
      h('div', { class: 'row' },
        h('span', { class: 'k grow mono' }, u.file.name),
        h('span', { class: `tag ${u.state === 'failed' ? 'bad' : u.state === 'done' ? 'ok' : 'warn'}` },
          u.state),
        ['queued', 'uploading', 'starting', 'retrying'].includes(u.state)
          ? h('button', { class: 'btn sm', onclick: () => { u.cancel(); } }, 'stop') : null),
      h('div', { class: 'bar' }, h('i', { style: { width: `${(u.sent / Math.max(1, u.file.size)) * 100}%` } })),
      u.error ? h('div', { class: 'hint' }, u.error) : null)),
    h('button', { class: 'btn sm', onclick: () => { app.uploads.clearFinished(); app.renderInspector(); } },
      'clear finished')) : null;

  return ui.panel(`Data (${state.assets.length})`, [
    h('div', { class: 'hint' },
      'Files for this capture. The kinds come from the plugins installed here — change one ' +
      'and whoever wants that kind will simply see it appear. Nothing has to be uploaded ' +
      'twice, and a file that is already on the server does not have to be uploaded at all.'),
    rows.length ? h('div', { class: 'list' }, rows) : h('div', { class: 'hint' }, 'Nothing here yet.'),
    progress,
    h('div', { class: 'row' },
      h('button', { class: 'btn sm primary',
        onclick: () => uploadDialog(app, cap.id).then(() => app.renderInspector()) }, 'Upload…'),
      h('button', {
        class: 'btn sm',
        title: 'Point at a file the server can already open — the recordings share, a scratch '
             + 'disk, anywhere inside its media roots. Nothing is copied.',
        onclick: async () => {
          const { pick } = await import('./browse.js');
          const paths = await pick(app, { multiple: true, title: 'Files on the server' });
          if (!paths?.length) return;
          let ok = 0;
          for (const path of paths) {
            try { await app.api.linkAsset(cap.id, path); ok += 1; }
            catch (e) { ui.toast('Cannot link', `${path}: ${e.message}`, 'warn'); }
          }
          if (ok) {
            ui.toast(`Linked ${ok} file(s)`,
              'Read where they lie — nothing was copied. File them under a kind to use them.');
            await app.loadAssets();
            app.renderInspector();
          }
        },
      }, 'From the server…')),
  ], { id: 'cap-data' });
}

// ------------------------------------------------------------------ layers
function layers(app) {
  const cap = app.store.get('capture');
  const types = [...new Set((app.manifests || []).flatMap((m) =>
    (m.provides.layerTypes || []).map((t) => t.id)))];

  const rows = (cap.layers || []).map((l) => h('div', { class: 'item' },
    h('span', { class: 'k grow' }, l.name),
    h('span', { class: 'hint mono' }, `${l.n_objects} obj`),
    h('select', {
      class: 'sel', title: 'What kind of geometry is in this layer?',
      onchange: async (e) => {
        try {
          await app.api.patchLayer(l.id, { type: e.target.value });
          ui.toast('Layer type changed',
            `${l.name} is now ${e.target.value}. The payloads were not touched — this says ` +
            'who should draw them.');
          await app.reopen();
        } catch (err) { ui.toast('Cannot change that', err.message, 'warn'); }
      },
    }, ...types.map((t) => {
      const o = h('option', { value: t }, t);
      if (t === l.type) o.selected = true;
      return o;
    })),
    h('button', { class: 'btn sm danger', onclick: async () => {
      if (!await ui.confirm(`Delete layer ${l.name}?`,
        `${l.n_objects} objects and every shape in them. Ops that name them stay in the log.`,
        'Delete')) return;
      await app.api.deleteLayer(l.id);
      await app.reopen();
    } }, '×')));

  return ui.panel(`Layers (${(cap.layers || []).length})`, [
    rows.length ? h('div', { class: 'list' }, rows)
      : h('div', { class: 'hint' }, 'No layers — run an importer from the Data panel below.'),
    h('div', { class: 'hint' },
      'Changing a layer\'s type re-labels what is in it; nothing is converted, because the ' +
      'core never opened the payloads. The plugin that owns the new type takes over on the ' +
      'next paint.'),
  ], { id: 'cap-layers', collapsed: true });
}

// -------------------------------------------------------------- readiness
// What each plugin says it cannot do here, and why. The server enforces this
// (a blocked job is refused); this panel is where somebody finds out what to
// upload, rather than staring at a grey button.
function readiness(app) {
  const r = app._readiness || {};
  const blocked = Object.entries(r).filter(([, v]) => !v.ok);
  if (!blocked.length) return null;
  return ui.panel('Not ready', blocked.map(([pid, v]) => h('div', {},
    h('div', { class: 'row' },
      h('span', { class: 'k grow' }, (app.manifests || []).find((m) => m.id === pid)?.name || pid),
      h('span', { class: 'tag warn' }, 'blocked')),
    ...v.missing.map((m) => h('div', {},
      h('div', { class: 'hint' }, m.why),
      m.assetKind ? h('button', { class: 'btn sm', onclick: () =>
        uploadDialog(app, app.store.get('capture').id, { kind: m.assetKind })
          .then(() => app.renderInspector()) }, m.fix || 'Upload it…') : null)))),
    { id: 'cap-ready' });
}

export function capturePanels(app) {
  return h('div', {}, overview(app), streams(app), data(app), readiness(app), layers(app));
}

// ------------------------------------------------------------ new capture
export async function newCaptureDialog(app) {
  const providers = (app.manifests || []).flatMap((m) =>
    (m.provides.providers || []).map((p) => ({ ...p, plugin: m.id, pluginName: m.name })));
  // A provider that reads uploads is the one you want by default; the ones
  // that read a directory on the server are the legacy path.
  const preferred = providers.find((p) => p.plugin === 'multiview') || providers[0];
  if (!preferred) { ui.toast('No providers', 'No installed plugin can make a capture.', 'warn'); return; }

  let picked = preferred;
  const name = h('input', { class: 'txt', placeholder: 'e.g. Line 3, Tuesday morning' });
  const chips = h('div', { class: 'row wrap' }, ...providers.map((p) => {
    const b = h('button', { class: `chipbtn${p === picked ? ' on' : ''}`, title: p.description || '',
      onclick: () => { picked = p; [...chips.children].forEach((c) => c.classList.remove('on'));
                       b.classList.add('on'); why.textContent = p.description || ''; } },
      p.title, h('span', { class: 'hint' }, p.pluginName));
    return b;
  }));
  const why = h('div', { class: 'hint' }, picked.description || '');

  const go = await ui.modal({
    title: 'New capture', ok: 'Create',
    content: [
      h('div', { class: 'field' }, h('label', {}, 'Name'), name),
      h('div', { class: 'field' }, h('label', {}, 'Built by'), chips, why),
      h('div', { class: 'hint' },
        'This makes an empty capture you can upload into. Nothing is read until you build it, ' +
        'and you can come back to it tomorrow.'),
    ],
  });
  if (!go) return;
  const key = (name.value.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
    || 'capture') + '-' + Math.random().toString(36).slice(2, 6);
  try {
    const { capture } = await app.api.createCapture({
      key, name: name.value.trim() || 'Untitled capture',
      provider: picked.plugin, provider_kind: picked.id });
    await app.goto(capture.id, CAPTURE_TOOL.id);
    ui.toast('Draft created', 'Upload its files on the Data panel, then press Build.');
  } catch (e) { ui.toast('Could not create it', e.message, 'err'); }
}
