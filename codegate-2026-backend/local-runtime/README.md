# LLM Wiki Local Runtime

사용자 PC에서 원본 문서 변경 이벤트를 받아
[`codegate-2026-convert`](https://github.com/dotenv-uploaded/codegate-2026-convert)로
Markdown을 만들고 기존 `WikiBuildService`를 실행하는 로컬 오케스트레이터다.

원본과 현재 위키 사이의 경계는 다음과 같다.

```text
프론트 Watcher
  -> POST /api/v1/source-events
  -> SQLite 이벤트/작업 기록
  -> SHA-256 원본 캐시
  -> doc2md /convert/async
  -> 후보 Markdown 입력 스냅샷
  -> WikiBuildService
  -> 빌드 활성화 성공 시에만 입력 스냅샷 승격
```

원본 파일과 활성 Markdown 입력은 직접 수정하지 않는다. 변환이나 enrichment, 검증 또는
빌드 활성화가 실패하면 기존 `current.json`과 활성 입력 스냅샷을 유지한다.

## 실행 준비

Python 3.12와 `uv`가 필요하다. 먼저 변환 API를 로컬 전용 주소에서 실행한다.

```bash
cd /path/to/codegate-2026-convert/services/doc2md
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8123
```

그다음 런타임을 실행한다.

```bash
cd local-runtime
cp .env.example .env
# .env에서 원본 루트, 위키 설정과 API 토큰을 지정한다.
uv sync --extra dev
uv run llm-wiki-local
```

기본 주소는 `http://127.0.0.1:8765`다. `LLMWIKI_API_HOST`는 loopback 주소만 허용한다.
브라우저 프론트가 다른 포트에서 실행되면 그 정확한 Origin을
`LLMWIKI_ALLOWED_ORIGINS`에 추가한다. `*` 와일드카드는 허용하지 않는다.

## 이벤트 API

세 이벤트 모두 같은 엔드포인트를 사용한다.

```text
POST /api/v1/source-events
GET  /api/v1/jobs/{job_id}
GET  /api/v1/sources
GET  /health
```

`LLMWIKI_API_TOKEN`을 설정했다면 다음 중 하나를 보낸다.

```http
Authorization: Bearer <token>
X-LLMWiki-Token: <token>
```

### 생성

```json
{
  "event_id": "event-0001",
  "event_type": "created",
  "source_id": "source-0001",
  "sequence": 1,
  "relative_path": "regulations/security.hwpx",
  "source_path": "/absolute/path/security.hwpx",
  "source_sha256": null,
  "metadata": {
    "id": "REG-000001",
    "title": "정보보안 규정",
    "doc_type": "regulation",
    "revision": "1",
    "access": "internal"
  }
}
```

`metadata.id`를 생략하면 `source_id`로부터 결정적인 ID를 생성한다. 생성된 ID는 이후 수정에도
유지한다.

### 수정

```json
{
  "event_id": "event-0002",
  "event_type": "updated",
  "source_id": "source-0001",
  "sequence": 2,
  "relative_path": "regulations/security.hwpx",
  "source_path": "/absolute/path/security.hwpx",
  "base_source_sha256": "현재 활성 원본 SHA-256",
  "source_sha256": "새 원본 SHA-256",
  "metadata": {
    "revision": "2"
  }
}
```

수정은 registry의 기존 `doc_id`를 변환 API `metadata.id`로 강제한다. 변환 API의 SHA 기반
자동 ID가 수정 때 새 문서를 만드는 문제를 방지한다.

### 삭제

```json
{
  "event_id": "event-0003",
  "event_type": "deleted",
  "source_id": "source-0001",
  "sequence": 3,
  "relative_path": "regulations/security.hwpx",
  "base_source_sha256": "삭제 직전 활성 원본 SHA-256"
}
```

삭제는 변환 API를 호출하지 않는다. 기존 문서의 모든 Markdown 조각을 후보 입력에서 제거하고
전체 위키를 빌드한다. 성공 후 source registry에는 tombstone을 남기며 원본 캐시와 이전 불변
빌드는 보존한다.

접수 성공은 처리 완료가 아니라 내구성 있는 작업 등록을 의미한다.

```json
{
  "accepted": true,
  "duplicate": false,
  "event_id": "event-0001",
  "job_id": "...",
  "status": "queued"
}
```

응답 코드는 `202 Accepted`다. 같은 `event_id`를 다시 보내면 기존 `job_id`를 반환한다.

## 저장 구조

`LLMWIKI_STATE_DIR` 아래에는 다음 데이터가 생긴다.

```text
local-data/
├── state.sqlite3
├── source-cache/{sha256}/{filename}
└── input-snapshots/
    ├── .candidate-{job_id}/
    └── snapshot-{build_id}/
```

변환 결과의 청크 파일명과 `chunk_no`는 원본 파일명이 아니라 문서 ID 기준으로 정규화한다.
doc2md가 추가하는 `canonical_sha256`, `converter_version` 같은 변환기 전용 provenance는
위키 빌더의 명시적 front matter 허용 목록에 없으므로 후보 Markdown을 쓸 때 제거한다.

```text
REG-000001_001.md
REG-000001_002.md
```

따라서 서로 다른 원본 폴더에 같은 파일명이 있어도 위키 입력 파일이 충돌하지 않는다.

이미 `current.json`이 있는 위키에 처음 연결할 때는 그 빌드의 정본 Markdown 세트를
`LLMWIKI_BOOTSTRAP_INPUT_DIR`로 지정해야 한다. 활성 위키는 있는데 bootstrap Markdown이
비어 있으면 첫 이벤트가 기존 문서를 모두 제거하지 않도록 작업을 거부한다. 새 위키는 빈
bootstrap 디렉터리로 시작할 수 있다.

## 제약

- 현재 이벤트 본문은 로컬 절대 `source_path`를 사용한다. 브라우저 File System Access API만
  사용하는 프론트는 절대 경로를 제공할 수 없으므로 추후 multipart 업로드 어댑터가 필요하다.
- 변환 API의 작업 상태는 메모리 기반이지만, 이 런타임의 SQLite 작업이 정본이다. 재시작 시
  `queued` 또는 `running` 작업을 다시 제출한다.
- 원본 경로는 가능하면 `LLMWIKI_ALLOWED_SOURCE_ROOTS`로 제한한다.
- `LLMWIKI_MAX_SOURCE_BYTES`보다 큰 원본은 캐시하거나 변환하지 않는다.
- 생성된 `local-data/`, `wiki-storage/`, `source-md/`와 위키 산출물은 프론트 Watcher의 감시
  대상에서 제외한다.

## 검증

```bash
uv run ruff check src tests
uv run pytest
```
