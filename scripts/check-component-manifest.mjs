import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

import { rootDir } from './lib.mjs';

const manifestPath = path.join(rootDir, 'component-manifest.json');
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const failures = [];

if (manifest.schema_version !== 1) failures.push('unsupported manifest schema');
if (manifest.source_of_truth?.branch !== 'main') failures.push('source-of-truth branch must be main');
if (manifest.runtime?.node !== '22.x') failures.push('Node runtime must be pinned to 22.x');
if (manifest.runtime?.python !== '3.12.x') failures.push('Python runtime must be pinned to 3.12.x');
if (manifest.runtime?.kordoc !== '4.2.5') failures.push('Kordoc runtime must be pinned to 4.2.5');

for (const component of manifest.components ?? []) {
  if (!component.id || !component.path || !existsSync(path.join(rootDir, component.path))) {
    failures.push(`missing component path: ${component.id ?? 'unknown'}`);
  }
}

const localPackage = JSON.parse(
  readFileSync(path.join(rootDir, 'codegate-2026-local', 'package.json'), 'utf8'),
);
if (localPackage.dependencies?.kordoc !== manifest.runtime.kordoc) {
  failures.push('desktop Kordoc dependency does not match the component manifest');
}

for (const project of [
  'codegate-2026-agent/pyproject.toml',
  'codegate-2026-backend/wiki-builder/pyproject.toml',
  'codegate-2026-backend/local-runtime/pyproject.toml',
  'packages/codegate-filesystem/pyproject.toml',
]) {
  const content = readFileSync(path.join(rootDir, project), 'utf8');
  if (!content.includes('3.12')) failures.push(`${project} does not pin Python 3.12`);
}

if (failures.length) {
  throw new Error(`component manifest check failed:\n- ${failures.join('\n- ')}`);
}

console.log('✓ component manifest and runtime pins');
