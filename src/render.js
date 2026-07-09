import path from 'node:path';
import { mkdir, writeFile } from 'node:fs/promises';
import { VIEW_DIRECTORIES } from './layout.js';

export async function renderSourceManifest(root, scan) {
  const viewsDir = path.join(path.resolve(root), 'views');
  const systemDir = path.join(viewsDir, '_system');
  for (const dir of [...VIEW_DIRECTORIES, '_system']) {
    await mkdir(path.join(viewsDir, dir), { recursive: true, mode: 0o700 });
  }

  const manifest = {
    app: 'localgraph',
    schemaVersion: 1,
    generatedAt: new Date().toISOString(),
    source: scan
  };
  const manifestPath = path.join(systemDir, 'source-manifest.json');

  await writeFile(path.join(viewsDir, 'README.md'), renderReadme(scan));
  await writeFile(path.join(systemDir, 'README.md'), '# Localgraph system views\n\nGenerated manifests and diagnostics live here.\n');
  await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);

  return {
    viewsDir,
    manifestPath,
    exports: scan.exports.length,
    messageFiles: scan.totalMessageFiles
  };
}

function renderReadme(scan) {
  return `# Localgraph Views

These directories are generated projections over private local source data.

- Source kind: ${scan.sourceKind}
- Source path: ${scan.sourcePath}
- Exports discovered: ${scan.exports.length}
- Message files discovered: ${scan.totalMessageFiles}

Use the person, group, thread, project, and tag directories as stable symlink
targets from other local project folders.
`;
}
