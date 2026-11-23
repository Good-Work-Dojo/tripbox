
CREATE TABLE IF NOT EXISTS schema_version(v INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);
-- messages: one row per email or paste read. status is archived while it is read (or when a
-- reading stopped half-way), waiting when nothing read it and only the model can (error: why,
-- pipeline.run.WAIT_REASONS; so too while it is read again), else processed, ignored, rejected
-- or failed. found_at is when its
-- bookings first reached the ledger, which every reading of it dates its findings by.
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY,
  message_id TEXT,
  sha256 TEXT UNIQUE NOT NULL,
  source TEXT NOT NULL,
  mail_uid INTEGER,
  received_at TEXT,
  email_date TEXT,
  from_addr TEXT,
  subject TEXT,
  origin_ok INTEGER NOT NULL DEFAULT 0,
  archive_path TEXT,
  status TEXT NOT NULL,
  document_type TEXT,
  parser TEXT,
  error TEXT,
  answer_json TEXT,
  found_at TEXT,
  processed_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_mid ON messages(message_id);
-- items is the log of what each message did: one row per booking it read (n: its place in the
-- message, from 0), written once. outcome is what became of the booking, detail the word beside
-- updated or ignored or a failure's sentence, item_key the booking's key once the message was
-- filed, payload_json what the message said, trip_item_id the booking. Reading a message again
-- deletes its rows and writes them anew.
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id),
  n INTEGER NOT NULL,
  kind TEXT NOT NULL,
  item_key TEXT NOT NULL,
  outcome TEXT NOT NULL
    CHECK (outcome IN ('filed', 'updated', 'duplicate', 'ignored', 'failed')),
  detail TEXT,
  payload_json TEXT NOT NULL,
  confidence REAL,
  trip_item_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_key ON items(item_key);
CREATE INDEX IF NOT EXISTS items_message ON items(message_id);
CREATE INDEX IF NOT EXISTS items_trip_item ON items(trip_item_id);
CREATE TABLE IF NOT EXISTS ref_cache(
  kind TEXT NOT NULL, code TEXT NOT NULL, json TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY(kind, code)
);
CREATE TABLE IF NOT EXISTS llm_calls(
  id INTEGER PRIMARY KEY, message_id INTEGER, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER,
  cost_usd REAL, created_at TEXT NOT NULL
);
-- aeroapi_calls.item_id and enrichment.item_id hold a trip item's id (trip_items.id): the
-- flight that enrichment looked up.
CREATE TABLE IF NOT EXISTS aeroapi_calls(
  id INTEGER PRIMARY KEY, month TEXT NOT NULL, item_id INTEGER, ident TEXT, result_sets INTEGER,
  est_cost_usd REAL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enrichment(
  item_id INTEGER PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0, last_attempt TEXT,
  enriched_at TEXT, fields_set_json TEXT
);
-- trips and trip_items hold the one current state of each booking, one row per booking
-- (source_message_id: the last message that filed or changed it; NULL for one added by hand or
-- from AirTrail). Maintained only by trips.store.TripStore (the executor and the web repairs go
-- through it). Their ids are never given twice (AUTOINCREMENT): a Telegram button and a page
-- name a booking or a trip by its id, and a row goes when a draft becomes one with its booking
-- and when Recently deleted is purged. A migration that rebuilds either table carries its row
-- of sqlite_sequence over to the new table, or the next row takes an id given before.
CREATE TABLE IF NOT EXISTS trips(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  place TEXT,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  notes TEXT,
  deleted_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trips_dates ON trips(start_date, end_date);
CREATE TABLE IF NOT EXISTS trip_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trip_id INTEGER REFERENCES trips(id),
  item_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  start_at TEXT,
  end_at TEXT,
  start_tz TEXT,
  end_tz TEXT,
  confirmation TEXT,
  payload_json TEXT NOT NULL,
  passengers_json TEXT NOT NULL,
  checks_json TEXT NOT NULL DEFAULT '[]',
  sync_state TEXT NOT NULL DEFAULT 'off'
    CHECK (sync_state IN ('off', 'blocked', 'pending', 'synced')),
  sync_hold TEXT CHECK (sync_hold IN ('past', 'deleted')),
  airtrail_flight_id INTEGER,
  told_json TEXT,
  sync_error TEXT CHECK (sync_error IN ('outage', 'refused', 'airport')),
  sync_error_at TEXT,
  sync_attempts INTEGER NOT NULL DEFAULT 0,
  sync_after TEXT,
  sync_claimed_at TEXT,
  sync_logged TEXT,
  source_message_id INTEGER REFERENCES messages(id),
  removed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK ((trip_id IS NULL) = (start_at IS NULL))
);
CREATE INDEX IF NOT EXISTS trip_items_trip ON trip_items(trip_id);
CREATE INDEX IF NOT EXISTS trip_items_sync ON trip_items(sync_state);
-- uploads is the waiting list of what the page was handed to read (a paste, an email file, a
-- PDF): the page adds a row, the worker claims the oldest waiting one, reads it through the
-- pipeline and finishes it, which drops its body. token names it outside the server (its id
-- never leaves it); sha256 makes the same bytes one upload; name is shown, never opened;
-- message_id is the message it became; attempts counts the times a worker took it to read.
CREATE TABLE IF NOT EXISTS uploads(
  id INTEGER PRIMARY KEY,
  token TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('text', 'eml', 'pdf')),
  name TEXT,
  sha256 TEXT NOT NULL UNIQUE,
  size INTEGER NOT NULL,
  body BLOB,
  submitted_by TEXT,
  state TEXT NOT NULL DEFAULT 'waiting'
    CHECK (state IN ('waiting', 'processing', 'filed', 'nothing', 'failed')),
  message_id INTEGER REFERENCES messages(id),
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- sessions: who is signed in to the page with its password. A session is a random token in the
-- browser's cookie, of which only its SHA-256 is kept here; cred is a fingerprint of the
-- password's hash it was made under (a changed password ends it); seen_at is rewritten at most
-- once an hour. devices: the browsers that have signed in before, by the SHA-256 of their
-- cookie's id, each with a sign-in throttle of its own. Each keeps its 50 newest by seen_at.
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY,
  cred TEXT NOT NULL,
  created_at TEXT NOT NULL,
  seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices(
  id_hash TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  seen_at TEXT NOT NULL
);
-- share_links: the links that show one trip, read-only, to anyone who holds one, without
-- signing in. token is kept so the owner can copy the link again; a link is found by
-- token_hash (its SHA-256) alone. shows: the fields it shows (trips.shares.FIELDS), as JSON.
-- A trip that goes for good takes its links with it (a merge empties and deletes its source).
CREATE TABLE IF NOT EXISTS share_links(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  token TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  shows TEXT NOT NULL,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS share_links_trip ON share_links(trip_id);
