import { EventEmitter } from 'node:events';
import { describe, expect, it } from 'vitest';
// Production launcher helper is intentionally plain ESM.
// @ts-expect-error JavaScript helper에는 별도 declaration 파일을 만들지 않는다.
import { waitForTrackedExit } from '../scripts/child-process-lifecycle.mjs';

class FakeChild extends EventEmitter {
  exitCode: number | null = null;
  signalCode: NodeJS.Signals | null = null;
}

describe('integrated launcher child lifecycle', () => {
  it('listener 등록 전에 SIGTERM으로 끝난 process를 즉시 정상 종료로 처리한다', async () => {
    const child = new FakeChild();
    child.signalCode = 'SIGTERM';

    await expect(
      waitForTrackedExit({ child, name: 'electron', spawnError: null }),
    ).resolves.toBeUndefined();
  });

  it('listener 등록 전에 비정상 signal로 끝난 process를 즉시 거부한다', async () => {
    const child = new FakeChild();
    child.signalCode = 'SIGKILL';

    await expect(
      waitForTrackedExit({ child, name: 'electron', spawnError: null }),
    ).rejects.toThrow('SIGKILL');
  });
});
