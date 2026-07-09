import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { mkdir, readFile, stat, writeFile } from 'node:fs/promises';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { initializeRoot } from '../src/layout.js';
import { databasePath, initializeDatabase, checkSchema, SCHEMA_TABLES } from '../src/schema.js';
import { scanInstagramSource } from '../src/instagram.js';
import { renderSourceManifest } from '../src/render.js';

test('schema initializes the canonical Localgraph tables', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'localgraph-composite-'));
  await initializeRoot(root);
  await initializeDatabase(databasePath(root));
  const schema = await checkSchema(databasePath(root));

  assert.equal(schema.ok, true);
  for (const table of SCHEMA_TABLES) assert.equal(schema.tables.includes(table), true);
});

test('Instagram scanner detects transfer exports without reading message bodies', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'localgraph-composite-'));
  const exportRoot = path.join(root, 'sources/instagram/meta-2026/instagram-jamieburkart-2026-07-08-6HfoR9UN');
  const inbox = path.join(exportRoot, 'your_instagram_activity/messages/inbox/alice_123');
  await mkdir(inbox, { recursive: true });
  await writeFile(path.join(inbox, 'message_1.json'), '{"messages":[{"content":"private"}]}');

  const scan = await scanInstagramSource(root);

  assert.equal(scan.sourceKind, 'instagram');
  assert.equal(scan.exports.length, 1);
  assert.equal(scan.exports[0].name, 'instagram-jamieburkart-2026-07-08-6HfoR9UN');
  assert.deepEqual(scan.exports[0].threadFolders, ['your_instagram_activity/messages/inbox/alice_123']);
});

test('renderSourceManifest writes symlink-friendly view directories and manifest', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'localgraph-composite-'));
  await initializeRoot(root);
  const scan = { sourceKind: 'instagram', sourcePath: path.join(root, 'sources/instagram'), exports: [], totalMessageFiles: 0 };
  const result = await renderSourceManifest(root, scan);

  assert.equal((await stat(path.join(root, 'views', 'people'))).isDirectory(), true);
  assert.match(await readFile(path.join(root, 'views', 'README.md'), 'utf8'), /Localgraph Views/);
  const manifest = JSON.parse(await readFile(result.manifestPath, 'utf8'));
  assert.equal(manifest.app, 'localgraph');
  assert.equal(manifest.source.sourceKind, 'instagram');
});
