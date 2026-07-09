import path from 'node:path';
import { stat } from 'node:fs/promises';
import { toPosix, walkFiles } from './fs-utils.js';

const MESSAGE_FILE = /(^|\/)message_\d+\.json$/;

export async function scanInstagramSource(root, sourcePath) {
  const workspaceRoot = path.resolve(root);
  const resolvedSource = path.resolve(workspaceRoot, sourcePath ?? 'sources/instagram');
  const messageFiles = await walkFiles(resolvedSource, (file) => MESSAGE_FILE.test(toPosix(file)));
  const exports = new Map();

  for (const file of messageFiles) {
    const exportRoot = detectExportRoot(resolvedSource, file);
    const rel = toPosix(path.relative(exportRoot, file));
    const threadFolder = toPosix(path.dirname(rel));
    const item = exports.get(exportRoot) ?? {
      name: path.basename(exportRoot),
      path: exportRoot,
      relativePath: toPosix(path.relative(resolvedSource, exportRoot)) || '.',
      messageFiles: 0,
      threadFolders: new Set(),
      latestModifiedTime: null
    };
    item.messageFiles += 1;
    item.threadFolders.add(threadFolder);
    const modified = (await stat(file)).mtime.toISOString();
    item.latestModifiedTime = item.latestModifiedTime && item.latestModifiedTime > modified
      ? item.latestModifiedTime
      : modified;
    exports.set(exportRoot, item);
  }

  return {
    sourceKind: 'instagram',
    sourcePath: resolvedSource,
    exports: [...exports.values()]
      .map((item) => ({ ...item, threadFolders: [...item.threadFolders].sort() }))
      .sort((a, b) => a.relativePath.localeCompare(b.relativePath)),
    totalMessageFiles: messageFiles.length
  };
}

export function detectExportRoot(sourcePath, file) {
  const relParts = path.relative(sourcePath, file).split(path.sep);
  const activityIndex = relParts.indexOf('your_instagram_activity');
  if (activityIndex > 0) return path.join(sourcePath, ...relParts.slice(0, activityIndex));

  const messagesIndex = relParts.indexOf('messages');
  if (messagesIndex > 0) return path.join(sourcePath, ...relParts.slice(0, messagesIndex));

  return sourcePath;
}
