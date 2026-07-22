#!/usr/bin/env node
import path from 'node:path';

import {
  agentDir,
  cloudApiDir,
  convertDir,
  executable,
  filesystemDir,
  localDir,
  localRuntimeDir,
  nodeModulesExecutable,
  run,
  wikiBuilderDir,
} from './lib.mjs';

const uv = executable('uv');
const tsc = nodeModulesExecutable(localDir, 'tsc');
const vitest = nodeModulesExecutable(localDir, 'vitest');
const electronVite = nodeModulesExecutable(localDir, 'electron-vite');

await import('./check-runtime-environment.mjs');
await import('./check-component-manifest.mjs');

await run(tsc, ['--noEmit', '-p', 'tsconfig.node.json'], {
  cwd: localDir,
  name: 'Electron main typecheck',
});
await run(tsc, ['--noEmit', '-p', 'tsconfig.web.json'], {
  cwd: localDir,
  name: 'Electron web typecheck',
});
await run(vitest, ['run'], { cwd: localDir, name: 'Electron tests' });
await run(electronVite, ['build'], { cwd: localDir, name: 'Electron build' });

for (const [name, project, projectArgs, commands] of [
  [
    'Agent',
    agentDir,
    [],
    [
      ['ruff', 'format', '--check', '.'],
      ['ruff', 'check', '.'],
      ['mypy', 'src'],
      ['pytest'],
    ],
  ],
  ['Cloud API', cloudApiDir, ['--extra', 'dev'], [['ruff', 'check', '.'], ['pytest']]],
  ['LLMWIKI builder', wikiBuilderDir, ['--extra', 'dev'], [['ruff', 'check', '.'], ['pytest']]],
  ['Local runtime', localRuntimeDir, ['--extra', 'dev'], [['ruff', 'check', '.'], ['pytest']]],
]) {
  for (const command of commands) {
    await run(uv, ['run', '--project', project, '--locked', ...projectArgs, ...command], {
      cwd: project,
      name: `${name}: ${command[0]}`,
    });
  }
}

await run(uv, ['run', '--project', convertDir, '--locked', '--extra', 'test', 'pytest', '-q', 'tests'], {
  cwd: convertDir,
  name: 'doc2md tests',
});

for (const command of [
  ['ruff', 'format', '--check', filesystemDir],
  ['ruff', 'check', filesystemDir],
  ['mypy', path.join(filesystemDir, 'src', 'codegate_filesystem')],
]) {
  await run(uv, ['run', '--project', agentDir, '--locked', ...command], {
    cwd: agentDir,
    name: `Filesystem adapter: ${command[0]}`,
  });
}

if (process.argv.includes('--smoke')) await import('./smoke.mjs');
