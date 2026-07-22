import { describe, expect, it } from 'vitest';
import {
  planSingleRootAddition,
  SINGLE_ROOT_LIMIT_MESSAGE,
} from '@main/util/roots';

describe('한 폴더 지식 파이프라인', () => {
  it('등록된 폴더가 없으면 정규화한 첫 폴더를 받는다', () => {
    expect(planSingleRootAddition([], '/사업/계약/..')).toEqual({
      action: 'add',
      path: '/사업',
    });
  });

  it('같은 폴더나 그 하위는 기존 활성 폴더에 이미 포함된 것으로 본다', () => {
    expect(planSingleRootAddition(['/사업'], '/사업')).toEqual({
      action: 'covered',
      path: '/사업',
    });
    expect(planSingleRootAddition(['/사업'], '/사업/계약')).toEqual({
      action: 'covered',
      path: '/사업',
    });
  });

  it('다른 두 번째 폴더와 상위 폴더 교체는 기존 폴더를 해제할 때까지 막는다', () => {
    expect(planSingleRootAddition(['/사업'], '/개인')).toEqual({
      action: 'blocked',
      activePath: '/사업',
    });
    expect(planSingleRootAddition(['/사업/계약'], '/사업')).toEqual({
      action: 'blocked',
      activePath: '/사업/계약',
    });
    expect(SINGLE_ROOT_LIMIT_MESSAGE).toContain('기존 폴더를 해제');
  });

  it('이름이 비슷할 뿐인 형제 폴더를 하위로 착각하지 않는다', () => {
    expect(planSingleRootAddition(['/사업'], '/사업2').action).toBe('blocked');
  });

  it('이전 버전의 여러 폴더가 남아도 첫 폴더만 활성 폴더로 판단한다', () => {
    expect(planSingleRootAddition(['/활성', '/예전'], '/예전')).toEqual({
      action: 'blocked',
      activePath: '/활성',
    });
  });
});
