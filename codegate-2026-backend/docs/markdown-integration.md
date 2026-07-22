# Markdown 변환·정규화 연동 가이드

## 담당 범위

변환 서비스는 원본 파일을 보관하고, LLM 위키 빌더가 읽을 수 있는 정규화 Markdown을
생성한다. 빌더는 PDF/HWPX/DOCX 변환을 수행하지 않는다.

정식 계약은 다음 두 파일이다.

- [`wiki-builder/INPUT_CONTRACT.md`](../wiki-builder/INPUT_CONTRACT.md)
- [`source-document.schema.json`](../wiki-builder/schemas/source-document.schema.json)

## 전달 방식 선택

변환 결과가 문서 전체라면 파일명과 `id`가 같은 Markdown 하나를 전달한다. 이 방식은
`chunk_no` 없이 H1 하나와 H2 하나 이상을 요구하며, 긴 H2는 빌더가 재청킹할 수 있다.

변환 단계에서 문서를 의미 단위로 이미 나눴다면 다음 규칙을 사용한다.

- 같은 문서 조각은 같은 `id`를 사용한다.
- 조각마다 고유한 `chunk_no`를 넣고 파일명을 `{chunk_no}.md`로 만든다.
- 같은 `id` 안에서는 `chunk_no`를 제외한 front matter를 동일하게 유지한다.
- H1/H2는 넣지 않아도 된다. 입력 파일 하나가 검색 청크 하나가 된다.
- 조각 순서는 `chunk_no`의 자연 정렬 순서로 결정된다.

두 방식 모두 UTF-8/LF, 원문 의미 보존, 원본 SHA-256, 접근 등급과 같은 위키 내 고유한
논리 문서 ID가 필요하다. 내부 링크는 대상 논리 문서 ID의 `.md` 링크를 사용한다.

## 전달 순서

1. 원본을 테넌트 전용 저장소에 기록한다.
2. SHA-256과 출처 URI를 계산한다.
3. 임시 작업 디렉터리에서 Markdown을 생성한다.
4. JSON Schema와 선택한 입력 모드의 구조 조건을 검증한다.
5. 완성된 입력 디렉터리를 원자적으로 게시한다.
6. 변경된 논리 `doc_id` 목록과 현재 wiki 식별자를 빌드 작업 큐에 전달한다.

의미 청크 하나만 바뀌어도 해당 조각 파일을 교체한 뒤 논리 문서 `id` 하나를 재빌드 대상으로
전달한다. 빌더는 조각 전체의 해시를 다시 계산해 기존 enrichment를 자동으로 stale 처리한다.

빌더 검증은 다음 명령으로 먼저 실행할 수 있다.

```bash
cd wiki-builder
uv run python -m wiki_builder \
  --input-dir /absolute/path/to/source-md \
  --tenant-id tenant-a \
  --wiki-id wiki-a \
  --production \
  normalize
```

성공 결과에서 `documents`는 논리 문서 수, `source_files`는 실제 Markdown 입력 수,
`source_fragment_documents`는 의미 청크 모드 문서 수다. 예를 들어 같은 `id`의 파일 11개는
`documents=1`, `source_files=11`, `sections=11`이 된다.

`normalize` 성공은 입력 계약 통과를 의미하지만 원본 파일과 변환문의 의미적 동일성을 대신
보증하지 않는다. 변환 품질 검사는 변환 서비스에서 별도로 수행해야 한다.

## 변경과 삭제

- 본문 개정 시 같은 `id`를 유지하고 `revision`을 증가시킨다.
- 의미 청크 모드에서는 같은 개정의 모든 조각이 동일한 front matter를 가져야 한다.
- 상태만 바뀌어도 같은 `id`의 조각 전체에 상태를 동일하게 반영한다.
- 조각 추가·삭제·교체는 논리 문서의 집계 해시를 바꾸고 enrichment를 무효화한다.
- 문서 삭제는 해당 `id`의 모든 조각을 입력 세트에서 제거하고 새 build를 만든다.
- 규제나 감사 요구가 있는 경우 물리적 삭제는 별도 보존 정책을 따른다.

## 로컬 변환 API 연동

사용자 PC에서 자동 갱신할 때는 저장소의 `local-runtime`이 프론트 변경 이벤트와 변환 API
사이를 중계한다. 변환기는
[`codegate-2026-convert`](https://github.com/dotenv-uploaded/codegate-2026-convert)의
`POST /convert/async`를 사용한다.

- `created`: 새 `doc_id`를 정한 뒤 변환 결과를 후보 입력에 추가한다.
- `updated`: registry의 기존 `doc_id`를 `metadata.id`로 전달하고 해당 문서 조각 전체를
  교체한다.
- `deleted`: 변환기를 호출하지 않고 해당 문서 조각 전체를 후보 입력에서 제거한다.
- 변환 청크의 `chunk_no`와 파일명은 `{doc_id}_{ordinal}.md`로 정규화한다.
- 후보 입력이 빌드·검증·활성화에 모두 성공한 경우에만 새 활성 입력으로 승격한다.

상세 실행 방법과 이벤트 본문은 [`local-runtime/README.md`](../local-runtime/README.md)를
따른다.
