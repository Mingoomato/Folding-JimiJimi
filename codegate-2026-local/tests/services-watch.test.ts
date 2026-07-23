import { describe, expect, it } from 'vitest';
import type { FileRow } from '@main/db/store';
import { IPC } from '@contracts';
import {
  assertWriteProvisioning,
  DEFAULT_CLOUD_API_URL,
  managedDocumentChangeMatches,
  watchChangeNeedsKnowledgeSync,
} from '@main/services';

const indexed: FileRow = {
  rootId: 'root-1',
  relPath: 'policy.md',
  absPath: '/workspace/policy.md',
  sha256: 'a'.repeat(64),
  size: 10,
  mtime: '2026-07-22T00:00:00.000Z',
  status: 'done',
  dirty: false,
  deleted: false,
};

describe('watch knowledge sync routing', () => {
  it('touch처럼 SHA가 같은 metadata-only 이벤트는 재동기화하지 않는다', () => {
    const touched = { ...indexed, mtime: '2026-07-22T00:01:00.000Z' };
    expect(watchChangeNeedsKnowledgeSync('change', indexed, touched)).toBe(false);
  });

  it('내용 변경과 실제 삭제만 재동기화한다', () => {
    expect(
      watchChangeNeedsKnowledgeSync('change', indexed, {
        ...indexed,
        sha256: 'b'.repeat(64),
        dirty: true,
      }),
    ).toBe(true);
    expect(watchChangeNeedsKnowledgeSync('unlink', indexed, null)).toBe(true);
    expect(watchChangeNeedsKnowledgeSync('unlink', { ...indexed, deleted: true }, null)).toBe(false);
  });

  it('승인 executor가 만든 동일 path+SHA 파일만 일반 watcher 재빌드에서 제외한다', () => {
    expect(
      managedDocumentChangeMatches(
        '보고서\\일일업무.HWPX',
        'A'.repeat(64),
        '보고서/일일업무.hwpx',
        'a'.repeat(64),
      ),
    ).toBe(true);
    expect(
      managedDocumentChangeMatches(
        '보고서/일일업무.hwpx',
        'b'.repeat(64),
        '보고서/일일업무.hwpx',
        'a'.repeat(64),
      ),
    ).toBe(false);
  });
});

describe('통합 서비스 계약', () => {
  it('로컬 cloud-api 기본 주소는 실제 backend 포트 8000을 쓴다', () => {
    expect(DEFAULT_CLOUD_API_URL).toBe('http://127.0.0.1:8000/api/v1');
  });

  it('cloud retry/undo는 Google 세션의 provisioning을 반드시 확인한다', () => {
    expect(() =>
      assertWriteProvisioning('cloud', true, {
        authenticated: true,
        provisioned: false,
        writeScope: 'none',
      }),
    ).toThrow('문서 쓰기 권한');

    expect(() =>
      assertWriteProvisioning('cloud', true, {
        authenticated: true,
        provisioned: true,
        writeScope: 'workspace',
      }),
    ).not.toThrow();
  });

  it('local retry/undo는 계정 provisioning 대신 활성 폴더를 확인한다', () => {
    expect(() => assertWriteProvisioning('local', false, { authenticated: true })).toThrow(
      '문서 폴더',
    );
    expect(() => assertWriteProvisioning('local', true, { authenticated: true })).not.toThrow();
  });

  it('취소할 수 없는 sidecar 동기화를 build cancel IPC로 광고하지 않는다', () => {
    expect('buildCancel' in IPC).toBe(false);
    expect('rootsPickDialogMulti' in IPC).toBe(false);
  });
});
