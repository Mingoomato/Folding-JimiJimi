# ADR 0006: Claude prompt contract와 평가 경계

- 상태: 채택
- 날짜: 2026-07-22

## 배경

Claude Agent SDK는 구조화 출력과 read-only MCP 도구를 제공하지만, JSON schema만으로 도구 선택,
근거의 최신성, prompt injection 무시, 변경 문구의 출처를 보장하지 않는다. 또한 SDK session을
resume하면 이전 turn의 tool result가 대화 문맥에 남는다. 기존 prompt는 안전 규칙을 포함했지만
도구 설명이 한 문장이었고, 실패를 성공 locate와 구별하는 필드와 실제 행동 평가 corpus가 없었다.

## 결정

서비스 prompt를 version 2.0 계약으로 관리한다.

- 정적 system prompt를 role, instruction hierarchy, trust, current-turn evidence, tool workflow,
  change, failure, examples, output contract로 구분한다. 동적 사용자 입력은 JSON의
  `untrusted_input`에만 넣는다.
- 다섯 가지 경계 예시를 제공하되 예시 값을 근거로 재사용하지 못하게 명시한다. 모든 turn은 현재
  graph version에 맞는 fresh tool result를 다시 요구한다.
- 구조화 출력에 `outcome`, `prompt_contract_version`, `source_sha256`을 추가하고 Pydantic
  cross-field validator로 locate/non-ready operation, 근거 없는 ready change를 거부한다.
- 변경 preview는 model이 반환한 source hash를 현재 snapshot과 비교하고, replacement가 사용자
  요청에 실제 포함됐는지 확인한다. 선택한 문서와 current-turn tool이 검증한 대상이 다르면 계획을
  만들지 않는다.
- MCP tool description은 WHAT, WHEN, WHEN NOT, 입력, 반환, 신뢰 경계를 설명한다. 결과 envelope은
  tool contract version과 `trust=untrusted_content`, source kind, graph version을 포함한다.
  Gateway는 검색 query, 검색 결과 안의 대상, `document_get`과 source read 순서, 결과 payload 형태와
  provenance를 구조화 결정과 대조한다. Tool result는 CLI가 반환한 `UserMessage` block에서만 수집하고
  assistant-authored result block은 신뢰하지 않는다. `ToolAnnotations`는 read-only closed-world 힌트로만
  사용하며 보안 경계로 간주하지 않는다. SDK의 read-only `StructuredOutput` control tool만 별도로
  허용하고 application tool trace에서는 제외한다.
- session resume은 system prompt, output schema, tool contract, SDK, model, AgentGuide, graph version,
  authorization snapshot의 SHA-256 fingerprint가 같고 1시간 TTL 안이며 가장 최근 run이 성공했을
  때만 허용한다. 실패하거나 중단된 최신 run 뒤에는 새 session으로 회전한다. Production은 재현 가능한
  정확한 model ID를 필수 설정으로 받는다.
- `evals/agent_prompt_cases.json`을 고정 corpus로 두고 routing, exact change, ambiguity, direct
  injection을 code grader로 평가한다. 실제 모델 canary는 명시한 model ID와 demo 문서만 사용하며
  CI의 결정적 단위 테스트와 분리한다.

이 결정은 Anthropic의 [system prompt 구성](https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts),
[prompting 권장사항](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices),
[tool 정의](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools),
[structured output](https://code.claude.com/docs/en/agent-sdk/structured-outputs),
[평가 방법](https://platform.claude.com/docs/en/test-and-evaluate/develop-tests)을 따른다.

## 검토했지만 선택하지 않은 대안

- Prompt 문구만 보강: model 동작 오류가 application boundary를 통과할 수 있어 선택하지 않았다.
- Claude Code system preset: 이 runtime은 코딩 도구와 설정을 의도적으로 제거한 문서 라우터이므로
  제품 정체성과 tool surface가 맞지 않는다.
- 모든 turn에서 새 session: stale context는 없지만 안전한 follow-up 문맥과 비용 이점을 잃는다.
  대신 fingerprint와 TTL로 resume 범위를 제한한다.
- 별도 model로 모든 tool result 사전 분류: latency와 비용이 늘고 prompt injection을 완전히 증명하지
  못한다. 고위험 운영 데이터에서 측정된 필요가 생기면 별도 방어층으로 검토한다.
- LLM grader만 사용: 보안·hash·target 불변식은 결정적 code grader가 더 재현 가능하다. 문장 품질
  평가가 필요할 때만 보조 grader를 추가한다.

## 결과와 남은 위험

Prompt 오류는 구조화된 non-ready 응답으로 닫히며, stale source나 대상 불일치는 계획 생성 전에
거부된다. Fingerprint가 바뀐 과거 session은 재사용하지 않지만 SDK transcript 파일 자체는 app-data에
남는다. 실제 파일 삭제는 보존 정책과 사용자 동의가 필요한 별도 lifecycle 작업으로 다룬다. Prompt와
평가는 권한 allowlist, ACL, hash, 별도 `plan_hash` 승인, 결정적 writer를 대체하지 않는다.
