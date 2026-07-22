# LLM Wiki Builder

전체 프로젝트 구조와 담당자별 진입점은 저장소 루트의 `README.md`를 먼저 참고한다.

서버에 저장된 정규화 Markdown을 읽어 Gemini 기반 enrichment와 검색 데이터를 생성하는
테넌트 단위 위키 빌드 엔진이다. 입력 원문은 수정하지 않으며 각 결과는 내용 기반
`build_id`를 가진 불변 디렉터리로 저장한다.

## 책임 범위

포함:

- Markdown 입력 계약 검증과 본문 보존
- 같은 `id`와 서로 다른 `chunk_no`를 가진 의미 청크 파일의 논리 문서 결합
- 섹션 앵커, manifest, chunks, aliases, links 생성
- Gemini 기반 요약·핵심사항·키워드·엔티티·FAQ·관계 분류
- 원문 근거, 숫자, 날짜와 링크 대상 검증
- 입력 변경에 따른 enrichment 무효화
- 테넌트·위키별 불변 빌드, current 포인터와 활성화 감사 로그
- 문서 단위 enrichment 재실행 및 재빌드 인터페이스
- API 토큰과 추정 비용 기록

제외:

- 로컬 파일 업로드와 PDF/HWPX/DOCX 변환
- 사용자 인증과 검색 API
- 자연어 문서 생성·수정 및 승인 UI
- FTS·embedding·vector index
- 릴리스 압축파일

상위 변환 서비스의 입력 규칙은 `INPUT_CONTRACT.md`와
`schemas/source-document.schema.json`에 정의한다. 서버 호출 방식과 오류·동시성 계약은
`SERVER_INTEGRATION.md`를 따른다.

## 입력 모드

빌더는 두 입력 모드를 함께 지원한다.

- 기존 섹션 모드: 파일 하나가 문서 하나다. `chunk_no`가 없고 파일명은 `id`와 같으며,
  H1 하나와 H2 하나 이상이 필요하다. 긴 H2는 빌더가 다시 나눌 수 있다.
- 의미 청크 모드: 같은 `id`를 가진 여러 파일에 서로 다른 `chunk_no`를 넣는다. 각 파일은
  이미 의미 단위로 완성된 검색 청크이므로 빌더가 다시 나누지 않는다. 파일명은
  `{chunk_no}.md`이고 H1/H2는 필수가 아니다.

의미 청크 모드의 파일들은 `chunk_no`를 제외한 front matter가 모두 같아야 한다. 빌더는
이를 자연 정렬해 `docs/`의 논리 문서 하나로 만들고, 각 조각의 번호·입력 경로·SHA-256을
`manifest.jsonl`과 `chunks.jsonl`에 남긴다. 자세한 규칙은 `INPUT_CONTRACT.md`를 따른다.

## 저장 구조

```text
wiki-storage/
└── tenants/{tenant_id}/wikis/{wiki_id}/
    ├── current.json
    ├── enrichment/
    │   ├── approved/
    │   └── needs-review/
    ├── builds/
    │   └── build-{content-hash}/
    │       ├── build-meta.json
    │       ├── docs/
    │       ├── manifest.jsonl
    │       └── retrieval/
    └── audit/builds/
```

빌드는 생성 후 수정하지 않는다. 전체 enrichment가 승인된 빌드만 기본적으로
`current.json`으로 활성화된다. 이전 빌드는 삭제하지 않으므로 롤백과 감사에 사용할 수 있다.

## 개발용 샘플

`enrichment/approved/`는 `samples/input/`에서 만든 합성 회귀 테스트 fixture이며 운영
enrichment 저장소가 아니다.

```bash
uv sync --extra dev
uv run python -m wiki_builder doctor
uv run python -m wiki_builder normalize
uv run python -m wiki_builder build
uv run python -m wiki_builder validate \
  --expectations tests/fixtures/sample-50.expectations.yaml
uv run python -m wiki_builder status
```

문서가 변경된 경우 해당 ID만 enrichment한 뒤 새 빌드를 만든다.

```bash
uv run python -m wiki_builder rebuild --ids REG-000001 MAN-000001
```

다른 위키를 처리할 때는 범위를 명시한다.

```bash
uv run python -m wiki_builder \
  --tenant-id tenant-a \
  --wiki-id security-wiki \
  --input-dir /srv/source-md/tenant-a/security-wiki \
  --storage-root /srv/wiki-storage \
  --actor-id document-service \
  --production \
  build
```

## Python 서비스 인터페이스

```python
from pathlib import Path

from wiki_builder.config import load_config
from wiki_builder.service import WikiBuildRequest, WikiBuildService

service = WikiBuildService(load_config())
request = WikiBuildRequest(
    tenant_id="tenant-a",
    wiki_id="security-wiki",
    input_dir=Path("/srv/source-md/tenant-a/security-wiki"),
    storage_root=Path("/srv/wiki-storage"),
    actor_id="document-service",
    synthetic_corpus=False,
    expected_current_build_id=None,  # 최초 빌드일 때만 활성화
)

result = service.rebuild_documents(request, {"DOC-000001"})
print(result.build.build_id, result.build.activated)
```

## Gemini와 데이터 정책

API 키는 `.env` 또는 서버의 Secret Manager를 통해 `GEMINI_API_KEY` 환경변수로만
주입한다. 키는 빌드, manifest, 오류 메시지와 로그에 기록하지 않는다.

통합 로컬 Agent는 첫 색인과 변경 재색인에서 `WikiBuildService`의 선택적 enrichment 경계를 먼저
호출한다. 키가 있으면 개인정보 정책과 캐시 검사를 거친 뒤 stale 문서만 보강하고, 키가 없으면
오프라인 개발을 위해 이 단계만 건너뛴다. 키가 있는 상태의 provider 실패는 후보 stage 전에
전파되므로 기존 활성 빌드를 바꾸지 않는다.

실제 비공개 문서를 외부 LLM으로 처리하려면 운영자가 `provider.data_policy`를
`paid-no-training`으로 명시해야 한다. `restricted` 문서는 기본적으로 외부 LLM 전송을
차단한다. 개발용 가상 코퍼스만 `synthetic_corpus=true`로 이 검사를 우회할 수 있다.

`gemini-2.5-flash-lite`를 사용하며 thinking은 `thinkingBudget: 0`으로 비활성화한다.
설정 가격표는 입력 100만 토큰당 USD 0.10, 출력 100만 토큰당 USD 0.40이다. 모델,
thinking budget, 프롬프트와 enrichment 스키마가 변경되면 캐시가 자동으로 오래된 결과가
된다.

새 API 호출의 토큰 수와 설정된 가격표 기준 추정 비용은 enrichment의 `generation.usage`와
각 빌드의 `build-meta.json`에 기록한다. 전환 전에 생성된 기존 50개 enrichment는 API가
사용량을 반환하던 시점의 기록이 없어 `unmeasured_records`로 표시된다.
