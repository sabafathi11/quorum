// REST + websocket client. Every call funnels through `req` so an auth failure
// or a dead server is reported once, in one voice.
//
// This client is expected to run at the far end of a slow link: the GPU box is
// reached over `ssh -L`, where a round trip is ~700 ms rather than the ~1 ms it
// is on a laptop. Three things follow, and they are the difference between
// "works" and "works there":
//
//   nothing waits forever   a request with no timeout does not fail on a link
//                           that drops, it *hangs*, and the button that started
//                           it stays dead until the tab is reloaded.
//   reads are retried       one lost packet on a 700 ms link is a lost panel.
//                           GETs are safe to repeat, so they are.
//   the latency is measured  because the client has decisions to make that
//                           depend on it — how much mask runway to prefetch —
//                           and guessing is what makes it lag on one link and
//                           waste bandwidth on the other.

// A read that has not answered in this long is treated as lost. Generous: it
// is a ceiling on a hung socket, not a target.
const READ_TIMEOUT = 45000;
// Writes get longer and are never retried — a repeated POST is a second edit.
const WRITE_TIMEOUT = 120000;
const RETRIES = 2;
// Statuses worth trying again. A 500 is not here on purpose: the server meant
// it, and asking twice just makes two log entries.
const TRANSIENT = new Set([408, 425, 429, 502, 503, 504]);

export class Api {
  constructor(token = localStorage.getItem('quorum.token') || '') {
    this.token = token;
    this.onError = null;
    // Called with ('slow'|'retry'|'ok', detail) so the shell can say what is
    // happening rather than leaving a frozen panel to explain itself.
    this.onStatus = null;
    // Round trip, milliseconds, exponentially smoothed over small reads. Seeded
    // optimistically; the first real answer dominates it immediately.
    this.rtt = 40;
  }

  headers(extra = {}) {
    const hh = { ...extra };
    if (this.token) hh.authorization = `Bearer ${this.token}`;
    return hh;
  }

  // Only small reads inform the estimate. A 40 MB export or a mask window on a
  // busy layer is dominated by transfer and by the server, and folding those in
  // would make the client believe the link is far worse than it is.
  noteRtt(ms, bytes) {
    if (bytes > 65536) return;
    this.rtt = this.rtt * 0.7 + ms * 0.3;
  }

  // `quiet` suppresses the global error toast so a caller can present the
  // failure in its own words — a 409 from a structural edit carries a reason
  // worth reading, and "Request failed" is not it.
  async req(path, { method = 'GET', body, raw = false, quiet = false,
                    timeout = 0, retries = null, signal = null } = {}) {
    const idempotent = method === 'GET' || method === 'HEAD';
    const limit = retries == null ? (idempotent ? RETRIES : 0) : retries;
    const ms = timeout || (idempotent ? READ_TIMEOUT : WRITE_TIMEOUT);
    let last;
    for (let attempt = 0; ; attempt++) {
      try {
        const res = await this.once(path, { method, body, ms, signal });
        if (attempt) this.onStatus?.('ok', '');
        return raw ? res : res.json();
      } catch (e) {
        last = e;
        // The caller cancelled, or the failure is one that repeating cannot
        // mend (401, 404, 409 — the server has an opinion and it will not
        // change). Either way, stop.
        if (e.name === 'AbortError' && signal?.aborted) throw e;
        const retryable = e.transient === true
          || (e.status !== undefined && TRANSIENT.has(e.status));
        if (attempt >= limit || !retryable) break;
        this.onStatus?.('retry', `${method} ${path} — ${e.message}`);
        await sleep(Math.min(400 * 2 ** attempt, 4000) * (0.7 + Math.random() * 0.6));
      }
    }
    if (!quiet) {
      if (last.status === undefined) this.onError?.(`cannot reach the server (${last.message})`);
      else this.onError?.(last.message, last.status);
    }
    throw last;
  }

  // One attempt. Everything that can go wrong on a slow link is turned into an
  // Error carrying `transient`, so the loop above never has to know about
  // fetch's habit of reporting every network fault as the same TypeError.
  async once(path, { method, body, ms, signal }) {
    const ctl = new AbortController();
    const onAbort = () => ctl.abort();
    signal?.addEventListener('abort', onAbort, { once: true });
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; ctl.abort(); }, ms);
    const started = performance.now();
    try {
      const init = {
        method, signal: ctl.signal,
        headers: this.headers(body !== undefined ? { 'content-type': 'application/json' } : {}),
      };
      if (body !== undefined) init.body = JSON.stringify(body);
      let res;
      try {
        res = await fetch(path, init);
      } catch (e) {
        if (timedOut) {
          throw Object.assign(
            new Error(`the server did not answer within ${Math.round(ms / 1000)}s`),
            { transient: true });
        }
        if (signal?.aborted) throw e;
        // A dropped connection. Indistinguishable from a server that is not
        // there, which is why it is worth retrying before saying so.
        throw Object.assign(new Error(e.message || 'the connection dropped'),
                            { transient: true });
      }
      const elapsed = performance.now() - started;
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
        throw Object.assign(new Error(detail), { status: res.status });
      }
      this.noteRtt(elapsed, Number(res.headers.get('content-length') || 0));
      return res;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
    }
  }

  session()            { return this.req('/api/session'); }
  // What is on the *server's* disks, inside the media roots. The other half of
  // uploading: on the GPU box the recordings are already there, and asking for
  // them by path is the whole feature.
  browse(path = '', q = '') {
    const qs = new URLSearchParams();
    if (path) qs.set('path', path);
    if (q) qs.set('q', q);
    return this.req(`/api/browse${qs.toString() ? `?${qs}` : ''}`);
  }
  linkAsset(captureId, path, kind = '') {
    const at = captureId ? `/api/captures/${captureId}/assets/link` : '/api/assets/link';
    return this.req(at, { method: 'POST', body: { path, kind } });
  }
  captures()           { return this.req('/api/captures'); }
  capture(id)          { return this.req(`/api/captures/${id}`); }
  ops(id, since = 0)   { return this.req(`/api/captures/${id}/ops?since=${since}`); }
  postOp(id, op)       { return this.req(`/api/captures/${id}/ops`, { method: 'POST', body: op }); }
  history(id, limit = 120) { return this.req(`/api/captures/${id}/ops?all=true&limit=${limit}`); }
  undoLast(id, kind = '') {
    return this.req(`/api/captures/${id}/undo${kind ? `?kind=${encodeURIComponent(kind)}` : ''}`,
                    { method: 'POST' });
  }
  undoOp(id, oid)      { return this.req(`/api/captures/${id}/ops/${oid}/undo`, { method: 'POST' }); }
  redoOp(id, oid)      { return this.req(`/api/captures/${id}/ops/${oid}/redo`, { method: 'POST' }); }
  objects(lid, stream) { return this.req(`/api/layers/${lid}/objects${stream ? `?stream=${stream}` : ''}`); }
  // `quiet`: this one is asked again on every frame change, so a link that is
  // down would raise a toast per frame. `LayerData` handles the failure by
  // leaving the window unfilled, and the connection badge is what says why.
  frames(lid, frame, span = 0) {
    return this.req(`/api/layers/${lid}/frames?frame=${frame}&span=${span}`, { quiet: true });
  }
  jobs(capture)        { return this.req(`/api/jobs${capture ? `?capture=${capture}` : ''}`); }
  submit(job)          { return this.req('/api/jobs', { method: 'POST', body: job }); }
  cancelJob(jid)       { return this.req(`/api/jobs/${jid}/cancel`, { method: 'POST' }); }
  doc(cid, ns, key)    { return this.req(`/api/captures/${cid}/docs/${ns}/${key}`); }
  putDoc(cid, ns, key, value, version) {
    return this.req(`/api/captures/${cid}/docs/${ns}/${key}`, { method: 'PUT', body: { value, version } });
  }
  plugin(pid, path, opts) { return this.req(`/api/p/${pid}${path}`, opts); }

  // -- captures you can edit ------------------------------------------------
  createCapture(body)  { return this.req('/api/captures', { method: 'POST', body }); }
  patchCapture(id, body) { return this.req(`/api/captures/${id}`, { method: 'PATCH', body }); }
  buildCapture(id, params = {}) {
    return this.req(`/api/captures/${id}/build`, { method: 'POST', body: { params }, quiet: true });
  }
  deleteCapture(id)    { return this.req(`/api/captures/${id}`, { method: 'DELETE' }); }
  patchStream(id, key, body) {
    return this.req(`/api/captures/${id}/streams/${key}`, { method: 'PATCH', body, quiet: true });
  }
  deleteStream(id, key) { return this.req(`/api/captures/${id}/streams/${key}`, { method: 'DELETE' }); }
  probeStream(id, key) {
    return this.req(`/api/captures/${id}/streams/${key}/probe`, { method: 'POST', quiet: true });
  }
  patchLayer(lid, body) { return this.req(`/api/layers/${lid}`, { method: 'PATCH', body, quiet: true }); }
  deleteLayer(lid)      { return this.req(`/api/layers/${lid}`, { method: 'DELETE' }); }
  labels(id)            { return this.req(`/api/captures/${id}/labels`); }
  readiness(id)         { return this.req(`/api/captures/${id}/readiness`); }

  // -- assets ---------------------------------------------------------------
  assets(capture)       { return this.req(`/api/assets${capture ? `?capture=${capture}` : ''}`); }
  createAsset(capture, body) {
    return this.req(capture ? `/api/captures/${capture}/assets` : '/api/assets',
                    { method: 'POST', body, quiet: true });
  }
  completeAsset(aid)    { return this.req(`/api/assets/${aid}/complete`, { method: 'POST', quiet: true }); }
  patchAsset(aid, body) { return this.req(`/api/assets/${aid}`, { method: 'PATCH', body, quiet: true }); }
  deleteAsset(aid)      { return this.req(`/api/assets/${aid}`, { method: 'DELETE' }); }

  // -- exports --------------------------------------------------------------
  exports(capture)      { return this.req(`/api/exports${capture ? `?capture=${capture}` : ''}`); }
  deleteExport(path)    {
    return this.req(`/api/exports?path=${encodeURIComponent(path)}`, { method: 'DELETE' });
  }
  exportUrl(path) {
    const q = new URLSearchParams({ path });
    if (this.token) q.set('token', this.token);
    return `/api/exports/download?${q}`;
  }

  // Every source frame's presentation time, in seconds. The frame map says
  // *which* of a camera's frames belongs to a capture frame; this says *when*
  // that frame is, which is what a <video> is seeked by. Having both is what
  // makes "the picture is the frame the mask was drawn on" exact rather than
  // approximately true — see media.js.
  async timestamps(cid, stream) {
    const res = await this.req(`/api/captures/${cid}/streams/${stream}/timestamps`,
                               { raw: true, quiet: true });
    const buf = await res.arrayBuffer();
    if (!buf.byteLength) return null;
    const us = new BigInt64Array(buf);
    const out = new Float64Array(us.length);
    for (let i = 0; i < us.length; i++) out[i] = Number(us[i]) / 1e6;
    return out;
  }

  async frameMap(cid, stream) {
    const res = await this.req(`/api/captures/${cid}/streams/${stream}/framemap`, { raw: true });
    const buf = await res.arrayBuffer();
    return buf.byteLength ? new Int32Array(buf) : null;
  }

  // `rendition` is which copy of the pixels: the source, or one that
  // `media.prepare` made so a browser could decode it or so six of them at
  // once would not melt the machine. They share a timeline, so switching
  // between them mid-playback is safe.
  mediaUrl(cid, stream, rendition = 'source') {
    const q = new URLSearchParams();
    if (rendition && rendition !== 'source') q.set('r', rendition);
    if (this.token) q.set('token', this.token);
    const s = q.toString();
    return `/api/captures/${cid}/media/${stream}${s ? `?${s}` : ''}`;
  }
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// How often to prove the socket is still there, and how long to wait for the
// proof. A forwarded port does not report its own death: `ssh -L` holds the
// local listener open while the far end is gone, so the tab keeps a socket it
// believes is fine and simply stops hearing about other people's edits. Only a
// ping that goes unanswered can tell the difference — the server has answered
// `{t:"ping"}` with a pong all along, and nothing ever asked.
const PING_MS = 15000;
const PONG_GRACE = 20000;

// One socket per open capture. Reconnects with backoff; a reconnect re-fetches
// the ops it missed rather than assuming it missed none.
export class Socket {
  constructor(api, captureId, handlers = {}) {
    this.api = api; this.captureId = captureId; this.handlers = handlers;
    this.tries = 0; this.closed = false; this.lastOp = 0;
    // Null until the first `hello`. That is what tells a *reconnect* apart from
    // a first connection: on a reconnect there is a watermark to catch up from,
    // and on a first one the capture is being loaded fresh anyway.
    this.seen = null;
    this.state = 'connecting';
    this.connect();
  }

  setState(s, detail = '') {
    if (this.state === s) return;
    this.state = s;
    this.handlers.state?.(s, detail);
  }

  connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const t = this.api.token ? `?token=${encodeURIComponent(this.api.token)}` : '';
    this.setState(this.tries ? 'reconnecting' : 'connecting');
    let ws;
    try {
      ws = this.ws = new WebSocket(`${proto}://${location.host}/ws/capture/${this.captureId || 0}${t}`);
    } catch {
      this.retry();
      return;
    }
    ws.onopen = () => {
      if (ws !== this.ws) return;
      const again = this.everOpen === true;
      this.everOpen = true;
      this.tries = 0;
      this.setState('open');
      this.beat();
      // `again` matters more than it looks. Replaying the ops we missed is not
      // enough on its own: an *undo* during the gap changes an op that is
      // already behind our watermark, and `ops_since` excludes undone ops, so
      // there is no message that would ever tell us. A reconnect therefore has
      // to re-read what it derives, not just catch up on what was appended.
      this.handlers.open?.(again);
    };
    ws.onmessage = (e) => {
      if (ws !== this.ws) return;
      this.lastHeard = Date.now();
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }
      if (msg.t === 'pong') return;
      if (msg.t === 'op') { this.lastOp = Math.max(this.lastOp, msg.op.id); this.seen = this.lastOp; }
      if (msg.t === 'hello') {
        this.lastOp = msg.lastOp;
        this.resync(msg.lastOp);
        return;
      }
      this.handlers[msg.t]?.(msg);
    };
    ws.onclose = () => {
      if (ws !== this.ws || this.closed) return;
      this.retry();
    };
    ws.onerror = () => { /* onclose always follows; nothing useful to add here */ };
  }

  retry() {
    this.stopBeat();
    this.tries++;
    this.setState('reconnecting');
    this.handlers.down?.(this.tries);
    const wait = Math.min(500 * 2 ** this.tries, 8000);
    clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => { if (!this.closed) this.connect(); },
                                 wait * (0.7 + Math.random() * 0.6));
  }

  // Everything that happened while we were not listening. Without this a
  // dropped socket is silent data loss on screen: the ops are in the log and
  // durable, but this tab never hears about them and goes on drawing a capture
  // that is several edits out of date — including its *own* edits, when a job
  // it started finished during the gap.
  async resync(lastOp) {
    const from = this.seen;
    this.seen = lastOp;
    if (from == null) { this.handlers.hello?.({ t: 'hello', lastOp }); return; }
    if (lastOp <= from) { this.handlers.hello?.({ t: 'hello', lastOp }); return; }
    try {
      const { ops } = await this.api.ops(this.captureId, from);
      for (const op of ops || []) this.handlers.op?.({ t: 'op', op });
      this.handlers.resynced?.(ops?.length || 0);
    } catch {
      // The socket is up but the fetch failed. Say so rather than pretending to
      // be current — a stale view that claims to be live is the worse failure.
      this.handlers.stale?.();
    }
    this.handlers.hello?.({ t: 'hello', lastOp });
  }

  // -- liveness -------------------------------------------------------------
  beat() {
    this.stopBeat();
    this.lastHeard = Date.now();
    this.beatTimer = setInterval(() => {
      if (this.closed) return this.stopBeat();
      if (Date.now() - this.lastHeard > PING_MS + PONG_GRACE) {
        // Nothing has come back for long enough that this socket is a fiction.
        // Drop it and let the reconnect path run: `onclose` on a half-open
        // socket may never arrive on its own.
        const dead = this.ws;
        this.ws = null;
        try { dead?.close(); } catch { /* already gone */ }
        this.retry();
        return;
      }
      this.send({ t: 'ping' });
    }, PING_MS);
  }

  stopBeat() { clearInterval(this.beatTimer); this.beatTimer = null; }

  send(msg) { if (this.ws?.readyState === 1) this.ws.send(JSON.stringify(msg)); }

  close() {
    this.closed = true;
    this.stopBeat();
    clearTimeout(this.retryTimer);
    const ws = this.ws;
    this.ws = null;
    try { ws?.close(); } catch { /* already gone */ }
  }
}
