import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { readFile, realpath, writeFile } from 'node:fs/promises';
import path from 'node:path';
import readline from 'node:readline';
import { pathToFileURL } from 'node:url';
import { fillDailyWorkTemplate } from './kordoc_template_layout.mjs';

const [stagingArg, kordocArg] = process.argv.slice(2);
if (!stagingArg || !kordocArg) throw new Error('staging and Kordoc roots are required');
const stagingRoot = await realpath(stagingArg);
const kordocRoot = await realpath(kordocArg);
const requireFromKordoc = createRequire(path.join(kordocRoot, 'package.json'));
const packagePath = path.join(kordocRoot, 'node_modules', 'kordoc', 'package.json');
const kordocEntry = requireFromKordoc.resolve('kordoc');
const requireFromPackage = createRequire(kordocEntry);
const kordoc = await import(pathToFileURL(kordocEntry).href);
const packageJson = JSON.parse(await readFile(packagePath, 'utf8'));
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
  const binPath = path.resolve(path.dirname(packagePath), binValue);
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
    case 'deriveHwpTemplate': {
      const parsed = await parseDocument(request.input);
      if (typeof parsed.markdown !== 'string') throw new Error('Kordoc HWP parse returned no Markdown');
      const before = await kordoc.markdownToHwpx(parsed.markdown);
      await writeFile(await outputPath(request.beforeOutput), Buffer.from(before));
      const filled = fillDailyWorkTemplate(parsed.markdown, request.markdown);
      const profile = await kordoc.hwpxToProfile(Buffer.from(before));
      const proposed = await kordoc.markdownToHwpx(filled.markdown, { profile });
      await writeFile(await outputPath(request.output), Buffer.from(proposed));
      const verified = await parseDocument(request.output);
      for (const column of filled.verifiedLines ?? []) {
        if (!verified.markdown.includes(column)) {
          throw new Error('Kordoc template derivation parse-after-write verification failed');
        }
      }
      return {
        output: request.output,
        beforeOutput: request.beforeOutput,
        inserted: filled.inserted,
        layout: { ...filled.layout, profileApplied: true },
      };
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
        'patch', inputPath, editedPath, '--output', resultPath, '--silent',
      ]);
      if (cli.code !== 0) {
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
      return { output: request.output, applied: (request.operations ?? []).length, skipped: [] };
    }
    case 'render_document': {
      const inputPath = await existingPath(request.input);
      const outputDir = await existingPath(request.outputDir);
      const svgPath = path.join(outputDir, 'document.svg');
      const pngPath = path.join(outputDir, 'page-1.png');
      const cli = await runCli([
        'render', inputPath, '--reflow', '--output', svgPath, '--silent',
      ]);
      if (cli.code !== 0) throw new Error(`Kordoc render failed: ${cli.stderr || cli.stdout}`);
      const sharpModule = await import(pathToFileURL(requireFromPackage.resolve('sharp')).href);
      await sharpModule.default(svgPath, { density: 120 }).png().toFile(pngPath);
      return { pages: [path.join(request.outputDir, 'page-1.png').replaceAll('\\', '/')] };
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
