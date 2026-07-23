import assert from 'node:assert/strict';

import { buildRuntimeEnvironments, FIXED_CALLBACK } from './runtime-environment.mjs';

const { cloudEnv, desktopEnv } = buildRuntimeEnvironments(
  {
    PATH: '/safe/bin',
    GEMINI_API_KEY: 'gemini-secret',
    CODEGATE_GOOGLE_CLIENT_ID: 'google-id',
    CODEGATE_GOOGLE_CLIENT_SECRET: 'google-secret',
    CODEGATE_SESSION_PEPPER: 'session-secret',
    UNRELATED_SECRET: 'must-not-cross',
  },
  {
    runtimeDir: '/runtime',
    convertDir: '/convert',
    doc2mdPython: '/convert/python',
    sidecarBin: '/agent/codegate-local',
    backendDir: '/backend',
    localDir: '/local',
    nodeBin: '/runtime/node',
  },
);

assert.equal(cloudEnv.CODEGATE_GOOGLE_CLIENT_ID, 'google-id');
assert.equal(cloudEnv.CODEGATE_GOOGLE_CLIENT_SECRET, 'google-secret');
assert.equal(cloudEnv.CODEGATE_SESSION_PEPPER, 'session-secret');
assert.equal(cloudEnv.GEMINI_API_KEY, undefined);
assert.equal(cloudEnv.UNRELATED_SECRET, undefined);

assert.equal(desktopEnv.GEMINI_API_KEY, 'gemini-secret');
assert.equal(desktopEnv.CODEGATE_GOOGLE_CLIENT_ID, undefined);
assert.equal(desktopEnv.CODEGATE_GOOGLE_CLIENT_SECRET, undefined);
assert.equal(desktopEnv.CODEGATE_SESSION_PEPPER, undefined);
assert.equal(desktopEnv.UNRELATED_SECRET, undefined);
assert.equal(desktopEnv.CODEGATE_OAUTH_REDIRECT_URI, FIXED_CALLBACK);
assert.equal(desktopEnv.CODEGATE_GEMINI_MODEL, 'gemini-2.5-flash-lite');
assert.equal(desktopEnv.CODEGATE_GEMINI_DATA_POLICY, 'paid-no-training');
assert.equal(desktopEnv.CODEGATE_NODE_BIN, '/runtime/node');
assert.equal(desktopEnv.CODEGATE_KORDOC_WORKSPACE_ROOT, '/local');

console.log('✓ root launcher secret scope and fixed runtime contract');
