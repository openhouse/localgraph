import path from 'node:path';
import { mkdir } from 'node:fs/promises';

export const SCHEMA_TABLES = Object.freeze([
  'source_imports',
  'identities',
  'accounts',
  'threads',
  'thread_participants',
  'messages',
  'media_objects',
  'annotations',
  'graph_edges'
]);

export const SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS source_imports (
  id INTEGER PRIMARY KEY,
  source_kind TEXT NOT NULL,
  source_identifier TEXT NOT NULL UNIQUE,
  source_path TEXT,
  imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  raw_metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS identities (
  id INTEGER PRIMARY KEY,
  stable_key TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('person', 'group', 'organization', 'unknown')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  identity_id INTEGER REFERENCES identities(id) ON DELETE SET NULL,
  source_kind TEXT NOT NULL,
  account_key TEXT NOT NULL,
  display_name TEXT NOT NULL,
  profile_url TEXT,
  raw_metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(source_kind, account_key)
);

CREATE TABLE IF NOT EXISTS threads (
  id INTEGER PRIMARY KEY,
  source_kind TEXT NOT NULL,
  source_thread_key TEXT NOT NULL,
  title TEXT NOT NULL,
  thread_kind TEXT NOT NULL CHECK (thread_kind IN ('direct', 'group', 'channel', 'unknown')),
  first_message_at TEXT,
  last_message_at TEXT,
  raw_metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(source_kind, source_thread_key)
);

CREATE TABLE IF NOT EXISTS thread_participants (
  thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  identity_id INTEGER REFERENCES identities(id) ON DELETE SET NULL,
  account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
  role TEXT NOT NULL DEFAULT 'participant',
  PRIMARY KEY(thread_id, account_id, role)
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  source_message_key TEXT NOT NULL,
  sender_identity_id INTEGER REFERENCES identities(id) ON DELETE SET NULL,
  sender_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
  sent_at TEXT NOT NULL,
  body_text TEXT,
  body_format TEXT NOT NULL DEFAULT 'plain',
  raw_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(thread_id, source_message_key)
);

CREATE TABLE IF NOT EXISTS media_objects (
  id INTEGER PRIMARY KEY,
  message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
  object_key TEXT NOT NULL UNIQUE,
  source_uri TEXT,
  local_path TEXT,
  mime_type TEXT,
  checksum TEXT,
  raw_metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS annotations (
  id INTEGER PRIMARY KEY,
  target_kind TEXT NOT NULL,
  target_key TEXT NOT NULL,
  body_markdown TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS graph_edges (
  id INTEGER PRIMARY KEY,
  from_kind TEXT NOT NULL,
  from_key TEXT NOT NULL,
  edge_kind TEXT NOT NULL,
  to_kind TEXT NOT NULL,
  to_key TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'derived',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(from_kind, from_key, edge_kind, to_kind, to_key, source)
);
`;

export function databasePath(root) {
  return path.join(path.resolve(root), 'state', 'localgraph.sqlite');
}

export async function initializeDatabase(dbPath) {
  await mkdir(path.dirname(dbPath), { recursive: true, mode: 0o700 });
  const db = await openDatabase(dbPath);
  try {
    db.exec(SCHEMA_SQL);
    return { path: dbPath, tables: listTables(db) };
  } finally {
    db.close();
  }
}

export async function openDatabase(dbPath) {
  const { DatabaseSync } = await import('node:sqlite');
  const db = new DatabaseSync(dbPath);
  db.exec('PRAGMA foreign_keys = ON;');
  db.exec('PRAGMA journal_mode = WAL;');
  return db;
}

export function listTables(db) {
  return db
    .prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    .all()
    .map((row) => row.name);
}

export async function checkSchema(dbPath) {
  const db = await openDatabase(dbPath);
  try {
    const found = new Set(listTables(db));
    const missing = SCHEMA_TABLES.filter((table) => !found.has(table));
    return { ok: missing.length === 0, tables: [...found].sort(), missing };
  } finally {
    db.close();
  }
}
