// Uploading, and the dialog around it.
//
// Resumable in chunks, because one camera video here is 150 MB and this is
// meant to run over a 1.5 MB/s link one day: a browser tab that loses the
// connection at 140 MB must not start again at zero. The server tells us where
// it got to (409 with the byte count) and we carry on from there.
//
// Nothing in this file knows what any of these files *are*. The kinds it
// offers come from the server, declared by whichever plugins wanted them — so
// a plugin added next month gets an upload UI without this file changing. That
// is the whole point of the asset design; see docs/DESIGN §1.
import { h, mount, bytes } from './util.js';
import * as ui from './ui.js';

const CHUNK = 4 * 1024 * 1024;
// A chunk that failed on the wire is retried here rather than failing the whole
// file. This is the difference between the design's promise and its behaviour:
// the *server* has always been resumable, but the client treated one dropped
// connection at 140 MB as the end of the upload, so resuming meant a person
// noticing and dragging the file in again. On a link that drops, that is the
// same as not being resumable at all.
//
// The server is the authority on where it got to, so a retry asks it rather
// than assuming: `GET /api/assets/{id}` returns `received`, and the loop
// continues from there. That also makes a partially-received chunk harmless.
const CHUNK_TRIES = 5;
const RETRY_BASE = 800;
// A stalled socket must eventually be called stalled. Without this an upload
// that loses the far end hangs on `x.send` and never reaches the retry above:
// no error, no progress, no way back except reloading the tab.
const CHUNK_TIMEOUT = 180000;

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

export class Upload {
  constructor(api, { captureId, file, kind }) {
    this.api = api;
    this.captureId = captureId;
    this.file = file;
    this.kind = kind;
    this.sent = 0;
    this.state = 'queued';
    this.error = '';
    this.onProgress = null;
    this.cancelled = false;
  }

  cancel() { this.cancelled = true; this.xhr?.abort(); }

  async run() {
    try {
      this.state = 'starting';
      const { asset } = await this.api.createAsset(this.captureId, {
        name: this.file.name, size: this.file.size, kind: this.kind,
        content_type: this.file.type || '' });
      this.asset = asset;
      this.state = 'uploading';
      let offset = asset.received || 0;
      let tries = 0;
      while (offset < this.file.size) {
        if (this.cancelled) { this.state = 'cancelled'; return null; }
        const end = Math.min(offset + CHUNK, this.file.size);
        try {
          const r = await this.put(this.file.slice(offset, end), offset);
          offset = r.received;
          tries = 0;
          this.state = 'uploading';
        } catch (e) {
          if (e.aborted) throw e;
          // The server's refusal carries the answer: resume from *there*.
          // Retrying blind, or starting over, are both worse.
          if (e.received != null && e.received !== offset) { offset = e.received; continue; }
          // A refusal the server meant — the wrong kind, too big, gone. Asking
          // again would get the same answer, more slowly.
          if (e.status && e.status !== 408 && e.status !== 429
              && e.status < 500) throw e;
          if (++tries >= CHUNK_TRIES) throw e;
          this.state = 'retrying';
          this.error = e.message || String(e);
          this.onProgress?.(this);
          await sleep(Math.min(RETRY_BASE * 2 ** (tries - 1), 8000)
                      * (0.7 + Math.random() * 0.6));
          if (this.cancelled) { this.state = 'cancelled'; return null; }
          // Where did it actually get to? A chunk can be received in full and
          // the acknowledgement lost, and re-sending from the old offset would
          // then be refused for the rest of the file.
          try {
            const { asset: fresh } = await this.api.req(`/api/assets/${this.asset.id}`,
                                                        { quiet: true });
            if (typeof fresh?.received === 'number') offset = fresh.received;
          } catch { /* the next PUT's 409 carries the same answer */ }
          continue;
        }
        this.sent = offset;
        this.onProgress?.(this);
      }
      this.error = '';
      this.state = 'finishing';
      this.onProgress?.(this);
      const done = await this.api.completeAsset(this.asset.id);
      this.asset = done.asset;
      this.state = 'done';
      this.onProgress?.(this);
      return done.asset;
    } catch (e) {
      this.state = 'failed';
      this.error = e.message || String(e);
      this.onProgress?.(this);
      throw e;
    }
  }

  // XHR rather than fetch: it reports upload progress, and fetch still does
  // not. On a 150 MB file over a slow link that is the difference between a
  // progress bar and a frozen dialog.
  put(blob, offset) {
    return new Promise((resolve, reject) => {
      const x = this.xhr = new XMLHttpRequest();
      x.open('PUT', `/api/assets/${this.asset.id}/data?offset=${offset}`);
      if (this.api.token) x.setRequestHeader('authorization', `Bearer ${this.api.token}`);
      x.upload.onprogress = (e) => {
        this.sent = offset + e.loaded;
        this.onProgress?.(this);
      };
      x.onload = () => {
        if (x.status >= 200 && x.status < 300) return resolve(JSON.parse(x.responseText));
        let detail = `${x.status} ${x.statusText}`;
        try { detail = JSON.parse(x.responseText).detail || detail; } catch { /* not json */ }
        const err = Object.assign(new Error(detail), { status: x.status });
        const got = x.getResponseHeader('x-quorum-received');
        if (got != null) err.received = parseInt(got, 10);
        reject(err);
      };
      x.onerror = () => reject(new Error('the connection dropped'));
      x.onabort = () => reject(this.cancelled
        ? Object.assign(new Error('cancelled'), { aborted: true })
        : new Error('the connection dropped'));
      x.timeout = CHUNK_TIMEOUT;
      x.ontimeout = () => reject(new Error(
        `this chunk stalled for ${Math.round(CHUNK_TIMEOUT / 1000)}s`));
      x.send(blob);
    });
  }
}

// --------------------------------------------------------------------- queue
export class UploadQueue {
  constructor(app) {
    this.app = app;
    this.items = [];
    this.running = false;
  }

  add(captureId, files, kind) {
    for (const f of files) {
      const u = new Upload(this.app.api, { captureId, file: f, kind });
      u.onProgress = () => this.app.bus.emit('uploads', this.items);
      this.items.push(u);
    }
    this.app.bus.emit('uploads', this.items);
    this.pump();
  }

  // One at a time: two large uploads over one link finish later than the same
  // two in sequence, and a queue you can watch beats a race you cannot.
  async pump() {
    if (this.running) return;
    this.running = true;
    try {
      for (const u of this.items) {
        if (u.state !== 'queued') continue;
        try { await u.run(); } catch { /* the item carries its own error */ }
        this.app.bus.emit('uploads', this.items);
      }
    } finally {
      this.running = false;
      this.app.bus.emit('assets', null);
    }
  }

  get active() { return this.items.filter((u) => !['done', 'failed', 'cancelled'].includes(u.state)); }
  clearFinished() {
    this.items = this.items.filter((u) => !['done', 'cancelled'].includes(u.state));
    this.app.bus.emit('uploads', this.items);
  }
}

// -------------------------------------------------------------------- dialog
// Built entirely from what the server says exists. If it lists a kind, you can
// upload one; this file does not know what any of them mean.
export async function uploadDialog(app, captureId, { kind = '', title = 'Upload files' } = {}) {
  const { kinds } = await app.api.assets(captureId);
  if (!kinds.length) {
    return ui.modal({ title, ok: 'Close', cancel: '', content:
      h('div', { class: 'hint' }, 'No plugin declares a kind of file to upload. ' +
        'A plugin adds one with PLUGIN.asset_kind(...); see docs/PLUGINS.md.') });
  }
  let picked = kinds.some((k) => k.id === kind) ? kind : kinds[0].id;
  let files = [];

  const kindOf = () => kinds.find((k) => k.id === picked);
  const list = h('div', { class: 'list' });
  const input = h('input', { type: 'file', multiple: true, style: { display: 'none' },
                             onchange: (e) => addFiles([...e.target.files]) });
  const drop = h('div', {
    class: 'dropzone',
    onclick: () => input.click(),
    ondragover: (e) => { e.preventDefault(); drop.classList.add('over'); },
    ondragleave: () => drop.classList.remove('over'),
    ondrop: (e) => { e.preventDefault(); drop.classList.remove('over');
                     addFiles([...e.dataTransfer.files]); },
  }, h('div', {}, 'Drop files here, or click to choose'),
     h('div', { class: 'hint' }, ''));

  const addFiles = (fs) => { files = [...files, ...fs]; render(); };
  const chips = h('div', { class: 'row wrap' });

  const render = () => {
    const k = kindOf();
    mount(chips, ...kinds.map((kk) => h('button', {
      class: `chipbtn${kk.id === picked ? ' on' : ''}`,
      title: kk.description || '',
      onclick: () => { picked = kk.id; render(); },
    }, `${kk.icon || '▤'} ${kk.title}`, h('span', { class: 'hint' }, kk.pluginName))));
    drop.lastChild.textContent = [k?.description,
      k?.accept?.length ? `Usually ${k.accept.join(', ')}` : '',
      k && !k.multiple ? 'One per capture.' : ''].filter(Boolean).join('  ·  ');
    mount(list, ...files.map((f, i) => h('div', { class: 'item' },
      h('span', { class: 'k grow mono' }, f.name),
      h('span', { class: 'hint' }, bytes(f.size)),
      h('button', { class: 'btn sm', onclick: () => { files.splice(i, 1); render(); } }, '×'))));
  };
  render();

  const go = await ui.modal({
    title, wide: true, ok: 'Upload',
    content: [
      h('div', { class: 'hint', style: { marginBottom: '8px' } },
        'What kind of file is this? The list comes from the plugins installed on this server — ' +
        'you can change an answer later without uploading again.'),
      chips, drop, list, input],
  });
  if (!go || !files.length) return [];
  app.uploads.add(captureId, files, picked);
  ui.toast('Uploading', `${files.length} file(s) → ${kindOf()?.title}`);
  return files;
}
