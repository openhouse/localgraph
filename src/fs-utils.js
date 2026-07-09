import path from 'node:path';
import { readdir, stat } from 'node:fs/promises';

const SKIP_DIRS = new Set(['.git', 'node_modules', '.DS_Store']);

export async function pathExists(filePath) {
  try {
    await stat(filePath);
    return true;
  } catch {
    return false;
  }
}

export async function walkFiles(root, predicate = () => true) {
  const out = [];
  if (!await pathExists(root)) return out;
  await walk(root, out, predicate);
  return out.sort();
}

async function walk(current, out, predicate) {
  const entries = await readdir(current, { withFileTypes: true });
  for (const entry of entries) {
    if (SKIP_DIRS.has(entry.name)) continue;
    const next = path.join(current, entry.name);
    if (entry.isDirectory()) {
      await walk(next, out, predicate);
    } else if (entry.isFile() && predicate(next)) {
      out.push(next);
    }
  }
}

export function toPosix(filePath) {
  return filePath.split(path.sep).join('/');
}
