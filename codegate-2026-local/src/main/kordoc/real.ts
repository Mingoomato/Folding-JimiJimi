/**
 * 실제 kordoc CLI 를 spawn 하는 구현체 (스펙 v1.3 §1 L3).
 *
 * kordoc 바이너리는 아직 손에 없다. 없으면 **조용히 성공한 척하지 않고 한국어 오류로 크게 실패한다**
 * (스펙 v1.3 §5 silent fail 금지). 목 구현으로 돌리려면 `CODEGATE_MOCK=1`.
 */
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { UserFacingError } from '@main/util/errors';
import { KordocBase, type RunPatchOutcome } from './base';

const KORDOC_NOT_FOUND =
  'kordoc 실행 파일을 찾을 수 없습니다. kordoc 을 설치하거나 CODEGATE_KORDOC_BIN 환경변수로 경로를 지정해 주세요.';

interface CliResult {
  code: number;
  stdout: string;
  stderr: string;
}

export interface RealKordocOptions {
  /** kordoc 실행 파일 경로. 기본값은 PATH 상의 `kordoc`. */
  bin?: string;
  /** 임시 작업 디렉터리 (기본: OS temp) */
  tmpDir?: string;
}

export class RealKordoc extends KordocBase {
  private readonly bin: string;
  private readonly tmpDir: string;

  constructor(options: RealKordocOptions = {}) {
    super();
    this.bin = options.bin ?? process.env.CODEGATE_KORDOC_BIN ?? 'kordoc';
    this.tmpDir = options.tmpDir ?? os.tmpdir();
  }

  async parse(filePath: string): Promise<string> {
    const result = await this.run(['parse', filePath, '--format', 'md']);
    if (result.code !== 0) {
      throw new UserFacingError(
        `문서를 읽지 못했습니다: ${path.basename(filePath)} (kordoc 종료 코드 ${result.code})`,
      );
    }
    return result.stdout;
  }

  async generate(md: string, outPath: string, preset?: string): Promise<void> {
    const mdPath = await this.writeScratch(md, '.md');
    try {
      const args = ['generate', '--md', mdPath, '--out', outPath];
      if (preset) args.push('--preset', preset);
      const result = await this.run(args);
      if (result.code !== 0) {
        throw new UserFacingError(
          `문서를 생성하지 못했습니다 (kordoc 종료 코드 ${result.code}). ${firstLine(result.stderr)}`,
        );
      }
    } finally {
      await fs.rm(mdPath, { force: true });
    }
  }

  protected async runPatch(
    sourcePath: string,
    editedMd: string,
    outPath: string,
  ): Promise<RunPatchOutcome> {
    const mdPath = await this.writeScratch(editedMd, '.md');
    try {
      // `fill` 은 오탐 때문에 금지 (기획안 §8) — 반드시 patch + 명시 치환.
      const result = await this.run([
        'patch',
        sourcePath,
        '--md',
        mdPath,
        '--out',
        outPath,
        '--json',
      ]);
      const report = parsePatchReport(result.stdout);
      return {
        exitCode: result.code,
        applied: report.applied,
        unapplied: report.unapplied,
      };
    } finally {
      await fs.rm(mdPath, { force: true });
    }
  }

  protected async renderPages(filePath: string, pages?: number[]): Promise<Buffer[]> {
    const outDir = path.join(this.tmpDir, `kordoc-render-${randomUUID()}`);
    await fs.mkdir(outDir, { recursive: true });
    try {
      const args = ['render', filePath, '--reflow', '--out', outDir];
      if (pages && pages.length > 0) args.push('--pages', pages.join(','));
      const result = await this.run(args);
      if (result.code !== 0) {
        throw new UserFacingError(
          `페이지를 렌더하지 못했습니다 (kordoc 종료 코드 ${result.code}). ${firstLine(result.stderr)}`,
        );
      }
      const names = (await fs.readdir(outDir)).filter((n) => n.endsWith('.png')).sort();
      return Promise.all(names.map((n) => fs.readFile(path.join(outDir, n))));
    } finally {
      await fs.rm(outDir, { recursive: true, force: true });
    }
  }

  private async writeScratch(content: string, ext: string): Promise<string> {
    const target = path.join(this.tmpDir, `kordoc-${randomUUID()}${ext}`);
    await fs.writeFile(target, content, 'utf8');
    return target;
  }

  private run(args: string[]): Promise<CliResult> {
    return new Promise((resolve, reject) => {
      const child = spawn(this.bin, args, { stdio: ['ignore', 'pipe', 'pipe'] });
      let stdout = '';
      let stderr = '';
      child.stdout.on('data', (c: Buffer) => (stdout += c.toString('utf8')));
      child.stderr.on('data', (c: Buffer) => (stderr += c.toString('utf8')));
      child.on('error', (err: NodeJS.ErrnoException) => {
        // 바이너리 부재는 반드시 크게 실패한다 — 조용히 목으로 대체하지 않는다.
        reject(
          err.code === 'ENOENT'
            ? new UserFacingError(KORDOC_NOT_FOUND, { cause: err })
            : new UserFacingError(`kordoc 실행에 실패했습니다: ${err.message}`, { cause: err }),
        );
      });
      child.on('close', (code) => resolve({ code: code ?? 1, stdout, stderr }));
    });
  }
}

/** kordoc `--json` 리포트 파싱. 형식이 달라도 앱이 죽지 않게 보수적으로 읽는다. */
function parsePatchReport(stdout: string): { applied: number; unapplied: string[] } {
  try {
    const parsed = JSON.parse(stdout) as { applied?: number; unapplied?: unknown };
    const unapplied = Array.isArray(parsed.unapplied) ? parsed.unapplied.map(String) : [];
    return { applied: typeof parsed.applied === 'number' ? parsed.applied : 0, unapplied };
  } catch {
    return { applied: 0, unapplied: [] };
  }
}

function firstLine(text: string): string {
  return text.split('\n')[0]?.trim() ?? '';
}
