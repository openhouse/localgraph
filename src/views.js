import path from 'node:path';
import { stableViewName } from './slug.js';

export const VIEW_KIND_TO_DIR = Object.freeze({
  person: 'people',
  group: 'groups',
  thread: 'threads',
  project: 'projects',
  tag: 'tags'
});

export function viewKinds() {
  return Object.keys(VIEW_KIND_TO_DIR);
}

export function viewDirectoryForKind(kind) {
  const viewDir = VIEW_KIND_TO_DIR[kind];
  if (!viewDir) throw new Error(`Unsupported view kind: ${kind}`);
  return viewDir;
}

export function viewPath(root, kind, label, sourceKey = label) {
  const viewDir = viewDirectoryForKind(kind);
  return path.join(path.resolve(root), 'views', viewDir, stableViewName(label, sourceKey));
}
