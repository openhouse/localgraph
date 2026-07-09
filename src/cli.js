#!/usr/bin/env node
import process from 'node:process';
import { initializeRoot, plannedLayout, renderPlan } from './layout.js';
import { viewKinds, viewPath } from './views.js';

const COMMANDS = new Set(['help', 'init', 'plan', 'view-name']);

function parseArgs(argv) {
  const [command = 'help', ...rest] = argv;
  const options = { json: false, force: false, dryRun: false };
  const positional = [];

  for (const arg of rest) {
    if (arg === '--json') options.json = true;
    else if (arg === '--force') options.force = true;
    else if (arg === '--dry-run') options.dryRun = true;
    else positional.push(arg);
  }

  return { command, positional, options };
}

function help() {
  return `localgraph

Usage:
  localgraph plan [root] [--json]
  localgraph init [root] [--json] [--force] [--dry-run]
  localgraph view-name <kind> <label> <source-key> [root] [--json]

View kinds:
  ${viewKinds().join(', ')}
`;
}

async function main(argv = process.argv.slice(2)) {
  const { command, positional, options } = parseArgs(argv);
  if (!COMMANDS.has(command)) throw new Error(`Unknown command: ${command}`);

  if (command === 'help') {
    process.stdout.write(help());
    return;
  }

  if (command === 'plan') {
    const root = positional[0] ?? process.cwd();
    const payload = plannedLayout(root);
    process.stdout.write(options.json ? `${JSON.stringify(payload, null, 2)}\n` : renderPlan(root));
    return;
  }

  if (command === 'init') {
    const root = positional[0] ?? process.cwd();
    const payload = await initializeRoot(root, options);
    process.stdout.write(options.json ? `${JSON.stringify(payload, null, 2)}\n` : renderInit(payload));
    return;
  }

  if (command === 'view-name') {
    const [kind, label, sourceKey, root = process.cwd()] = positional;
    if (!kind || !label || !sourceKey) {
      throw new Error('view-name requires <kind> <label> <source-key>');
    }
    const payload = {
      kind,
      label,
      sourceKey,
      path: viewPath(root, kind, label, sourceKey)
    };
    process.stdout.write(options.json ? `${JSON.stringify(payload, null, 2)}\n` : `${payload.path}\n`);
  }
}

function renderInit(payload) {
  const lines = [
    `Initialized Localgraph root: ${payload.root}`,
    `Config: ${payload.configPath}`,
    `Created: ${payload.created.length ? payload.created.join(', ') : 'none'}`,
    `Existing: ${payload.existing.length ? payload.existing.join(', ') : 'none'}`
  ];
  return `${lines.join('\n')}\n`;
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
