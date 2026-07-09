import path from 'node:path';
import { viewDirectoryForKind, viewKinds } from './views.js';

export const PRIVATE_DIRECTORIES = Object.freeze([
  'sources',
  'state',
  'objects',
  'views',
  'annotations',
  'exports'
]);

export function plannedLayout(root = process.cwd()) {
  const absoluteRoot = path.resolve(root);
  return {
    root: absoluteRoot,
    configPath: path.join(absoluteRoot, 'localgraph.config.json'),
    privateDirectories: PRIVATE_DIRECTORIES.map((name) => ({ name, path: path.join(absoluteRoot, name) })),
    viewDirectories: viewKinds().map((kind) => ({ kind, path: path.join(absoluteRoot, 'views', viewDirectoryForKind(kind)) }))
  };
}

export function renderPlan(root = process.cwd()) {
  const layout = plannedLayout(root);
  return `Localgraph root: ${layout.root}

Private local directories:
${layout.privateDirectories.map((dir) => `  - ${dir.name}/`).join('\n')}

Generated view directories:
${layout.viewDirectories.map((dir) => `  - ${dir.path.replace(`${layout.root}/`, '')}/`).join('\n')}

Config:
  - ${path.basename(layout.configPath)}
`;
}
