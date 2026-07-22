# CODEGATE 2026 Backend

성주가 담당하는 지식 그래프 기반 문서 검색·수정 에이전트의 FastAPI 백엔드다. Gemini REST 또는 Claude Agent SDK가 질문을 설명하고, 애플리케이션이 ACL·원문 근거·승인·파일 쓰기·Undo·지식 버전 공개를 결정적으로 통제한다.

합성 fixture와 통합본에 함께 고정된 LLMWIKI checkout 기준으로 다음 수직 흐름이 동작한다.

```text
질문 또는 변경 요청
→ Gemini REST, Claude Agent SDK 또는 deterministic local gateway
→ status·효력 기간·ACL 필터
→ alias·chunk·verified relation 검색
→ canonical section/quote 근거 검증
→ exact diff와 immutable plan_hash
→ 사용자 승인
→ backup·journal·symlink-safe atomic replace
→ outbox worker
→ 문서 변환
→ FTS / vector / graph 병렬 빌드
→ schema·reference·checksum barrier
→ immutable knowledge VERSION 원자 공개
```

다운로드 앱에서는 agent gateway, app-owned read 경계, 파일 write, LLMWIKI build를 사용자
장치에서 실행한다. Gemini 모델 추론에는 Google API 호출과 사용자 API key가 필요하다. 원본
파일이 정본이고 LLMWIKI는 로컬 파생 지식 snapshot이다.

## 빠른 시작

Python 3.12와 [uv](https://docs.astral.sh/uv/)가 필요하다.

```bash
uv sync --all-groups
cp .env.example .env
uv run uvicorn codegate_api.main:app --reload
```

다른 터미널에서 anonymous public 검색을 확인한다.

```bash
curl http://127.0.0.1:8000/api/v1/health

curl -X POST http://127.0.0.1:8000/api/v1/chat/messages \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 00000000-0000-0000-0000-000000000001' \
  -d '{"conversation_id":"demo","message":"개인정보 보관 기간 문서 어디 있어?"}'
```

실제 변경 demo는 `.env.example`의 개발용 bearer token으로 `http://127.0.0.1:8000/docs`에서 실행한다. 변경 요청은 preview만 만들며, `plan_hash`를 approve API에 다시 보내야 파일이 바뀐다. approve와 Undo는 `202 Accepted`와 `Location`을 반환하므로 execution URL을 polling한다.

## 구현된 API

- `GET /api/v1/health`
- `GET /api/v1/auth/me`
- `POST /api/v1/chat/messages`
- `GET /api/v1/documents/{document_id}`
- `POST /api/v1/documents/{document_id}/source-sync-plans`
- `POST /api/v1/change-plans/{change_plan_id}/approve`
- `POST /api/v1/change-plans/{change_plan_id}/reject`
- `GET /api/v1/executions/{execution_id}`
- `POST /api/v1/executions/{execution_id}/retry-sync`
- `POST /api/v1/executions/{execution_id}/undo`
- `POST /api/v1/demo/reset`, production 비활성

실행 응답은 `file_status`, `sync_status`, `stage`, `terminal`, `recommended_poll_after_ms`를 분리한다. `sync_retryable`은 자동 재시도가 없는 action-required terminal 상태다. 이전 knowledge version을 계속 제공하되 실패 batch를 retry 또는 Undo로 해소하기 전에는 새 source 승인을 막아 stale event가 뒤 요청을 오염시키지 않는다. 원본에 아무 효과도 남기지 않은 일시적 `file_failed`는 같은 approve 또는 Undo 요청을 다시 보내면 기존 execution·journal을 재사용해 안전하게 재개한다.

## 보안·일관성 경계

- invalid 또는 expired bearer token을 anonymous로 강등하지 않는다.
- Supabase JWT는 JWKS signature, issuer, audience, expiry와 hook-owned tenant claim을 검증한다. `kid`별 key cache, unknown-`kid` negative cache, 최소 refresh 간격, 짧은 fetch timeout과 인증 IP rate limit으로 JWKS refresh 폭주를 제한한다.
- ACL을 검색, direct get, relation 확장, preview, approve, Undo마다 적용한다.
- 모델에는 built-in `Edit`, `Write`, `Bash`, web tool을 주지 않는다.
- Gemini는 파일 도구를 전혀 받지 않으며 검색 근거를 설명하는 `assistant_text`만 반환한다. 대상
  ID, exact operation, 현재 source SHA는 Python이 ACL 적용 repository를 다시 읽어 결정한다.
- `CODEGATE_GEMINI_DATA_POLICY=development-free`에서는 public 근거만 Gemini에 보낸다. 내부·제한
  문서는 billed Gemini 프로젝트를 사용한다는 명시적 `paid-no-training` 설정 없이는 실패 폐쇄한다.
- 모든 Claude session은 checksum과 schema를 통과한 `AGENT_GUIDE.md` frontmatter로 시작한다.
- Prompt v2는 사용자·문서·tool content를 untrusted data로 분리하고 매 turn 현재 graph version의
  fresh read를 요구한다. 구조화 `outcome`과 source hash를 Python validator와 preview 경계에서 다시
  검증한다.
- Prompt, model, SDK, tool schema, graph version, authorization fingerprint가 같고 TTL 안인 session만
  resume한다.
- `llm-wiki`에는 원본 symlink 대신 checksummed `references/*.source.json`을 둔다.
- 근거 quote는 선언된 `section_id`의 canonical section 안에 정확히 한 번 있어야 한다.
- 계획의 exact operation·diff·base/result hash·expiry를 canonical hash로 고정하고 owner는 tenant·subject scoped persistence로 별도 결박한다.
- 승인 뒤에도 ACL과 현재 source hash를 다시 검사한다.
- 파일은 dirfd와 `O_NOFOLLOW`, immutable backup, journal, fsync, same-directory `os.replace`로 교체한다. 원본 무변경 상태에서 쓰기가 실패하면 같은 감사 execution을 다시 준비해 재시도한다.
- 실제 쓰기는 UTF-8 Markdown/TXT만 지원한다. PDF, DOCX, HWP는 검증된 round-trip writer 전까지 read-only다.

## 팀 통합

통합본의 `codegate-2026-convert/services/doc2md` v0.2 `POST /v2/convert`를 내부 adapter로 호출한다. stable document ID와 `source://` URI override를 강제하고 source/canonical SHA, converter provenance, stable section mapping과 구조화 diagnostic을 검증한다. Backend 재발행 경로는 doc2md 청킹을 끄고, 같은 filesystem namespace에서는 `source.kind=path`, 별도 service에서는 최대 32 MiB의 `source.kind=bytes`와 Bearer 인증을 사용한다. 함께 고정된 LLMWIKI의 `WikiBuildService`를 in-process로 호출하며 native tenant/wiki `current.json`을 유일한 활성 포인터로 유지한다. Backend용 artifact는 native build ID에 결박된 checksum 포함 read cache다. 정확한 upstream repo SHA는 통합 루트 `UPSTREAM_SOURCES.md`를 따른다.

로컬 변경은 원본 파일과 정규화 Markdown의 본문·source SHA·revision을 갱신한 뒤 새 native build를 공개한다. `server-v2`의 단일 `sections` fragment만 원자 수정 대상으로 허용하고, 여러 `source-fragments`를 합친 문서는 전체 fragment snapshot과 원자 rollback 계약이 생길 때까지 검색 전용이다. 최신 converter·Backend·LLMWIKI 실제 process로 승인 변경, 실패 뒤 재시도, 새 근거 검색과 Undo까지 smoke 검증했다. 실행과 디렉터리 계약은 [로컬 runtime](docs/LOCAL_RUNTIME.md), 경계 결정은 [ADR 0004](docs/adr/0004-local-agent-llmwiki-and-auth-boundaries.md)와 [ADR 0005](docs/adr/0005-llmwiki-server-v2-write-compatibility.md)에 있다.

native build를 stage하기 직전에 LLMWIKI의 선택적 Gemini enrichment를 실행한다. 입력·모델·프롬프트
fingerprint가 같은 문서는 캐시되고, stale 문서만 다시 처리된다. 키가 설정된 provider 호출이나
데이터 정책 검사가 실패하면 stage와 activation을 진행하지 않는다.

로컬 watcher가 외부 편집을 감지하면 현재 파일 본문, 활성 source hash, 단일 exact operation을 `source-sync-plans`에 보낸다. Backend는 파일을 바로 덮어쓰지 않고 idempotent diff 계획을 반환한다. 클라이언트가 그 `plan_hash`를 표준 approve API에 다시 제출한 뒤에만 안전한 원본 교체와 동일 outbox fan-out이 시작된다. 협조하지 않는 외부 writer와의 완전한 POSIX CAS는 보장할 수 없으므로 실제 원본 writer는 이 프로토콜과 문서 lock을 함께 사용해야 한다.

팀원이 꼭 맞춰야 하는 필드와 이벤트는 [통합 계약](docs/INTEGRATION_CONTRACT.md), 바로 보낼 내용은 [팀 handoff](docs/TEAM_HANDOFF.md), 성주 역할은 [역할 및 로드맵](docs/ROLE_AND_ROADMAP.md)에 있다.

## Gemini 로컬 gateway

Gemini 2.5 Flash-Lite를 로컬 기본 gateway로 사용한다. API key는 query string이 아니라
`x-goog-api-key` header로만 전송한다.

```text
CODEGATE_AGENT_MODE=gemini
CODEGATE_GEMINI_MODEL=gemini-2.5-flash-lite
CODEGATE_GEMINI_TIMEOUT_SECONDS=45
GEMINI_API_KEY=<user-api-key>
```

무료/development 정책에서는 public 문서만 전송된다. 내부 문서를 사용하는 경우 API key가 실제
billed Gemini 프로젝트에 속하는지 사용자가 확인한 뒤에만 다음을 설정한다.

```text
CODEGATE_GEMINI_DATA_POLICY=paid-no-training
```

Gemini는 locate 응답 문구만 구조화 JSON으로 생성한다. 변경 요청은 모델로 보내지 않고 기존 quoted
`replace_exact` parser, 선택/명시된 문서 ID, ACL, 현재 원본의 유일한 일치, trusted SHA를 모두
검증한 뒤에만 preview 단계로 넘긴다.

## Claude와 production

실제 Claude Agent SDK session은 다음 설정으로 활성화한다.

```text
CODEGATE_AGENT_MODE=claude
CODEGATE_CLAUDE_MODEL=<exact-model-id>
CODEGATE_CLAUDE_SESSION_TTL_SECONDS=3600
ANTHROPIC_API_KEY=...
```

production은 Supabase Auth, network-backed agent mode, doc2md URL, 비활성 demo bootstrap,
Railway persistent volume이 없으면 설정 단계에서 기동을 거부한다. 적용 절차와 아직 사람이 해야 하는
작업은 [배포 runbook](docs/DEPLOYMENT.md)을 따른다.

## 로그인

Frontend가 Supabase Auth에 직접 로그인하고 access token만 cloud Backend 요청의 Bearer header로 보낸다. Backend는 password, refresh token, service-role key를 받지 않으며 asymmetric JWKS로 token을 검증한다. UI는 `GET /api/v1/auth/me`에서 tenant, read scope, write scope, provisioning 상태를 확인한다. 응답에는 token이 없고 `Cache-Control: private, no-store`가 적용된다.

다운로드 앱의 로컬 sidecar 권한은 Supabase 신원과 별개다. sidecar는 loopback과 명시적 CORS origin만 허용하고 사용자가 선택한 source root에만 접근한다. Frontend는 cloud Bearer client와 Bearer를 보내지 않는 local sidecar client를 분리해야 한다.

## 다운로드 앱 로컬 실행

앱과 함께 검증된 LLMWIKI bundle, 사용자가 허용한 source root, 별도 정규화 Markdown root와 storage root를 넘긴다.

```bash
export GEMINI_API_KEY=<user-api-key>

uv run codegate-local \
  --source-root /workspace/source \
  --llmwiki-project-root /app/LLMWIKI \
  --llmwiki-input-root /workspace/source-md \
  --llmwiki-storage-root /app-data/llmwiki \
  --data-root /app-data/codegate \
  --agent-mode gemini \
  --cors-origin http://127.0.0.1:3000
```

source, LLMWIKI input, storage root는 서로 겹치면 startup 전에 거부한다. 제품 기본은 Gemini
mode이며 `--agent-mode claude`도 지원한다. `--deterministic-agent`는 `--agent-mode deterministic`
호환 alias이며 오프라인 상태 머신 개발용이다.

## 검증

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
```

Prompt corpus와 grader 구성을 API 호출 없이 확인한다.

```bash
uv run python scripts/evaluate_agent_prompt.py --list
```

실제 model canary는 demo 문서와 read-only 도구만 사용하지만 Anthropic API 비용이 발생한다. 모델을
고정하고 필요한 case만 실행한다.

```bash
uv run python scripts/evaluate_agent_prompt.py \
  --model <exact-model-id> \
  --case exact-change \
  --case direct-injection-with-safe-task
```

Docker 확인:

```bash
docker build -t codegate-backend .
docker run --rm -p 8000:8000 codegate-backend
```

개발 도구는 저장소 루트의 `AGENTS.md`를 먼저 읽는다. Claude Code는 `CLAUDE.md`가 같은 파일을
import한다. 서비스의 Claude Agent SDK는 개발 도구 지침이 아니라 versioned
`llm-wiki/AGENT_GUIDE.md`의 제한된 frontmatter와 [prompt contract ADR](docs/adr/0006-claude-prompt-contract-and-evaluation.md)만 검증해 사용한다.
