# 팀에 지금 보낼 내용

상세 내부 설계 대신 아래 담당자별 문장과 OpenAPI만 공유한다.

## 전원

> Backend는 `https://github.com/dotenv-uploaded/Backend`에서만 작업합니다. 검색 결과는 `document_id + revision + graph_version + chunk_id + section_id` 근거를 반환하고, 파일 변경은 exact diff와 `plan_hash` 승인 뒤에만 실행합니다. 원본은 symlink가 아니라 `source://` URI와 SHA-256으로 참조합니다. 승인 뒤 파일 적용과 지식 동기화는 분리되며, 변환·FTS·vector·graph가 모두 검증된 새 VERSION만 공개합니다.

## 우창

> 로그인은 Supabase SDK에서 직접 처리하고 password·refresh token을 Backend로 보내지 마세요. cloud API에는 access token을 Bearer로 붙인 뒤 `GET /api/v1/auth/me`의 `provisioned`, `write_scope`, `writable_document_ids`를 화면 권한의 source of truth로 사용해 주세요. Supabase session client와 local sidecar client를 분리하고 sidecar에는 Bearer를 붙이지 않습니다. `POST /api/v1/chat/messages`에서 `location_result`, `target_selection`, `change_preview`, `error`를 처리해 주세요. `change_preview`의 diff를 보여주고 화면에서 받은 `plan_hash` 그대로 approve 또는 reject에 보내야 합니다. 로컬 watcher 업로드도 `POST /documents/{id}/source-sync-plans`에서 먼저 plan을 받고 같은 승인 흐름을 거칩니다. approve·reject·Undo에는 요청마다 8자 이상의 `Idempotency-Key`가 필수입니다. approve와 Undo는 `202 + Location`이므로 `GET /api/v1/executions/{id}`를 `recommended_poll_after_ms` 간격으로 polling해 주세요. `sync_retryable`은 polling 종료 후 retry 또는 Undo를 선택하는 action-required 상태입니다. 원본이 바뀌지 않은 일시적 `file_failed` 오류는 같은 approve/Undo endpoint를 새 key로 다시 호출하면 같은 execution으로 재개합니다. 브라우저에는 Anthropic key, source 절대경로, Claude session state를 두지 않습니다.

## 용휘

> `LLMWIKI@acf94f7`의 `WikiBuildService`, native `current.json`, `server-v2` manifest/chunks/links/aliases schema를 Backend에 연결했습니다. stable ID는 LLMWIKI regex와 최대 96자를 그대로 받습니다. Backend compatibility cache는 native build ID에만 결박하고 별도 current를 만들지 않습니다. 변경 event는 normalized Markdown의 source hash와 revision을 갱신한 뒤 expected current build ID CAS로 새 native build를 공개합니다. 현재 `sections` 단일 fragment만 수정 가능하고 `source-fragments` 문서는 검색 전용입니다. 여러 fragment의 원자 snapshot·rollback 계약을 추가하기 전에는 이 제한을 유지해 주세요. schema나 update entry point를 바꿀 때 build format/version과 migration contract를 같이 알려주세요.

## 민규

> `codegate-2026-convert/services/doc2md@30e6814`의 v0.2 `POST /v2/convert` adapter를 연결했습니다. Backend는 청킹을 끄고 stable ID·source URI metadata override, source/canonical SHA, converter version, stable section key, source page/span과 구조화 diagnostic을 검증해 LLMWIKI normalized input을 만듭니다. 원본 H1이 metadata title과 달라도 정확히 하나의 선행 H1이면 활성 manifest title로 정규화하며, 누락·중복 H1은 거부합니다. 같은 filesystem에서는 `source.kind=path`, 별도 service에서는 최대 32 MiB의 `source.kind=bytes`를 사용하며 production 호출은 Bearer 인증을 필수로 합니다. LLMWIKI local-runtime의 `/convert/async` 최초 수집 경로와 Backend outbox writer는 desktop에서 직렬화해 주세요. 응답 schema나 stable section 의미를 바꿀 때 version과 migration contract를 함께 알려주세요. HWP/PDF/DOCX는 현재 capability대로 parse와 원본 round-trip write를 분리하고, 검증된 writer가 없으므로 Backend는 read-only로 유지합니다.

## 보내지 않을 것

- 개인 로컬 경로와 `.private` 문서
- SQLite table과 atomic write 내부 구현
- 실제 key, token, Supabase service-role key
- 검증되지 않은 HWP/PDF write 지원 주장

실제 request/response schema는 실행 중인 `/openapi.json`을 source of truth로 사용한다.
