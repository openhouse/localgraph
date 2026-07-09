import path from 'node:path';
import { stableViewName } from './slug.js';

const VIEW_KIND_TO_DIR = Object.freeze({
  person: 'people',
  group: 'groups',
  thread: 'threads',
  project: 'projects',
  tag: 'tags'
});

export function viewPath(root, kind, label, sourceKey = label) {
  const viewDir = VIEW_KIND_TO_DIR[kind];
  if (!viewDir) {
    throw new Error(`Unsupported view kind: ${kind}`);
  }

  return path.join(path.resolve(root), 'views', viewDir, stableViewName(label, sourceKey));
}

export function viewKinds() {
  return Object.keys(VIEW_KIND_TO_DIR);
}
