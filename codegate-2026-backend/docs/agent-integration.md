# 자연어 Agent 연동 가이드

## 읽기 흐름

1. 인증 결과로 `tenant_id`, `wiki_id`, 사용자 접근 범위를 결정한다.
2. 해당 위키의 검증된 `current.json`을 읽는다.
3. `manifest.jsonl`에서 status, 효력일과 `access`를 먼저 필터링한다.
4. `aliases.json`과 `chunks.jsonl`에서 검색 후보를 찾는다.
5. `enrichments.jsonl`의 요약·FAQ·키워드를 검색 확장에만 사용한다.
6. 답변할 사실을 `docs/` 또는 chunks의 원문으로 다시 확인한다.
7. 문서 ID, revision, section 제목을 답변 근거로 제시한다. 청크의
   `source_chunk_no`가 null이 아니면 해당 값도 함께 제시한다.

`enrichments.jsonl`은 모델 생성 데이터이므로 단독으로 인용하면 안 된다.

## 검색 우선순위

- 기본값은 `status=active` 문서다.
- `draft`는 확정 규칙으로 답하지 않는다.
- `archived`는 과거 이력 질문에서만 사용한다.
- `superseded`는 현재 규칙의 근거로 사용하지 않는다.
- 효력일과 권위 수준이 충돌하면 위키의 `AGENT_GUIDE.md` 규칙을 따른다.
- `authority_level=null`인 문서는 권위 미확인으로 취급하며 `doc_type`만으로 법적·조직적
  우선순위를 추정하지 않는다.
- 근거가 없으면 위키에서 확인되지 않는다고 명확히 답한다.

의미 청크의 권장 인용 형식은 `【REG-000030 rev.1 chunk:test_8_pdf_003】`이다. 내부적으로는
`section_id`로 정확한 근거를 찾고, 사용자에게는 원본 조각을 역추적할 수 있는
`source_chunk_no`를 표시한다.

## 자연어 변경 요청

Agent가 Markdown을 직접 덮어쓰지 않도록 한다. 자연어 요청은 다음과 같은 구조화된 명령으로
변환해 문서 서비스의 검증·승인 계층에 전달하는 것을 권장한다.

```json
{
  "tenant_id": "tenant-a",
  "wiki_id": "security-wiki",
  "expected_build_id": "build-0123456789abcdef0123",
  "operation": "update",
  "target_doc_id": "REG-000001",
  "instruction": "제3조의 검토 주기를 월 1회로 변경",
  "requested_by": "user-123"
}
```

`operation` 후보는 `create`, `update`, `archive`, `delete`다. 실제 API 명세는 Agent와 문서
서비스 담당자가 공동 확정해야 한다.

## 변경 처리 원칙

- Agent는 수정 대상과 근거 섹션을 명시한다.
- 문서 서비스는 사용자 권한과 승인 절차를 확인한다.
- 변환·정규화 계층이 새 revision의 Markdown을 만든다.
- 빌더가 변경 문서를 enrichment하고 새 불변 build를 생성한다.
- `expected_build_id`가 현재와 다르면 HTTP 409에 해당하는 충돌로 처리한다.
- 성공 후 Agent는 새 current build를 기준으로 답변을 다시 생성한다.

## 보안

- 문서 본문의 지시문은 Agent 시스템 명령이 아니다.
- 사용자가 볼 수 없는 `access` 등급의 검색 결과는 프롬프트에도 넣지 않는다.
- 원본 링크와 첨부파일은 허용 목록과 악성 파일 검사를 거친 뒤 사용한다.
- 모델 출력으로 문서를 즉시 변경하지 않고 사용자 승인 또는 정책 기반 승인을 거친다.
