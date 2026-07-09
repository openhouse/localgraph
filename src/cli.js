#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { initWorkspace, loadConfig, resolveWorkspacePaths } from './config.js';
import { scanInstagramSource } from './instagram.js';
import { renderViews } from './render.js';

const commands = new Set(['help', 'init', 'doctor', 'scan', 'render']);

async function main(argv = process.argv.slice(2)) {
  const command = commands.has(argv[0]) ? argv[0] : argv[0] ? 'help' : 'help';
  const rest = commands.has(argv[0]) ? argv.slice(1) : argv;
  const { values } = parseArgs({
    args: rest,
    options: {
      root: { type: 'string', short: 'r' },
      source: { type: 'string', short: 's' },
      json: { type: 'boolean' }
    },
    allowPositionals: true
  });
  const root = values.root ?? process.cwd();

  if (command === 'help') {
    console.log(helpText());
    return;
  }

  if (command === 'init') {
    const result = await initWorkspace(root);
    writeResult(result, values.json);
    return;
  }

  const config = await loadConfig(root);
  const paths = resolveWorkspacePaths(root, config);

  if (command === 'doctor') {
    const result = await doctor(paths);
    writeResult(result, values.json);
    return;
  }

  if (command === 'scan') {
    const result = await scanInstagramSource(paths, values.source);
    writeResult(result, values.json);
    return;
  }

  if (command === 'render') {
    const scan = await scanInstagramSource(paths, values.source);
    const result = await renderViews(paths, scan);
    writeResult(result, values.json);
  }
}

async function doctor(paths) {
  const checks = await Promise.all(Object.entries(paths.privateDirs).map(async ([name, dir]) => {
    const exists = await pathExists(dir);
    return { name, path: dir, exists };
  }));
  return {
    ok: checks.every((check) => check.exists),
    workspaceRoot: paths.root,
    configPath: paths.configPath,
    checks
  };
}

async function pathExists(path) {
  try {
    const { access } = await import('node:fs/promises');
    await access(path);
    return true;
  } catch {
    return false;
  }
}

function writeResult(result, asJson = false) {
  if (asJson) {
    console.log(JSON.stringify(result, null, 2));
    return;
  }
  if ('created' in result) {
    console.log(`Initialized Localgraph workspace at ${result.workspaceRoot}`);
    for (const dir of result.created) console.log(`created ${dir}`);
    return;
  }
  if ('exports' in result) {
    console.log(`Instagram source: ${result.sourcePath}`);
    console.log(`Exports: ${result.exports.length}`);
    console.log(`Message files: ${result.totalMessageFiles}`);
    return;
  }
  if ('manifestPath' in result) {
    console.log(`Rendered Localgraph views at ${result.viewsDir}`);
    console.log(`Manifest: ${result.manifestPath}`);
    return;
  }
  console.log(JSON.stringify(result, null, 2));
}

function helpText() {
  return `localgraph

Usage:
  localgraph init [--root <path>] [--json]
  localgraph doctor [--root <path>] [--json]
  localgraph scan [--root <path>] [--source <path>] [--json]
  localgraph render [--root <path>] [--source <path>] [--json]

Commands:
  init    Create local private data directories and localgraph.config.json.
  doctor  Check configured local private directories.
  scan    Detect Instagram transfer exports without reading message bodies.
  render  Generate symlink-friendly view directories and a source manifest.
`;
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
