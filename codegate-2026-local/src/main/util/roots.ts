import path from 'node:path';
import { SINGLE_ROOT_LIMIT_MESSAGE } from '@contracts';
import { isInside } from './vpath';

export { SINGLE_ROOT_LIMIT_MESSAGE };

/**
 * 현재 sidecar 지식 파이프라인은 한 번에 원본 폴더 하나만 동기화한다.
 *
 * UI에서 버튼을 숨기는 것만으로는 IPC 직접 호출이나 이전 설치의 상태를 막지 못한다.
 * 그래서 저장 직전에도 현재 활성 폴더가 새 선택을 이미 포함하는지만 확인하고, 완전히
 * 다른 두 번째 폴더는 차단한다. 상위 폴더로 교체하려면 기존 폴더를 먼저 해제해야 한다.
 */
export type SingleRootPlan =
  | { action: 'add'; path: string }
  | { action: 'covered'; path: string }
  | { action: 'blocked'; activePath: string };

export function planSingleRootAddition(existing: string[], incoming: string): SingleRootPlan {
  const candidate = normalize(incoming);
  const active = existing[0] ? normalize(existing[0]) : null;
  if (!active) return { action: 'add', path: candidate };
  if (active === candidate || isInside(active, candidate)) {
    return { action: 'covered', path: active };
  }
  return { action: 'blocked', activePath: active };
}

/**
 * 끝의 구분자와 `.`·`..` 를 정리한다.
 * `/a/b/` 와 `/a/b` 가 다른 폴더로 등록되면 같은 파일을 두 번 읽는다.
 */
function normalize(value: string): string {
  return path.resolve(value);
}
