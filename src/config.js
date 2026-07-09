import path from 'node:path';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { STATE_SCHEMA_SQL } from './schema.js';

export const CONFIG_FILE = 'localgraph.config.json';
export const PRIVATE_DATA_README = 'PRIVATE-DATA-README.md';

export function defaultConfig() {
  return {
    schemaVersion: 1,
    paths: {
      sources: './sources',
      state: './state',
      objects: './objects',
      views: './views',
      annotations: './annotations',
      exports: './exports'
    },
    imports: {
      instagram: {
        localPath: './sources/instagram',
        googleDriveFolderId: null
      }
    }
  };
}

export async function loadConfig(root) {
  const configPath = path.join(root, CONFIG_FILE);
  try {
    return JSON.parse(await readFile(configPath, 'utf8'));
  } catch (error) {
    if (error?.code === 'ENOENT') return defaultConfig();
    throw error;
  }
}

export function resolveWorkspacePaths(root, config = defaultConfig()) {
  const resolve = (value) => path.resolve(root, value);
  return {
    root: path.resolve(root),
    configPath: path.join(path.resolve(root), CONFIG_FILE),
    sourcesDir: resolve(config.paths.sources),
    stateDir: resolve(config.paths.state),
    objectsDir: resolve(config.paths.objects),
    viewsDir: resolve(config.paths.views),
    annotationsDir: resolve(config.paths.annotations),
    exportsDir: resolve(config.paths.exports ?? './exports'),
    instagramSourceDir: resolve(config.imports.instagram.localPath),
    privateDirs: {
      sources: resolve(config.paths.sources),
      state: resolve(config.paths.state),
      objects: resolve(config.paths.objects),
      views: resolve(config.paths.views),
      annotations: resolve(config.paths.annotations),
      exports: resolve(config.paths.exports ?? './exports')
    }
  };
}

export async function initWorkspace(root) {
  const config = defaultConfig();
  const paths = resolveWorkspacePaths(root, config);
  const created = [];

  for (const dir of Object.values(paths.privateDirs)) {
    await mkdir(dir, { recursive: true, mode: 0o700 });
    created.push(dir);
  }
  await mkdir(paths.instagramSourceDir, { recursive: true, mode: 0o700 });
  await writeFile(path.join(paths.stateDir, 'schema.sql'), STATE_SCHEMA_SQL, { mode: 0o600 });
  await writeFile(path.join(paths.root, PRIVATE_DATA_README), privateDataReadme(), { mode: 0o600 });
  await writeFile(paths.configPath, `${JSON.stringify(config, null, 2)}\n`, { mode: 0o600 });

  return { workspaceRoot: paths.root, configPath: paths.configPath, schemaPath: path.join(paths.stateDir, 'schema.sql'), created };
}

function privateDataReadme() {
  return `# Local Private Data

This Localgraph workspace may contain private source exports, messages, media,
indexes, generated views, and annotations. These paths are intentionally ignored
by git.

Commit project code and public design notes. Keep correspondence data local.
`;
}
