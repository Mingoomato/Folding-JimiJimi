#!/usr/bin/env node
import path from 'node:path';

import {
  agentDir,
  backendDir,
  convertDir,
  localDir,
  rootDir,
  run,
  venvExecutable,
} from './lib.mjs';

const env = {
  ...process.env,
  CODEGATE_AGENT_MODE: 'deterministic',
  CODEGATE_DOC2MD_PROJECT_ROOT: convertDir,
  CODEGATE_DOC2MD_PYTHON: venvExecutable(convertDir, 'python'),
  CODEGATE_SIDECAR_BIN: venvExecutable(agentDir, 'codegate-local'),
  CODEGATE_LLMWIKI_PROJECT_ROOT: backendDir,
  DOC2MD_DATA_ROOT: path.join(rootDir, '.runtime', 'doc2md-smoke'),
};

await run(process.execPath, [path.join(localDir, 'scripts', 'smoke-integrations.mjs')], {
  cwd: localDir,
  env,
  name: '4-repository integration smoke',
});
