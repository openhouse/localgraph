import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { mkdtemp, readFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { initializeRoot, plannedLayout, PRIVATE_DIRECTORIES, VIEW_DIRECTORIES } from '../src/layout.js';

test('plannedLayout resolves private and view directories', () => {
  const layout = plannedLayout('/tmp/localgraph-example');
  assert.equal(layout.root, '/tmp/localgraph-example');
  assert.deepEqual(layout.privateDirectories.map((dir) => dir.name), PRIVATE_DIRECTORIES);
  assert.deepEqual(layout.viewDirectories.map((dir) => dir.name), VIEW_DIRECTORIES);
});

test('initializeRoot creates private directories, view directories, and config', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'localgraph-'));
  const result = await initializeRoot(root);

  for (const name of PRIVATE_DIRECTORIES) {
    assert.equal((await stat(path.join(root, name))).isDirectory(), true);
  }
  for (const name of VIEW_DIRECTORIES) {
    assert.equal((await stat(path.join(root, 'views', name))).isDirectory(), true);
  }

  const config = JSON.parse(await readFile(path.join(root, 'localgraph.config.json'), 'utf8'));
  assert.equal(config.formatVersion, 1);
  assert.equal(config.root, result.root);
});

test('initializeRoot preserves an existing config unless forced', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'localgraph-'));
  await initializeRoot(root);
  const first = await readFile(path.join(root, 'localgraph.config.json'), 'utf8');
  const second = await initializeRoot(root);

  assert.equal(second.existing.includes('sources'), true);
  assert.equal(second.existing.includes('views/people'), true);
  assert.equal(second.existing.includes('localgraph.config.json'), true);
  assert.equal(await readFile(path.join(root, 'localgraph.config.json'), 'utf8'), first);
});
