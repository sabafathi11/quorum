"""SQLite storage. Six tables, no ORM.

The core stores *shapes of work*, never the meaning of the work: an annotation
payload is opaque JSON owned by whichever plugin declared its layer type, and
an op is an opaque record the core appends, orders and broadcasts without
interpreting. That is the whole reason this file is short.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id       INTEGER PRIMARY KEY,
  name     TEXT NOT NULL UNIQUE,
  display  TEXT NOT NULL DEFAULT '',
  role     TEXT NOT NULL DEFAULT 'annotator',   -- annotator | reviewer | admin
  token    TEXT UNIQUE,
  created  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS captures (
  id        INTEGER PRIMARY KEY,
  key       TEXT NOT NULL UNIQUE,               -- stable external id, e.g. a stamp
  name      TEXT NOT NULL,
  provider  TEXT NOT NULL,                      -- plugin id that made it
  provider_kind TEXT NOT NULL DEFAULT '',       -- which of that plugin's providers
  state     TEXT NOT NULL DEFAULT 'ready',      -- draft | ready
  config    TEXT NOT NULL DEFAULT '{}',         -- provider's own record, opaque
  layout    TEXT NOT NULL DEFAULT '{}',         -- viewport hint: cells, scene size
  n_frames  INTEGER NOT NULL DEFAULT 0,
  fps       REAL NOT NULL DEFAULT 0,
  created   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS streams (
  id         INTEGER PRIMARY KEY,
  capture_id INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
  key        TEXT NOT NULL,                     -- 'cam1'
  idx        INTEGER NOT NULL DEFAULT 0,
  name       TEXT NOT NULL DEFAULT '',
  width      INTEGER NOT NULL DEFAULT 0,
  height     INTEGER NOT NULL DEFAULT 0,
  n_frames   INTEGER NOT NULL DEFAULT 0,
  media      TEXT NOT NULL DEFAULT '{}',        -- {path, kind, codec, playable, renditions}
  meta       TEXT NOT NULL DEFAULT '{}',
  enabled    INTEGER NOT NULL DEFAULT 1,        -- shown in the layout at all
  duration   REAL NOT NULL DEFAULT 0,           -- seconds, from the media
  time_offset REAL NOT NULL DEFAULT 0,          -- this stream's clock error, seconds
  timestamps BLOB,                              -- int64 LE µs, one per stream frame
  frame_map  BLOB,                              -- int32 LE, capture frame -> stream frame
                                                -- DERIVED from timestamps+offset; a cache
  UNIQUE(capture_id, key)
);

CREATE TABLE IF NOT EXISTS layers (
  id         INTEGER PRIMARY KEY,
  capture_id INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
  key        TEXT NOT NULL,
  name       TEXT NOT NULL,
  type       TEXT NOT NULL,                     -- layer-type id from a plugin
  provenance TEXT NOT NULL DEFAULT 'human',     -- human | model | derived
  config     TEXT NOT NULL DEFAULT '{}',
  created    REAL NOT NULL,
  UNIQUE(capture_id, key)
);

CREATE TABLE IF NOT EXISTS objects (
  id        INTEGER PRIMARY KEY,
  layer_id  INTEGER NOT NULL REFERENCES layers(id) ON DELETE CASCADE,
  stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
  key       TEXT NOT NULL,                      -- unique within the layer+stream
  label     TEXT NOT NULL DEFAULT '',
  first_frame INTEGER NOT NULL DEFAULT 0,       -- in *stream* frames
  last_frame  INTEGER NOT NULL DEFAULT 0,
  meta      TEXT NOT NULL DEFAULT '{}',
  UNIQUE(layer_id, stream_id, key)
);

CREATE TABLE IF NOT EXISTS shapes (
  object_id INTEGER NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
  frame     INTEGER NOT NULL,                   -- stream frame
  outside   INTEGER NOT NULL DEFAULT 0,
  payload   TEXT NOT NULL,                      -- opaque to the core
  PRIMARY KEY (object_id, frame)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ops (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  capture_id INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
  layer_id   INTEGER,
  actor      TEXT NOT NULL,
  ts         REAL NOT NULL,
  kind       TEXT NOT NULL,                     -- 'identity.merge', plugin-namespaced
  payload    TEXT NOT NULL DEFAULT '{}',
  undone     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS docs (
  capture_id INTEGER NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
  ns         TEXT NOT NULL,                     -- plugin id
  key        TEXT NOT NULL,
  version    INTEGER NOT NULL DEFAULT 1,
  value      TEXT NOT NULL DEFAULT '{}',
  updated    REAL NOT NULL,
  PRIMARY KEY (capture_id, ns, key)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS jobs (
  id         TEXT PRIMARY KEY,
  capture_id INTEGER,
  plugin     TEXT NOT NULL,
  kind       TEXT NOT NULL,
  params     TEXT NOT NULL DEFAULT '{}',
  state      TEXT NOT NULL DEFAULT 'queued',    -- queued|running|done|failed|cancelled
  progress   REAL NOT NULL DEFAULT 0,
  message    TEXT NOT NULL DEFAULT '',
  result     TEXT NOT NULL DEFAULT '{}',
  actor      TEXT NOT NULL DEFAULT '',
  created    REAL NOT NULL,
  updated    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
  id           INTEGER PRIMARY KEY,
  capture_id   INTEGER REFERENCES captures(id) ON DELETE CASCADE,   -- NULL = library
  kind         TEXT NOT NULL DEFAULT '',        -- a plugin-declared AssetKind id, '' = unsorted
  name         TEXT NOT NULL,                   -- the name it was uploaded under
  path         TEXT NOT NULL,                   -- inside cfg.upload_dir; never user-supplied
  size         INTEGER NOT NULL DEFAULT 0,      -- what the client promised
  received     INTEGER NOT NULL DEFAULT 0,      -- what is actually on disk (resumable)
  sha256       TEXT NOT NULL DEFAULT '',
  content_type TEXT NOT NULL DEFAULT '',
  state        TEXT NOT NULL DEFAULT 'uploading',  -- uploading | ready | failed
  meta         TEXT NOT NULL DEFAULT '{}',      -- whatever the owning plugin probed out of it
  actor        TEXT NOT NULL DEFAULT '',
  created      REAL NOT NULL,
  updated      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_assets_capture ON assets(capture_id, kind);
CREATE INDEX IF NOT EXISTS ix_shapes_frame  ON shapes(object_id, frame);
CREATE INDEX IF NOT EXISTS ix_objects_layer ON objects(layer_id, stream_id);
CREATE INDEX IF NOT EXISTS ix_ops_capture   ON ops(capture_id, id);
CREATE INDEX IF NOT EXISTS ix_jobs_capture  ON jobs(capture_id, created);
"""


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` does
# nothing to a table that already exists, and the database holding this repo's
# real capture is 387 MB — recreating it is not an option, so every schema
# change since must also appear here as an ALTER that is safe to re-run.
ADDED_COLUMNS = [
    ("captures", "provider_kind", "TEXT NOT NULL DEFAULT ''"),
    ("captures", "state", "TEXT NOT NULL DEFAULT 'ready'"),
    ("streams", "enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("streams", "duration", "REAL NOT NULL DEFAULT 0"),
    ("streams", "time_offset", "REAL NOT NULL DEFAULT 0"),
    ("streams", "timestamps", "BLOB"),
]


def migrate(c: sqlite3.Connection) -> None:
    for table, col, decl in ADDED_COLUMNS:
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if not have:                    # table does not exist yet; SCHEMA made it right
            continue
        if col not in have:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    c.commit()


class Database:
    """Thread-local connections over one file. WAL, so readers never block."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self.conn() as c:
            c.executescript(SCHEMA)
            migrate(c)

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    # -- reads ---------------------------------------------------------------
    def all(self, sql: str, *args) -> list[sqlite3.Row]:
        return list(self.conn().execute(sql, args))

    def one(self, sql: str, *args) -> sqlite3.Row | None:
        cur = self.conn().execute(sql, args)
        return cur.fetchone()

    def scalar(self, sql: str, *args):
        r = self.one(sql, *args)
        return None if r is None else r[0]

    # -- writes --------------------------------------------------------------
    def run(self, sql: str, *args) -> sqlite3.Cursor:
        with self._write_lock:
            c = self.conn()
            cur = c.execute(sql, args)
            c.commit()
            return cur

    def insert(self, table: str, **cols) -> int:
        keys = ",".join(cols)
        marks = ",".join("?" * len(cols))
        cur = self.run(f"INSERT INTO {table} ({keys}) VALUES ({marks})", *cols.values())
        return int(cur.lastrowid)

    def many(self, sql: str, rows) -> None:
        with self._write_lock:
            c = self.conn()
            c.executemany(sql, rows)
            c.commit()

    def tx(self):
        """`with db.tx() as c:` — one exclusive write transaction."""
        return _Tx(self)


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self) -> sqlite3.Connection:
        self.db._write_lock.acquire()
        self.c = self.db.conn()
        self.c.execute("BEGIN")
        return self.c

    def __exit__(self, exc, *_):
        try:
            self.c.rollback() if exc else self.c.commit()
        finally:
            self.db._write_lock.release()
        return False


def row_to_dict(r: sqlite3.Row | None, json_cols=()) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in json_cols:
        if k in d and isinstance(d[k], str):
            d[k] = json.loads(d[k] or "{}")
    for binary in ("frame_map", "timestamps"):
        d.pop(binary, None)      # served on their own endpoints, never inside JSON
    return d


def now() -> float:
    return time.time()
