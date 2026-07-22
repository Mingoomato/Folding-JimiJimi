# 기여 가이드

## 브랜치와 PR

- `main`에 직접 기능 변경을 푸시하지 않는다.
- 브랜치는 `feature/<description>`, `fix/<description>`, `docs/<description>` 형식을 사용한다.
- PR에는 변경 목적, 영향받는 계약, 검증 명령과 호환성 영향을 기록한다.

## 계약 변경

다음 파일 변경은 Markdown 변환, 위키 빌더와 Agent 담당자 모두의 검토가 필요하다.

- `wiki-builder/schemas/`
- `wiki-builder/INPUT_CONTRACT.md`
- `docs/agent-integration.md`
- manifest, chunk, link, enrichment의 필드 의미
- `current.json`과 build 활성화 규칙

호환되지 않는 변경은 schema version과 마이그레이션 계획을 함께 제출한다.

## 로컬 검증

```bash
cd wiki-builder
uv sync --extra dev
uv run ruff check src tests
uv run pytest

cd ../local-runtime
uv sync --extra dev
uv run ruff check src tests
uv run pytest
```

테스트에서는 실제 API 키와 외부 LLM 호출을 사용하지 않는다. 새 모델 공급자 통합은 mock 기반
단위 테스트와 별도의 수동 smoke test 절차를 함께 제공한다. 문서 변환 API 연동도 CI에서는
mock HTTP 계약으로 검증하고 실제 `codegate-2026-convert` smoke test는 로컬에서 수행한다.

## 데이터와 비밀정보

- 실제 고객·사내 문서를 fixture로 추가하지 않는다.
- `.env`, API 키, 생성된 위키 저장소를 커밋하지 않는다.
- 샘플 데이터는 합성 데이터임을 명시한다.
