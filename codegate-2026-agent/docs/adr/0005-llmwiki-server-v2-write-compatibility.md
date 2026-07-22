# ADR 0005: LLMWIKI server-v2 쓰기는 단일 sections fragment로 제한

- 상태: 채택
- 날짜: 2026-07-22

## 배경

`LLMWIKI@acf94f7`의 native build는 `server-v2`이며 한 논리 문서가 여러
`source-md/*.md` fragment로 구성될 수 있다. Native `ingest.aggregate_sha256`은 fragment path를
포함하지 않으며, 기존 Backend 상태와 backup은 문서당 원본·정규화 입력 한 파일만 추적한다.
Aggregate만 비교해 여러 fragment를 수정하면 승인 대상 경로가 바뀌거나 부분 write 뒤 Undo가 전체
문서를 복구하지 못할 수 있다.

## 결정

Backend는 native ingest를 mode와 정렬된 `(chunk_no, path, sha256)` descriptor로 정규화한다.
`chunking_mode=sections`, fragment 1개, `chunk_no=null`, aggregate와 fragment SHA가 같은 경우만
승인 변경과 Undo를 허용한다. Candidate에는 active와 같은 input path와 Backend가 기록한 승인-derived
fragment SHA를 요구한다. 관련 없는 문서는 aggregate뿐 아니라 descriptor 전체가 같아야 한다.

`source-fragments` 문서는 읽기·검색 package에는 포함하지만 `write_access=none`과
`editability=read_only`로 노출한다. 향후 쓰기 지원은 fragment별 before/after bytes, path, 생성·삭제
목록과 aggregate를 한 durable transaction에 기록하고 전체를 원자 복구한 뒤에만 연다.

doc2md 변환은 청킹을 끈 `POST /v2/convert`를 사용한다. 변환 body에는 정확히 하나의 선행 H1을
요구하고, 그 text를 active manifest title로 정규화한 뒤 LLMWIKI builder가 최종 section과 chunk를
다시 계산한다.

## 결과

- 현재 단일 Markdown/TXT 승인·retry·Undo는 기존 한 파일 journal과 일치한다.
- 다중 fragment 문서는 검색 가능하지만 편집 UI와 직접 queued event 모두 실패 폐쇄한다.
- Fragment path만 바꾸거나 unrelated input을 섞은 candidate는 활성화되지 않는다.
- Compatibility cache suffix를 올려 이전 ingest 해석 결과를 재사용하지 않는다.

## 검토한 대안

- `aggregate_sha256`만 승인 증거로 사용: path와 topology를 결박하지 않아 제외했다.
- 첫 fragment만 수정하고 나머지는 유지: 논리 문서의 부분 상태와 Undo 손실을 만들 수 있어 제외했다.
- 최신 팀 기능을 쓰기 위해 즉시 다중 fragment journal 구현: 현재 제품 쓰기 범위를 넘어 복잡도와
  crash surface가 커지므로 실제 다중 fragment 편집 요구가 생길 때로 미뤘다.
- `server-v2` 검사를 제거하거나 이전 LLMWIKI commit으로 고정: 미래 비호환 build를 허용하거나 현재
  팀 계약을 잃으므로 제외했다.

## 검증 조건

- 실제 `server-v2` 단일 fragment build에서 승인 변경, publish, 검색과 Undo 성공
- 잘못된 aggregate, mixed v1/v2 ingest, input path 변경과 unrelated input candidate 거부
- 다중 `source-fragments` materialize는 read-only이며 직접 sync event도 current를 바꾸지 않음
- doc2md H1 title 불일치는 안정된 title로 정규화하고 H1 누락·중복은 거부
