import path from 'node:path';
import { readdir, stat } from 'node:fs/promises';

const SKIP_DIRECTORIES = new Set(['.git', 'node_modules', '.DS_Store']);

export async function pathExists(filePath) {
  try {
    await stat(filePath);
    return true;
  } catch (error) {
    if (error?.code === 'ENOENT') return false;
    throw error;
  }
}

export async function directoryExists(dirPath) {
  try {
    return (await stat(dirPath)).isDirectory();
  } catch (error) {
    if (error?.code === 'ENOENT') return false;
    throw error;
  }
}

export async function walkFiles(root, predicate = () => true) {
  if (!await directoryExists(root)) return [];
  const out = [];
  await walk(root, out, predicate);
  return out.sort();
}

async function walk(current, out, predicate) {
  const entries = await readdir(current, { withFileTypes: true });
  for (const entry of entries) {
    if (SKIP_DIRECTORIES.has(entry.name)) continue;
    const next = path.join(current, entry.name);
    if (entry.isDirectory()) await walk(next, out, predicate);
    else if (entry.isFile() && predicate(next)) out.push(next);
  }
}

export function toPosix(filePath) {
  return filePath.split(path.sep).join('/');
}
