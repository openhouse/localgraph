import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { mkdtemp, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const cli = path.resolve('src/cli.js');

test('plan emits JSON layout', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'localgraph-cli-'));
  const { stdout } = await execFileAsync(process.execPath, [cli, 'plan', root, '--json']);
  const payload = JSON.parse(stdout);

  assert.equal(payload.root, root);
  assert.equal(payload.privateDirectories.some((dir) => dir.name === 'sources'), true);
});

test('init creates a localgraph root from the CLI', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'localgraph-cli-'));
  const { stdout } = await execFileAsync(process.execPath, [cli, 'init', root, '--json']);
  const payload = JSON.parse(stdout);

  assert.equal(payload.root, root);
  assert.equal((await stat(path.join(root, 'views', 'people'))).isDirectory(), true);
});

test('view-name prints a deterministic view path', async () => {
  const { stdout } = await execFileAsync(process.execPath, [
    cli,
    'view-name',
    'group',
    'Residency Planning',
    'instagram:thread:456',
    '/archive/localgraph'
  ]);

  assert.match(stdout.trim(), /\/archive\/localgraph\/views\/groups\/residency-planning--[a-f0-9]{8}$/);
});
