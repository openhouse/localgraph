import path from 'node:path';
import { mkdir, readFile, stat, writeFile } from 'node:fs/promises';

export const PRIVATE_DIRECTORIES = Object.freeze([
  'sources',
  'state',
  'objects',
  'views',
  'annotations',
  'exports'
]);

export const VIEW_DIRECTORIES = Object.freeze([
  'people',
  'groups',
  'threads',
  'projects',
  'tags'
]);

export function resolveRoot(root = process.cwd()) {
  return path.resolve(root);
}

export function plannedLayout(root = process.cwd()) {
  const absoluteRoot = resolveRoot(root);
  const privateDirectories = PRIVATE_DIRECTORIES.map((name) => ({
    name,
    path: path.join(absoluteRoot, name)
  }));
  const viewDirectories = VIEW_DIRECTORIES.map((name) => ({
    name,
    path: path.join(absoluteRoot, 'views', name)
  }));

  return {
    root: absoluteRoot,
    configPath: path.join(absoluteRoot, 'localgraph.config.json'),
    privateDirectories,
    viewDirectories
  };
}

export function defaultConfig(root = process.cwd()) {
  return {
    formatVersion: 1,
    root: resolveRoot(root),
    directories: {
      sources: 'sources',
      state: 'state',
      objects: 'objects',
      views: 'views',
      annotations: 'annotations',
      exports: 'exports'
    },
    views: {
      people: 'views/people',
      groups: 'views/groups',
      threads: 'views/threads',
      projects: 'views/projects',
      tags: 'views/tags'
    }
  };
}

export async function initializeRoot(root = process.cwd(), options = {}) {
  const layout = plannedLayout(root);
  const created = [];
  const existing = [];

  if (options.dryRun) return { ...layout, created, existing, dryRun: true };

  await mkdir(layout.root, { recursive: true });
  for (const dir of layout.privateDirectories) {
    if (await directoryExists(dir.path)) existing.push(dir.name);
    else {
      await mkdir(dir.path, { recursive: true });
      created.push(dir.name);
    }
  }
  for (const dir of layout.viewDirectories) {
    if (await directoryExists(dir.path)) existing.push(`views/${dir.name}`);
    else {
      await mkdir(dir.path, { recursive: true });
      created.push(`views/${dir.name}`);
    }
  }

  const configText = `${JSON.stringify(defaultConfig(layout.root), null, 2)}\n`;
  try {
    await readFile(layout.configPath, 'utf8');
    existing.push('localgraph.config.json');
    if (options.force) await writeFile(layout.configPath, configText, { mode: 0o600 });
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
    await writeFile(layout.configPath, configText, { mode: 0o600 });
    created.push('localgraph.config.json');
  }

  return { ...layout, created, existing, dryRun: false };
}

async function directoryExists(dirPath) {
  try {
    return (await stat(dirPath)).isDirectory();
  } catch (error) {
    if (error?.code === 'ENOENT') return false;
    throw error;
  }
}

export function renderPlan(root = process.cwd()) {
  const layout = plannedLayout(root);
  const lines = [
    `Localgraph root: ${layout.root}`,
    '',
    'Private local directories:',
    ...layout.privateDirectories.map((dir) => `  - ${dir.name}/`),
    '',
    'Generated view directories:',
    ...layout.viewDirectories.map((dir) => `  - views/${dir.name}/`),
    '',
    'Config:',
    `  - ${path.basename(layout.configPath)}`
  ];
  return `${lines.join('\n')}\n`;
}
