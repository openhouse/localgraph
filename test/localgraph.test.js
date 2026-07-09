import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { initWorkspace, loadConfig, resolveWorkspacePaths } from '../src/config.js';
import { scanInstagramSource } from '../src/instagram.js';
import { renderViews } from '../src/render.js';

test('initWorkspace creates private local directories and config', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'localgraph-'));
  const result = await initWorkspace(root);
  const config = await loadConfig(root);
  const paths = resolveWorkspacePaths(root, config);

  assert.equal(result.workspaceRoot, root);
  assert.equal(config.schemaVersion, 1);
  assert.equal(paths.instagramSourceDir, path.join(root, 'sources/instagram'));
  assert.match(await readFile(path.join(root, 'localgraph.config.json'), 'utf8'), /"schemaVersion": 1/);
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
