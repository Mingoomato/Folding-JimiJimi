import { describe, expect, it } from 'vitest';
import { SidecarSupervisor } from '@main/sidecar/supervisor';

/**
 * 위키가 등록 폴더와 어긋났을 때의 자기치유.
 *
 * 활성 빌드가 참조하는 원본이 사라지면(폴더를 바꾸거나 파일을 지우면) sidecar 는 기동
 * 자체를 거부한다. 실제로 등록 폴더를 바꿨더니 앱이 **아예 뜨지 못하는 상태**가 됐고,
 * 되살리는 방법이 화면에 없어 디렉터리를 손으로 지워야 했다. 사용자는 할 수 없는 일이다.
 *
 * 그래서 실패 원인을 stderr 로 판별한다. 이 판별이 무너지면 자기치유가 통째로 죽거나,
 * 반대로 **엉뚱한 실패에 위키를 지워 버린다.** 그래서 여기서 고정한다.
 */
function supervisorWithStderr(text: string): SidecarSupervisor {
  const supervisor = new SidecarSupervisor({ corsOrigin: 'app://test', onState: () => {} });
  // stderr 수집 경로를 직접 흉내 낸다 — 프로세스를 띄우지 않고 판별만 검증한다.
  (supervisor as unknown as { stderrTail: string }).stderrTail = text;
  return supervisor;
}

describe('위키 불일치 판별', () => {
  it('원본이 사라졌다는 신호를 알아본다', () => {
    const real =
      'codegate_api.knowledge.repository.KnowledgePackageError: ' +
      'source document is missing: source://%EC%9D%BC%EC%9D%BC%EC%97%85%EB%AC%B4.md';
    expect(supervisorWithStderr(real).failedOnStaleWiki).toBe(true);
  });

  it('다른 이유의 실패에는 손대지 않는다 — 모르면서 데이터를 지우지 않는다', () => {
    for (const other of [
      'RuntimeError: Claude Agent SDK requires ANTHROPIC_API_KEY',
      'OSError: [Errno 48] Address already in use',
      'ModuleNotFoundError: No module named wiki_builder',
      '',
    ]) {
      expect(supervisorWithStderr(other).failedOnStaleWiki, other || '(빈 stderr)').toBe(false);
    }
  });

  it('stderr 를 비우면 판별도 초기화된다 — 지난 실패가 다음 기동에 번지지 않는다', () => {
    const supervisor = supervisorWithStderr('source document is missing: source://a.md');
    expect(supervisor.failedOnStaleWiki).toBe(true);

    supervisor.clearRecentStderr();

    expect(supervisor.failedOnStaleWiki).toBe(false);
    expect(supervisor.recentStderr).toBe('');
  });
});
