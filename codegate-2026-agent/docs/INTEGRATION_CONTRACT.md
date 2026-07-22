# 팀 통합 계약 v0.4

이 문서는 각 담당자가 내부 구현을 추측하지 않고 병렬 개발하기 위한 최소 wire·artifact 계약이다. 실행 중인 Backend의 `/openapi.json`과 이 문서가 다르면 OpenAPI를 우선하고 계약 version을 함께 올린다.

## 담당 경계

| 담당 | 소유 | Backend 경계 |
| --- | --- | --- |
| 우창 | Next.js UI | chat, 근거, diff, approve/reject, execution polling, retry, Undo |
| 성주 | FastAPI·Claude Agent SDK·side effect | 인증·ACL, agent, 승인, 실제 file write, 상태·outbox·version publish |
| 용휘 | `llm-wiki`·검색·지식 그래프 | immutable manifest/chunks/links/enrichment와 index build 결과 |
| 민규 | 문서 변환 | source→canonical Markdown/assets/section mapping과 구조화 오류 |

다른 담당자의 저장소는 직접 수정하지 않는다. Backend는 adapter와 계약 테스트로만 연결한다.

## 공통 ID와 불변 조건

- `document_id`: `^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$`, 최대 96자인 stable ID다. 내용·경로·revision이 바뀌어도 유지한다.
- `file_version_id`: 원본 bytes가 바뀔 때마다 새 값이다.
- `graph_version`: 검증된 knowledge snapshot이 공개될 때마다 새 값이다.
- `event_id`: outbox와 모든 downstream consumer의 idempotency key다.
- 원본은 package에 복사·symlink하지 않고 `source://` URI, SHA-256, checksummed source reference로 연결한다.
- ACL은 query와 relation 확장 전에 적용하고 권한 없는 문서 ID도 model context에 넣지 않는다.
- 본문 metadata나 prompt는 access를 올릴 수 없다.
- enrichment는 후보 확장용이다. canonical section에 연결된 exact quote가 아니면 사실 근거가 아니다.
- 승인 전 source write는 0건이다. approve는 화면에 보인 exact `plan_hash`만 받는다.
- 승인 직전 ACL과 source base SHA-256을 다시 확인한다.
- 실제 write는 UTF-8 Markdown/TXT만 지원한다. PDF, DOCX, HWP는 verified round-trip writer 전까지 read-only다.
- 변환 뒤 FTS, vector, graph를 병렬 갱신하고 모두 검증된 immutable VERSION만 공개한다.
- request는 시작 시 하나의 snapshot을 pin한다. 한 응답에서 version을 섞지 않는다.

## `llm-wiki` acceptance 계약

실제 연동 기준은 `LLMWIKI@acf94f7`이다. native package의
`tenants/<tenant>/wikis/<wiki>/current.json`이 유일한 active pointer이며
`builds/build-<20 hex>`는 불변이다. Backend는 native manifest/chunks/links/aliases와 schema를
검증한 뒤 build ID별 compatibility read cache를 만들지만 두 번째 active pointer를 만들지 않는다.

현재 native `build_format_version`은 `server-v2`다. 쓰기 판단에는 aggregate hash만 사용하지 않고
`chunking_mode`, fragment 수, 각 `chunk_no`·path·SHA를 함께 검증한다.

```json
{
  "source_fragment_count": 1,
  "chunking_mode": "sections",
  "ingest": {
    "aggregate_sha256": "...",
    "fragments": [
      {"chunk_no": null, "path": "source-md/REG-000001.md", "sha256": "..."}
    ]
  }
}
```

Backend가 수정할 수 있는 형태는 `sections`, fragment 1개, `chunk_no=null`,
`aggregate_sha256 == fragment.sha256`인 단일 Markdown뿐이다. `source-fragments` 문서는 native
검색 결과에는 포함하지만 `write_access=none`, `editability=read_only`로 내린다. 여러 입력 파일의
before/after bytes와 생성·삭제 목록을 한 journal로 snapshot하고 전부 원자 복구하는 계약이 없기
때문이다. 이 제한을 풀 때는 fragment-set 상태 모델과 crash/Undo 회귀 테스트를 먼저 추가한다.

필수 native build 파일:

```text
.generated-by-wiki-builder
AGENT_GUIDE.md
README.md
build-meta.json
manifest.jsonl
docs/**/*.md
indexes/index-meta.json
retrieval/aliases.json
retrieval/chunks.jsonl
retrieval/links.jsonl
schemas/*.schema.json
```

Backend가 build ID에 결박해 생성하는 compatibility read cache에는 `VERSION`,
`checksums.sha256`, `references/<document_id>.source.json`과 Backend schema가 추가된다. Native
`current.json`만 활성 포인터이며 cache에는 별도 current를 만들지 않는다.

`enrichments.jsonl`은 현재 MVP acceptance의 선택 산출물이며 Backend 검색 adapter는 아직 로드하지 않는다. 용휘가 schema와 실제 후보 확장 adapter를 제공하면 `section_id`와 canonical exact quote를 검증한 항목만 검색 후보 확장에 사용한다. 그 전에는 enrichment를 답변 근거나 production graph readiness로 주장하지 않는다.

`AGENT_GUIDE.md`는 checksum에 포함하며 다음 citation을 모두 요구한다.

```toml
required_citations = ["document_id", "revision", "graph_version", "chunk_id", "section_id"]
```

Backend는 허용된 TOML frontmatter만 schema로 검증해 모든 Claude session의 고정 지침으로 바꾼다. 자유 형식 body는 model prompt에 넣지 않는다.

Manifest 최소 필드:

```json
{
  "schema_version": "1.0.0",
  "id": "REG-000001",
  "file_version_id": "fv_...",
  "title": "개인정보 처리 규정",
  "doc_type": "regulation",
  "revision": "3",
  "status": "active",
  "authority_level": "regulation",
  "effective_from": "2026-07-01",
  "effective_to": null,
  "source": {
    "filename": "regulations/REG-000001.md",
    "uri": "source://regulations/REG-000001.md",
    "media_type": "text/markdown",
    "sha256": "..."
  },
  "canonical_path": "docs/regulations/REG-000001.md",
  "access": "internal",
  "write_access": "restricted",
  "editability": "editable",
  "tags": [],
  "aliases": [],
  "conversion": {}
}
```

Chunk 최소 필드:

```json
{
  "schema_version": "1.0.0",
  "chunk_id": "REG-000001@3#sec-004",
  "document_id": "REG-000001",
  "file_version_id": "fv_...",
  "section_id": "sec-004",
  "section": "제4조 보관 기간",
  "heading_path": ["개인정보 처리 규정", "제4조 보관 기간"],
  "text": "인용 가능한 원문",
  "embedding_text": "검색 전용 보강 텍스트와 인용 가능한 원문",
  "text_sha256": "...",
  "ordinal": 0
}
```

`chunk_id`는 revision-bound다. section의 첫 chunk(`ordinal=0`)는
`<document_id>@<revision>#<section_id>`, 추가 chunk는 뒤에 `:<ordinal>`을 붙인다. revision이
바뀌면 해당 문서의 모든 chunk ID와 `links.evidence_chunk_ids`도 같은 candidate 안에서 함께
갱신한다.

Canonical Markdown은 stable anchor를 정확히 한 번 포함한다.

```markdown
<a id="sec-004"></a>
## 제4조 보관 기간
```

Backend load barrier는 anchor, 전체 heading path, file version, text hash를 확인하고, `text`가 선언한 section body에 정확히 한 번 있을 때만 evidence로 노출한다. `embedding_text`는 검색에만 사용하고 quote로 반환하지 않는다.

Link endpoint와 evidence chunk는 같은 package에 존재해야 한다. `VERIFIED` relation만 사용자 응답과 agent context에 사용한다.

## API 계약

Base path는 `/api/v1`이다.

- `GET /health`
- `GET /auth/me`
- `POST /chat/messages`
- `GET /documents/{document_id}`
- `POST /documents/{document_id}/source-sync-plans`
- `POST /change-plans/{change_plan_id}/approve`
- `POST /change-plans/{change_plan_id}/reject`
- `GET /executions/{execution_id}`
- `POST /executions/{execution_id}/retry-sync`
- `POST /executions/{execution_id}/undo`
- `POST /demo/reset`, non-production only

현재 Chat `response_type`은 `location_result`, `target_selection`, `change_preview`, `error` 중 하나다. 실행 상태는 chat 응답이 아니라 execution API로 조회한다. 위치 응답의 사실 문장은 canonical quote 그대로이며 각 quote와 구조화 evidence에 `[document_id rev.N §section_id]` citation을 붙인다.

Approve와 Undo는 `202 Accepted`, `Location: /api/v1/executions/{id}`를 반환한다. Frontend는 `recommended_poll_after_ms`를 사용해 `terminal`까지 polling한다.

Execution의 핵심 필드:

```json
{
  "status": "file_applied",
  "file_status": "applied",
  "sync_status": "pending",
  "stage": "sync_queued",
  "terminal": false,
  "recommended_poll_after_ms": 500,
  "graph_version_before": "...",
  "graph_version_after": null,
  "error": null
}
```

동기화 실패는 `stage=sync_retryable`, `terminal=true`, `recommended_poll_after_ms=null`, `error.retryable=true`인 action-required 상태다. 이전 knowledge version은 계속 active다. 같은 candidate batch에서 실패한 event들은 함께 retry하며, retry 또는 Undo로 해소하기 전에는 새 source 승인을 `KNOWLEDGE_SYNC_BLOCKED`로 막는다.

원본 교체 전에 storage 오류가 나고 source가 정확히 before hash로 남으면 execution은 `stage=file_failed`가 된다. 이때 `retry-sync`가 아니라 원래 approve 또는 Undo endpoint를 다시 호출한다. 같은 key와 새 key 모두 기존 execution·journal에 결박되며 새 승인·새 Undo row를 만들지 않는다. source가 before/after 어느 쪽도 아니면 conflict로 닫는다.

`GET /health`는 `pending_sync_events`, `failed_sync_events`, `stale_sync_events`, `worker_available`, `index_artifacts_available`을 반환한다. 짧은 정상 pending은 readiness를 낮추지 않지만 설정된 시간보다 오래된 pending/processing은 degraded다. immutable release의 index 검증 결과는 load 시 한 번 계산하고 probe에서는 재사용한다.

### 외부 파일 watcher

watchdog/File System Access client는 변경된 전체 UTF-8 본문, active source의 `base_sha256`, 그 둘을 정확히 연결하는 단일 `replace_exact` operation을 `source-sync-plans`에 보낸다. 응답은 파일 적용 결과가 아니라 `ChangePlanView`이며 source write는 0건이다. client는 diff를 확인하고 같은 `plan_hash`를 표준 approve API에 제출해야 한다. 같은 `Idempotency-Key`는 같은 plan으로 replay되고 다른 본문에는 409를 반환한다. 현재 adapter가 canonical evidence에 안전하게 표현할 수 없는 변경과 PDF/DOCX/HWP upload는 실패 폐쇄한다.

모든 원본 writer는 이 API를 사용해야 한다. Backend는 rename 직전 source hash를 다시 확인해 비협조 외부 edit의 대표 race를 막지만, POSIX에는 임의 외부 writer와의 원자 compare-and-swap이 없으므로 API 밖 직접 덮어쓰기는 지원 계약이 아니다.

### 인증 경계

Supabase JWT는 허용된 asymmetric algorithm과 bounded `kid`를 먼저 확인한 뒤 signature, issuer, audience, expiry와 필수 claim을 검증한다. known key는 제한 시간 cache하고 unknown `kid`는 negative cache하며 JWKS refresh에는 process-wide 최소 간격과 fetch timeout을 둔다. 모든 Authorization 요청에는 header 크기 제한과 pre-auth IP rate limit이 적용된다. Railway 앞단에서도 플랫폼 gateway/WAF rate limit을 유지해야 한다.

Frontend는 Supabase SDK로 직접 로그인한다. password와 refresh token은 Backend API로 보내지
않는다. cloud API request에 access token을 Bearer로 보내고 `GET /auth/me`에서 다음 권한
snapshot을 확인한다.

```json
{
  "authenticated": true,
  "subject_id": "uuid",
  "tenant_id": "tenant-a",
  "email": "user@example.com",
  "read_access": ["public", "internal"],
  "write_scope": "documents",
  "writable_document_ids": ["REG-000001"],
  "provisioned": true,
  "authz_source": "supabase-hook"
}
```

응답은 `Cache-Control: private, no-store`이며 token을 반환하지 않는다. Access Token Hook은
access row가 있을 때만 `codegate_provisioned=true`를 넣는다. Backend는 이 claim이 true가
아니면 signed `codegate_write_document_ids`가 있어도 쓰기를 거부한다.

로컬 sidecar는 별도 filesystem capability다. loopback에서만 받고 Bearer token을 받지 않으며
`authz_source=local-workspace`, `write_scope=workspace`를 반환한다. Frontend는 Supabase Bearer가
있는 cloud client와 Bearer가 없는 local client를 분리한다. 로그인 성공이 로컬 폴더 권한을
부여하지 않는다.

### 로컬 LLMWIKI 동기화

원본 file write 성공 뒤 같은 outbox event가 다음 순서를 지킨다.

1. doc2md가 설정됐으면 현재 source hash로 normalized Markdown을 다시 만든다. 없으면 지원되는
   Markdown/TXT exact operation을 기존 normalized body에 멱등 적용한다.
2. doc2md body의 정확히 하나인 선행 H1을 활성 manifest title로 정규화하고, normalized
   frontmatter의 `source.sha256`와 revision을 함께 갱신한다.
3. `WikiBuildService.build(..., activate_incomplete=true)`를 expected current build ID CAS로 호출한다.
4. native current가 가리키는 schema와 build metadata, source hash와 canonical evidence를 검증한다.
5. native build ID에 결박된 compatibility cache를 만들고 execution을 published로 완료한다.

부분 실패 때 native current는 바뀌지 않는다. publish 뒤 DB 완료 전에 process가 중단된 경우
current의 final source hash를 확인해 같은 event를 중복 build 없이 완료한다. source, normalized
input, native storage root는 서로 겹칠 수 없다.

## 문서 변환 계약

현재 연동 대상은 `codegate-2026-convert/services/doc2md@30e6814`의 v0.2
`POST /v2/convert`다. 이 저장소는 `codegate-2026-api`에서 이름이 바뀌었다. ADR 0004와 이전
WORK_LOG의 `codegate-2026-api@360ff41` 표기는 당시 기록으로 남기고, 현재 integration pin은
여기를 따른다. Backend는 metadata로 stable ID, source URI, doc type, access를 강제하고 요청 직전과
응답 후 source SHA를 대조한다. 응답은 `extra="forbid"`로 검증해 계약 밖 최상위 필드를 거부한다.

Backend 요청은 `chunking.enabled=false`로 고정한다. LLMWIKI `local-runtime`이 최초 수집과 외부
watcher에 사용하는 `POST /convert/async`는 별도 orchestration 경로이며 Backend outbox consumer가
호출하지 않는다. 같은 normalized input에 두 writer가 동시에 접근하지 않도록 desktop 계층이
직렬화해야 한다.

doc2md의 v1 `POST /convert` 7키 응답은 호환성을 위해 동결되어 있지만 Backend는 더 이상 이를
사용하지 않는다. 현재 adapter는 v0.2가 제공하는 다음 항목을 필수로 검증한다.

| 항목 | v0.2 제공 형태 |
|---|---|
| 변경 뒤에도 stable section anchor와 heading hierarchy 보존 | 정확히 하나의 H1, 비어있지 않은 H2 1개 이상, 레벨 점프 없음을 결정론적으로 보장. `sections[].stable_key`(heading path 해시)는 문서의 다른 곳이 바뀌어도 불변 |
| canonical Markdown, source/canonical SHA, converter/version | `source_sha256`, `canonical_sha256`, `converter{name,version,library,ocr_device}`. frontmatter(`schema_version` 1.1.0)에도 `canonical_sha256`·`converter_version` 포함 |
| section 또는 source page/span mapping | `sections[].char_start`/`char_end`는 전 형식. `source_page`는 PDF(본문 `<!--- page N --->` 마커)와 pptx(슬라이드 번호). hwp/hwpx는 형식상 페이지 개념이 없어 null |
| warning과 retryable 여부가 있는 구조화 오류 | `diagnostics[]{code,message,severity,retryable,detail}` + 오류 body `{code,message,retryable,detail}` |
| 형식별 parse와 original write-back capability 분리 | `GET /capabilities`. **write_back은 전 형식 false** — 검증된 writer가 없으므로 read-only로 유지한다 |

`sections[].ordinal`과 `anchor_hint`는 문서 중간에 절이 추가되면 이동한다. revision 간 같은 section을 매칭할 때는 반드시 `stable_key`를 쓴다.

doc2md가 metadata title과 다른 원본 H1을 보존할 수 있으므로 Backend는 응답에 정확히 하나의 선행
H1이 있는지 확인한 뒤 그 H1 text만 활성 LLMWIKI manifest title로 바꾼다. H1 누락·중복은 거부한다.
LLMWIKI builder가 최종 section anchor와 chunk를 다시 계산하므로 doc2md span을 수정 결과의 정본으로
재사용하지 않는다.

`expected_source_sha256`을 요청에 넣으면 doc2md가 변환을 시작하기 전에 원본 hash를 대조해 불일치 시 `409 SOURCE_HASH_MISMATCH`로 거부한다. Backend의 응답측 stale cache 검증과 별개의 방어선이다.

원격 doc2md의 무인증 path API를 public network에 노출하지 않는다. v0.2는
`DOC2MD_API_TOKEN` Bearer 인증과 `DOC2MD_ALLOWED_ROOTS` 경로 제한을 제공하며 Backend production
설정은 `CODEGATE_DOC2MD_API_TOKEN`이 없으면 기동을 거부한다.

`CODEGATE_DOC2MD_SOURCE_KIND=path`는 같은 filesystem namespace에서만 사용한다. 별도 Railway
service에는 `bytes`를 사용해 승인된 원본을 base64로 전송하며, Backend가 전송 직전 hash와
`CODEGATE_DOC2MD_MAX_SOURCE_BYTES` 상한을 검사한다. doc2md v0.2 자체는 object-storage `uri`도
지원하지만 현재 Backend adapter의 허용 transport는 `path|bytes`다. `http(s)`는 SSRF 통로가
되므로 양쪽 모두 사용하지 않는다.

## 지식 그래프 update 계약

Backend outbox payload:

```json
{
  "schema_version": "1.0.0",
  "event_id": "evt_...",
  "execution_id": "exec_...",
  "document_id": "REG-000001",
  "source_uri": "source://regulations/REG-000001.md",
  "before_sha256": "...",
  "after_sha256": "...",
  "file_version_id": "fv_...",
  "operation": {
    "type": "replace_exact",
    "expected_text": "1년",
    "replacement_text": "3년",
    "expected_occurrences": 1
  }
}
```

저장 envelope은 `{id, event_type, aggregate_id, payload, status, attempts}`이고
`id == payload.event_id`, `event_type == DocumentContentChanged`,
`aggregate_id == payload.execution_id`여야 한다. adapter entry point는 이 envelope을 받아
`event_id`를 consumer 멱등 key로 사용한다. 불일치한 ID는 build 전에 거부한다.

Consumer 규칙:

1. `event_id` 기준으로 중복 처리한다.
2. converter 결과 뒤 FTS, vector, graph를 병렬 build한다.
3. candidate에 parent VERSION과 포함 event IDs를 기록한다.
4. schema, source reference, canonical evidence, link, checksum을 모두 검증한다.
5. expected parent가 현재 active일 때만 publish한다.
6. CAS loser는 최신 active 위로 미반영 event를 rebase/coalesce한다.
7. 일부 stage 실패 시 active pointer를 바꾸지 않는다.

## 팀이 제공할 최소 fixture

- 용휘: 실제 manifest 1줄, chunk 2줄, link 1줄, aliases와 schema, update entry point, expected query 2개
- 민규: source 1개, canonical 1개, section mapping 1개, 성공/실패 응답, format capability 표
- 우창: API base URL, CORS origin, diff viewer, polling UI, retry·Undo UX

팀 fixture가 준비되기 전에는 이 저장소의 합성 문서와 deterministic adapter로 전체 상태 머신을 검증한다.
