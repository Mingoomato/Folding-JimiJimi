import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { readFile, readdir, realpath, writeFile } from 'node:fs/promises';
import path from 'node:path';
import readline from 'node:readline';

const [stagingArg, kordocArg] = process.argv.slice(2);
if (!stagingArg || !kordocArg) throw new Error('staging and Kordoc roots are required');
const stagingRoot = await realpath(stagingArg);
const kordocRoot = await realpath(kordocArg);
const requireFromKordoc = createRequire(path.join(kordocRoot, 'package.json'));
const kordoc = await import(requireFromKordoc.resolve('kordoc'));
const packageJson = JSON.parse(await readFile(requireFromKordoc.resolve('kordoc/package.json'), 'utf8'));
if (packageJson.version !== '4.2.5') throw new Error('Kordoc 4.2.5 is required');

function safePath(relative) {
  if (typeof relative !== 'string' || path.isAbsolute(relative) || relative.includes('\0')) {
    throw new Error('unsafe staging path');
  }
  const resolved = path.resolve(stagingRoot, relative);
  if (resolved !== stagingRoot && !resolved.startsWith(stagingRoot + path.sep)) {
    throw new Error('staging path escapes root');
  }
  return resolved;
}

function assertWithinStaging(resolved) {
  if (resolved !== stagingRoot && !resolved.startsWith(stagingRoot + path.sep)) {
    throw new Error('staging path resolves outside root');
  }
  return resolved;
}

async function existingPath(relative) {
  return assertWithinStaging(await realpath(safePath(relative)));
}

async function outputPath(relative) {
  const lexical = safePath(relative);
  assertWithinStaging(await realpath(path.dirname(lexical)));
  return lexical;
}

async function runCli(args) {
  const binValue = typeof packageJson.bin === 'string' ? packageJson.bin : packageJson.bin?.kordoc;
  if (!binValue) throw new Error('Kordoc CLI entry is unavailable');
  const binPath = path.resolve(path.dirname(requireFromKordoc.resolve('kordoc/package.json')), binValue);
  return await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [binPath, ...args], {
      cwd: stagingRoot,
      shell: false,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk.toString('utf8'); });
    child.stderr.on('data', (chunk) => { stderr += chunk.toString('utf8'); });
    child.once('error', reject);
    child.once('close', (code) => resolve({ code: code ?? 1, stdout, stderr }));
  });
}

async function parseDocument(input) {
  const source = await readFile(await existingPath(input));
  const result = await kordoc.parse(source);
  if (!result?.success) throw new Error(result?.error?.message ?? 'Kordoc parse failed');
  return result;
}

async function handle(request) {
  switch (request.command) {
    case 'parse': {
      const result = await parseDocument(request.input);
      return { markdown: result.markdown, blocks: result.blocks, metadata: result.metadata };
    }
    case 'markdownToHwpx': {
      const content = await kordoc.markdownToHwpx(String(request.markdown ?? ''));
      await writeFile(await outputPath(request.output), Buffer.from(content));
      return { output: request.output };
    }
    case 'patchHwpx': {
      const result = await parseDocument(request.input);
      let markdown = result.markdown;
      for (const operation of request.operations ?? []) {
        if (!['text.replace/v1', 'table.cell_set/v1'].includes(operation.type)) {
          throw new Error('unsupported HWPX operation');
        }
        const expected = String(operation.expected ?? '');
        const replacement = String(operation.replacement ?? '');
        if (!expected || markdown.split(expected).length - 1 !== 1) {
          throw new Error('HWPX expected text must be globally unique');
        }
        markdown = markdown.replace(expected, replacement);
      }
      const edited = request.editedMarkdown;
      const inputPath = await existingPath(request.input);
      const editedPath = await outputPath(edited);
      const resultPath = await outputPath(request.output);
      await writeFile(editedPath, markdown, 'utf8');
      const cli = await runCli([
        'patch', inputPath, '--md', editedPath, '--out', resultPath, '--json',
      ]);
      let report = {};
      try { report = JSON.parse(cli.stdout); } catch { report = {}; }
      const skipped = report.skipped ?? report.unapplied ?? [];
      if (cli.code !== 0 || skipped.length || Number(report.applied ?? 0) < (request.operations ?? []).length) {
        throw new Error(`Kordoc patch incomplete: ${cli.stderr || cli.stdout}`);
      }
      const verified = await parseDocument(request.output);
      if (verified.markdown !== markdown) {
        throw new Error('Kordoc changed non-target canonical content');
      }
      for (const operation of request.operations ?? []) {
        if (!verified.markdown.includes(String(operation.replacement ?? ''))) {
          throw new Error('Kordoc parse-after-write verification failed');
        }
      }
      return { output: request.output, applied: report.applied, skipped: [] };
    }
    case 'render_document': {
      const inputPath = await existingPath(request.input);
      const outputDir = await existingPath(request.outputDir);
      const cli = await runCli(['render', inputPath, '--reflow', '--out', outputDir]);
      if (cli.code !== 0) throw new Error(`Kordoc render failed: ${cli.stderr || cli.stdout}`);
      const names = (await readdir(outputDir)).filter((name) => name.endsWith('.png')).sort();
      return { pages: names.map((name) => path.join(request.outputDir, name).replaceAll('\\', '/')) };
    }
    default:
      throw new Error('unknown worker command');
  }
}

const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of lines) {
  let request;
  try {
    request = JSON.parse(line);
    const data = await handle(request);
    process.stdout.write(JSON.stringify({ id: request.id, ok: true, data }) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({
      id: request?.id ?? null,
      ok: false,
      error: String(error?.message ?? error).slice(0, 1000),
    }) + '\n');
  }
}
