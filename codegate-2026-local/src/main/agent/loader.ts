/**
 * `@codegate/agent`(성주 레포) 로더.
 * 아직 배포 전이라 해석에 실패할 수 있으므로, 실패하면 내장 목 에이전트로 폴백한다.
 * 어느 쪽이 선택됐는지는 로그로 분명히 남긴다 (조용한 폴백 금지).
 */
import type { CreateAgent } from '@contracts';
import { createMockAgent } from './mock-agent';
import { createSidecarAgent } from './sidecar-agent';

export type AgentSource = 'real' | 'mock';

export interface LoadedAgentFactory {
  createAgent: CreateAgent;
  source: AgentSource;
}

export async function loadCreateAgent(): Promise<LoadedAgentFactory> {
  if (process.env.CODEGATE_MOCK === '1') {
    console.info('[codegate:agent] CODEGATE_MOCK=1 — 내장 목 에이전트를 사용합니다.');
    return { createAgent: createMockAgent, source: 'mock' };
  }
  console.info('[codegate:agent] Python sidecar API를 사용합니다.');
  return { createAgent: createSidecarAgent, source: 'real' };
}
