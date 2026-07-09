import path from 'node:path';
import { mkdir, writeFile } from 'node:fs/promises';
import { viewDirectoryForKind, viewKinds } from './views.js';

const VIEW_DIRS = [...viewKinds().map((kind) => viewDirectoryForKind(kind)), '_system'];

export async function renderViews(paths, scan) {
  for (const dir of VIEW_DIRS) await mkdir(path.join(paths.viewsDir, dir), { recursive: true, mode: 0o700 });

  const manifest = {
    app: 'localgraph',
    schemaVersion: 1,
    generatedAt: new Date().toISOString(),
    source: scan
  };
  const manifestPath = path.join(paths.viewsDir, '_system', 'source-manifest.json');
  await writeFile(path.join(paths.viewsDir, 'README.md'), renderReadme(scan));
  await writeFile(path.join(paths.viewsDir, '_system', 'README.md'), '# Localgraph system views\n\nGenerated manifests and diagnostics live here.\n');
  await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);

  return {
    viewsDir: paths.viewsDir,
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
