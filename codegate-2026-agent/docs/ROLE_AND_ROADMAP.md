# 성주 역할 및 백엔드 로드맵

## 제품 목표

사용자가 흩어진 업무 문서를 자연어로 찾고, 지원되는 실제 source file을 안전하게 수정할 수 있는 지식 그래프 기반 파일 에이전트를 만든다.

최종 사용자 흐름은 두 개다.

1. 위치 찾기: 질문을 검색해 문서 ID, 실제 위치, 근거 청크, 관련 문서와 권한 상태를 반환한다.
2. 실제 수정: 자연어 변경 요청을 실제 파일 기준 diff로 만들고, 사용자가 정확한 계획을 승인한 뒤에만 수정·검증·버전·Undo·재변환·재색인을 수행한다.

## 성주의 단일 책임

성주는 frontend, converter, knowledge graph를 직접 구현하지 않는다. 세 구성요소를 안전한 실제 사용자 기능으로 연결하는 backend와 side effect의 최종 소유자다.

### API와 상태

- FastAPI와 OpenAPI schema
- chat, document, change plan, approval, execution, Undo API
- conversation, agent run, plan, approval, execution, file version, audit, outbox 상태
- idempotency key, correlation ID, retry와 terminal status
- polling 우선, 필요할 때만 SSE 추가

### Claude Agent SDK runtime

- `claude-agent-sdk`의 `ClaudeSDKClient`와 `ClaudeAgentOptions`
- application-owned read tool을 제공하는 in-process SDK MCP server
- built-in `Edit`, `Write`, `Bash` 비활성화와 `PreToolUse` allowlist hook
- `setting_sources=[]`, `skills=[]`, `plugins=[]`로 서버 runtime 격리
- `CLAUDE_CONFIG_DIR` 격리, auto memory·prompt history 비활성화
- checksum 포함을 강제한 `llm-wiki/AGENT_GUIDE.md`의 제한된 frontmatter만 검증·정규화해 모든 session 시작 지침에 주입
- versioned prompt contract, current-turn evidence, 구조화 outcome·source hash와 cross-field validator
- prompt/model/tool/graph/authorization fingerprint와 TTL이 일치하는 session만 resume
- routing·exact change·prompt injection 고정 eval corpus와 model-pinned read-only canary
- locate/change 의도 판단과 대상 선택
- model/tool 사용량과 비용 상한, session ID 추적에서 본문·secret 제외

모델 출력은 변경 제안일 뿐 실행 권한이 아니다. 경로, 권한, checksum, 승인, 실제 쓰기는 결정적 Python 코드가 판단한다.

### Knowledge adapter

- `LLMWIKI`의 tenant/wiki `current.json`과 불변 build를 native source of truth로 사용
- `WikiBuildService` in-process build와 expected current build ID CAS
- native schema를 Backend read 계약으로 변환한 build-ID compatibility cache
- versioned `llm-wiki`의 `VERSION`과 checksum 검증
- 요청자 ACL을 검색·relation 확장 전에 적용하고 권한 없는 문서의 존재도 숨김
- source와 chunk SHA-256을 실제 bytes·text와 대조
- canonical `section_id` index에서 heading path와 exact quote를 대조
- manifest, chunks, aliases, links schema와 참조 무결성 검증
- FTS/vector seed 검색과 graph 1-hop 확장
- evidence와 graph version을 모든 결과에 포함
- 용휘가 제공할 search API 또는 index format에 맞춘 adapter

### 안전한 파일 수정

- stable `document_id`를 현재 manifest에서 다시 조회
- `source://` URI를 allowlisted root 내부 path로만 해석
- symlink escape, read/write ACL, editability, MIME과 크기 검사
- 현재 source hash와 계획의 `base_sha256` compare-and-swap
- stable anchor에 대한 typed operation과 unified diff
- 계획 전체의 canonical `plan_hash`
- 승인 전 write 0건
- backup, journal, 같은 directory의 temporary file, 검증 후 atomic replace
- 새 file version과 hash, Undo conflict 검사

MVP actual write는 Markdown/TXT부터 지원한다. JSON/YAML은 schema 검증이 있을 때만 추가한다. HWP/PDF는 검증된 round-trip writer 전까지 read-only다.

### 변환·graph 동기화

- 수정 성공 뒤 `DocumentContentChanged` event 발행
- 민규 converter의 증분 재변환 호출과 `DocumentConverted` 수신
- 용휘 graph pipeline의 증분 update 호출과 `GraphUpdated` 수신
- 실제 파일 변경 성공과 conversion/graph sync 성공을 별도 상태로 표시
- 같은 event를 재처리해도 중복되지 않는 outbox/idempotency
- 변환 완료 뒤 FTS·vector·graph update를 병렬 fan-out하고 모두 성공한 version만 원자적으로 활성화
- 같은 문서는 base hash·execution lock으로 직렬화하고 서로 다른 문서는 병렬 처리
- graph publish CAS 패자는 최신 active version에 미반영 event를 rebase·coalesce해 선행 변경 유실 방지
- 동기화 실패 시 기존 graph version을 계속 제공하고 부분 version은 노출하지 않음
- approve HTTP와 긴 동기화를 분리한 lifespan outbox worker와 retry-sync

### 보안과 운영

- secret은 서버 환경변수에만 저장
- CORS allowlist, request size/schema 검증, 인증·인가 분리
- 공개 endpoint rate limit과 비용 상한
- 문서와 prompt를 신뢰할 수 없는 입력으로 취급
- prompt injection이 tool 권한이나 정책을 바꾸지 못하도록 경계 유지
- 합성 데이터만 사용한 demo reset과 audit evidence
- Railway health check, persistent source workspace 조건, structured logs

## 팀에서 받는 입력

### 용휘

- 실제 manifest/chunk/link sample과 JSON schema
- versioned `AGENT_GUIDE.md`의 검색·인용·ACL 규칙
- 최종 document ID와 relation enum
- `source://` mapping 규칙
- FTS/vector load 방법 또는 search API
- 문서 하나를 증분 update하는 진입점과 새 graph `VERSION`

### 민규

- sample source, canonical Markdown, asset, `ConversionResult`
- stable section 또는 source page/span mapping
- 형식별 parse/write-back capability
- 문서 하나의 증분 변환 진입점과 구조화 오류

### 우창

- `NEXT_PUBLIC_API_BASE_URL`과 CORS origin
- 현재 네 `response_type`과 별도 execution polling 화면 상태
- exact `plan_hash` 승인·거절 UI
- polling 간격, terminal status, Undo UX

입력이 늦어지면 이 저장소의 합성 fixture와 fake adapter로 backend 흐름을 먼저 완성한다.

## 구현 단계

| Phase | 범위 | 완료 기준 | 상태 |
| --- | --- | --- | --- |
| 0 | 계약과 합성 `llm-wiki` | checksum·schema·source URI 검증 | 완료 |
| 1 | read-only vertical slice | 질문 하나가 실제 위치·근거를 반환 | 완료 |
| 2 | Claude Agent SDK | deny-by-default runtime, 실제 SDK client 구조화 응답, server-owned session | 완료 |
| 3 | edit preview | 자연어 요청이 실제 파일 기준 diff를 만들고 write는 0건 | 완료 |
| 4 | approval·write·Undo | 승인 전 무변경, atomic write, crash recovery, Undo hash 복구 | 완료 |
| 5 | converter·graph sync | outbox, doc2md v0.2 adapter, 실제 LLMWIKI build, immutable VERSION publish | 완료(추가 enrichment/vector adapter 대기) |
| 6 | 통합·데모 보호 | E2E, 실패·retry, reset, Supabase/Railway fail-closed 설정 | 완료(외부 적용 대기) |
| 7 | 다운로드 앱 local runtime | loopback sidecar, 실제 LLMWIKI build, source/input 동기화, local capability | 완료(패키징 UI 대기) |
| 8 | 로그인 handoff | Supabase direct login, `/auth/me`, provisioning write gate, CORS 계약 | 완료(프로젝트 hook·UI 적용 대기) |

Phase 7은 실제 `codegate-2026-convert@30e6814` process와 `LLMWIKI@acf94f7` checkout으로 명령,
승인, source write, native current 교체, 변경된 근거 재검색과 Undo까지 검증했다. 실제 Claude 모델
호출은 사용자 API key, Supabase hook은
팀 project 설정, frontend login 화면과 desktop bundle은 각 담당 환경이 있어야 최종 smoke할 수
있다. 이 외부 조건을 Backend 구현 완료와 혼동하지 않는다.

## 최종 완료 기준

- 위치 검색이 실제 source file 존재와 근거까지 검증한다.
- 대상이 모호하면 자동 수정하지 않고 사용자 선택을 받는다.
- 승인 없이는 실제 파일이 바뀌지 않는다.
- 승인된 text 수정과 Undo가 hash로 증명된다.
- stale plan, root escape, read-only format을 안전하게 차단한다.
- 파일 변경과 conversion/graph sync 상태가 분리돼 보인다.
- 변경 후 검색 결과가 새 graph version과 내용을 반환한다.
- 병렬 색인 중에도 한 요청 안에서는 하나의 graph version snapshot만 사용한다.
- 정상 E2E, reject/unsupported 실패 흐름, Undo를 합성 데이터로 재현한다.
- 팀원이 README 하나로 로컬 실행할 수 있다.
