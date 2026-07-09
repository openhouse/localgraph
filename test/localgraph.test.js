import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { initWorkspace, loadConfig, resolveWorkspacePaths } from '../src/config.js';
import { scanInstagramSource } from '../src/instagram.js';
import { plannedLayout } from '../src/layout.js';
import { renderViews } from '../src/render.js';
import { STATE_SCHEMA_SQL } from '../src/schema.js';
import { slugify, stableHash, stableViewName } from '../src/slug.js';
import { viewPath } from '../src/views.js';

test('initWorkspace creates private local directories and config', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'localgraph-'));
  const result = await initWorkspace(root);
  const config = await loadConfig(root);
  const paths = resolveWorkspacePaths(root, config);

  assert.equal(result.workspaceRoot, root);
  assert.equal(config.schemaVersion, 1);
  assert.equal(paths.instagramSourceDir, path.join(root, 'sources/instagram'));
  assert.match(await readFile(path.join(root, 'localgraph.config.json'), 'utf8'), /"schemaVersion": 1/);
  assert.match(await readFile(path.join(root, 'state/schema.sql'), 'utf8'), /CREATE TABLE IF NOT EXISTS messages/);
  assert.match(await readFile(path.join(root, 'PRIVATE-DATA-README.md'), 'utf8'), /private source exports/);
});

test('plannedLayout exposes private directories and generated view directories', () => {
  const layout = plannedLayout('/tmp/localgraph-example');
  assert.equal(layout.root, '/tmp/localgraph-example');
  assert.equal(layout.privateDirectories.some((dir) => dir.name === 'sources'), true);
  assert.equal(layout.privateDirectories.some((dir) => dir.name === 'exports'), true);
  assert.equal(layout.viewDirectories.some((dir) => dir.kind === 'person'), true);
});

test('state schema includes source, identity, message, annotation, and graph tables', () => {
  for (const table of ['source_imports', 'identities', 'accounts', 'threads', 'messages', 'annotations', 'graph_edges']) {
    assert.match(STATE_SCHEMA_SQL, new RegExp(`CREATE TABLE IF NOT EXISTS ${table}`));
  }
});

test('viewPath creates deterministic symlink-friendly names', () => {
  assert.equal(slugify('Café mañana & 東京'), 'cafe-manana-and');
  assert.match(stableHash('source-key'), /^[a-f0-9]{10}$/);
  assert.equal(stableViewName('Alice Example', 'instagram:alice'), stableViewName('Alice Example', 'instagram:alice'));
  assert.notEqual(stableViewName('Alice Example', 'instagram:alice'), stableViewName('Alice Example', 'instagram:bob'));
  assert.match(viewPath('/archive/localgraph', 'person', 'Alice Example', 'instagram:alice'), /\/archive\/localgraph\/views\/people\/alice-example--[a-f0-9]{8}$/);
  assert.throws(() => viewPath('/archive/localgraph', 'account', 'Alice', 'alice'), /Unsupported view kind/);
});

test('scanInstagramSource discovers Instagram transfer exports without reading bodies', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'localgraph-'));
  await initWorkspace(root);
  const exportRoot = path.join(root, 'sources/instagram/meta-2026/instagram-jamieburkart-2026-07-08-6HfoR9UN');
  const inbox = path.join(exportRoot, 'your_instagram_activity/messages/inbox/alice_123');
  await mkdir(inbox, { recursive: true });
  await writeFile(path.join(inbox, 'message_1.json'), '{"messages":[{"content":"private"}]}');

  const paths = resolveWorkspacePaths(root, await loadConfig(root));
  const scan = await scanInstagramSource(paths);

  assert.equal(scan.exports.length, 1);
  assert.equal(scan.totalMessageFiles, 1);
  assert.equal(scan.exports[0].name, 'instagram-jamieburkart-2026-07-08-6HfoR9UN');
  assert.deepEqual(scan.exports[0].threadFolders, ['your_instagram_activity/messages/inbox/alice_123']);
});

test('renderViews writes a symlink-friendly view skeleton and manifest', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'localgraph-'));
  await initWorkspace(root);
  const paths = resolveWorkspacePaths(root, await loadConfig(root));
  const scan = { sourceKind: 'instagram', sourcePath: paths.instagramSourceDir, exports: [], totalMessageFiles: 0 };
  const result = await renderViews(paths, scan);

  assert.equal(result.exports, 0);
  assert.match(await readFile(path.join(paths.viewsDir, 'README.md'), 'utf8'), /Localgraph Views/);
  const manifest = JSON.parse(await readFile(result.manifestPath, 'utf8'));
  assert.equal(manifest.app, 'localgraph');
  assert.equal(manifest.source.sourceKind, 'instagram');
});
