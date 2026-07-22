# 위키 빌더 입력 계약

위키 빌더는 파일 업로드나 PDF/HWPX 변환을 담당하지 않는다. 상위 변환 서비스는 문서를
UTF-8 Markdown으로 변환한 뒤 아래 두 모드 중 하나로 전달한다. 한 `id` 안에서 두 모드를
섞을 수 없다.

## 공통 조건

- 모든 파일은 `schemas/source-document.schema.json`을 통과해야 한다.
- UTF-8, LF 줄바꿈을 사용하며 표, 목록, 코드블록, 각주, 숫자의 의미를 보존한다.
- `id`는 논리 문서 식별자이며 같은 위키 안에서 문서 단위로 고유하다.
- `source.sha256`은 변환 전 원본 파일의 SHA-256이다.
- 원본 해시를 실제로 확인했다면 `source.verification: verified`, 확인할 수 없으면
  `unavailable`을 기록한다.
- `authority_level`, `issuing_org`, 날짜와 공식 문서번호는 알 수 없을 때 `null`일 수 있다.
- 입력 파일은 일반 파일이어야 하며 심볼릭 링크는 허용하지 않는다.
- 빌더는 입력 파일을 수정하지 않는다.

## 모드 A: 문서 파일

기존 문서 하나를 Markdown 하나로 전달할 때 사용한다.

- `chunk_no`를 쓰지 않는다.
- 파일명은 front matter의 `id`와 같아야 한다. 예: `DOC-01AB23.md`.
- H1은 정확히 하나이며 `title`과 같아야 한다.
- 검색과 인용을 위한 비어 있지 않은 H2가 하나 이상 있어야 한다.
- 긴 H2는 `chunking.max_chars`와 `overlap_chars`에 따라 빌더가 여러 검색 청크로 나눌 수
  있다.

## 모드 B: 의미 청크 파일

변환 단계에서 같은 문서를 의미 단위별 Markdown으로 이미 나눈 경우 사용한다.

- 같은 논리 문서의 모든 파일은 같은 `id`를 사용한다.
- 각 파일은 고유한 `chunk_no`를 사용하며 파일명은 `{chunk_no}.md`와 같아야 한다.
- `chunk_no`는 영문자·숫자로 시작하고 영문자, 숫자, `.`, `_`, `-`만 사용한다.
- 같은 `id`의 파일은 `chunk_no`를 제외한 front matter가 정확히 같아야 한다. 여기에는
  `revision`, 상태, 접근 등급, 출처와 원본 SHA-256도 포함된다.
- H1과 H2는 필수가 아니다. H1을 넣는다면 자연 정렬상 첫 청크에만 하나를 넣을 수 있고
  `title`과 같아야 한다. 이후 청크에는 H1을 넣지 않는다.
- 파일 하나의 본문 전체가 검색 청크 하나다. 빌더는 `chunking.max_chars`를 적용해 다시
  나누지 않는다.
- 의미 청크 본문은 `input_limits.max_semantic_chunk_chars` 이하여야 한다.
- 빌더는 `chunk_no`를 자연 정렬한다. 예를 들어 `part-2`는 `part-10`보다 먼저 온다.

같은 원본 안에서 의미만 다른 부분은 같은 `id`와 서로 다른 `chunk_no`를 쓴다. 서로 독립된
문서라면 `id`도 다르게 부여해야 한다.

## 의미 청크 빌드 결과

같은 `id`의 입력 파일 N개는 다음과 같이 변환된다.

- `docs/{doc_type-directory}/{id}.md`: 논리 문서 1개. 빌더가 H1, 조각별 H2, 안정적인
  `section_id` 앵커와 `source-chunk` 경계를 생성한다.
- `manifest.jsonl`: 문서 1건. `chunking_mode=source-fragments`,
  `source_fragment_count=N`, `ingest.fragments`에 조각별 번호·경로·해시를 기록한다.
- `retrieval/chunks.jsonl`: 입력 파일마다 `chunk_type=source-fragment`인 청크 1건.
  `source_chunk_no`, `source_input_path`, `source_input_sha256`로 원본 조각을 추적한다.
- enrichment: 논리 문서 `id` 단위로 생성하며, 근거의 `section_id`는 해당
  `source_chunk_no`와 연결된다.

출력 Markdown의 H1/H2와 경계 표시는 탐색을 위한 파생 구조다. 입력 조각의 본문은 별도
재작성이나 요약 없이 그대로 청크 본문으로 사용한다.

## Front Matter 예시

```yaml
---
schema_version: "1.0.0"
id: REG-000030
title: 서식 1
doc_type: regulation
language: ko
revision: "1"
status: active
chunk_no: test_8_pdf_001
official_number: null
authority_level: null
issuing_org: null
issued_on: null
effective_from: null
effective_to: null
source:
  filename: original.pdf
  uri: source://regulations/original.pdf
  sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
access: internal
tags: []
aliases: []
---
```

## 처리 한도

기본 한도는 다음과 같다.

- 논리 문서 10,000개
- Markdown 입력 파일 50,000개
- 파일당 10 MiB
- 위키당 Markdown 총 1 GiB
- 문서당 H2 또는 의미 청크 500개
- 의미 청크당 12,000자

운영 환경의 작업 큐와 메모리 용량에 맞춰 `config/wiki.yaml`의 `input_limits`를 더 낮출 수
있다.

지원 문서 유형은 규정, 정책, 절차, 매뉴얼, 가이드, 명세, 계약, 보고서, 회의록 및 일반
문서다. 언어는 BCP 47 형식으로 기록한다. 예: `ko`, `en`, `ko-KR`.

위키 빌더는 정규화된 복사본과 파생 검색 데이터만 새 불변 빌드에 기록한다. 원본 파일과
입력 Markdown의 보존·삭제 정책은 상위 저장 서비스가 관리한다.
