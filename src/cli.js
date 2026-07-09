#!/usr/bin/env node
import process from 'node:process';
import { checkRoot, initializeRoot, plannedLayout, renderPlan } from './layout.js';
import { scanInstagramSource } from './instagram.js';
import { renderSourceManifest } from './render.js';
import { checkSchema, databasePath, initializeDatabase } from './schema.js';
import { viewKinds, viewPath } from './views.js';

const COMMANDS = new Set(['help', 'init', 'plan', 'doctor', 'scan', 'render', 'view-name']);

function parseArgs(argv) {
  const [command = 'help', ...rest] = argv;
  const options = { json: false, force: false, dryRun: false, source: undefined };
  const positional = [];

  for (let i = 0; i < rest.length; i++) {
    const arg = rest[i];
    if (arg === '--json') options.json = true;
    else if (arg === '--force') options.force = true;
    else if (arg === '--dry-run') options.dryRun = true;
    else if (arg === '--source') options.source = rest[++i];
    else positional.push(arg);
  }

  return { command, positional, options };
}

function help() {
  return `localgraph

Usage:
  localgraph plan [root] [--json]
  localgraph init [root] [--json] [--force] [--dry-run]
  localgraph doctor [root] [--json]
  localgraph scan [root] [--source <path>] [--json]
  localgraph render [root] [--source <path>] [--json]
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
    const schema = options.dryRun ? undefined : await initializeDatabase(databasePath(payload.root));
    const result = { ...payload, database: schema };
    process.stdout.write(options.json ? `${JSON.stringify(result, null, 2)}\n` : renderInit(result));
    return;
  }

  if (command === 'doctor') {
    const root = positional[0] ?? process.cwd();
    const rootCheck = await checkRoot(root);
    const dbPath = databasePath(rootCheck.root);
    const schema = rootCheck.configExists && await import('node:fs/promises').then(({ access }) => access(dbPath).then(() => true, () => false))
      ? await checkSchema(dbPath)
      : { ok: false, tables: [], missing: ['database'] };
    const payload = { ...rootCheck, databasePath: dbPath, schema, ok: rootCheck.ok && schema.ok };
    process.stdout.write(options.json ? `${JSON.stringify(payload, null, 2)}\n` : renderInit(payload));
    return;
  }

  if (command === 'scan') {
    const root = positional[0] ?? process.cwd();
    const payload = await scanInstagramSource(root, options.source);
    process.stdout.write(options.json ? `${JSON.stringify(payload, null, 2)}\n` : renderScan(payload));
    return;
  }

  if (command === 'render') {
    const root = positional[0] ?? process.cwd();
    const scan = await scanInstagramSource(root, options.source);
    const payload = await renderSourceManifest(root, scan);
    process.stdout.write(options.json ? `${JSON.stringify(payload, null, 2)}\n` : renderRender(payload));
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
  if ('privateDirectories' in payload && 'schema' in payload) {
    const missing = payload.schema.missing.length ? payload.schema.missing.join(', ') : 'none';
    return [
      `Localgraph root: ${payload.root}`,
      `Config: ${payload.configExists ? 'ok' : 'missing'}`,
      `Database: ${payload.schema.ok ? 'ok' : 'missing schema'}`,
      `Missing schema tables: ${missing}`
    ].join('\n') + '\n';
  }
  const lines = [
    `Initialized Localgraph root: ${payload.root}`,
    `Config: ${payload.configPath}`,
    `Database: ${payload.database?.path ?? 'not created'}`,
    `Created: ${payload.created.length ? payload.created.join(', ') : 'none'}`,
    `Existing: ${payload.existing.length ? payload.existing.join(', ') : 'none'}`
  ];
  return `${lines.join('\n')}\n`;
}

function renderScan(payload) {
  return [
    `Instagram source: ${payload.sourcePath}`,
    `Exports: ${payload.exports.length}`,
    `Message files: ${payload.totalMessageFiles}`
  ].join('\n') + '\n';
}

function renderRender(payload) {
  return [
    `Rendered Localgraph views at ${payload.viewsDir}`,
    `Manifest: ${payload.manifestPath}`,
    `Exports: ${payload.exports}`,
    `Message files: ${payload.messageFiles}`
  ].join('\n') + '\n';
}

main().catch((error) => {
  process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
});
