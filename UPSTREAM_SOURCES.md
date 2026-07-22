# Historical upstream source record

이 문서는 2026-07-22 통합 당시 provenance만 보존한다. 현재 개발·릴리스의 유일한 source of
truth는 `dotenv-uploaded/codegate-2026-folding-song`의 `main`이며, 실행 버전과 컴포넌트 경계는
[`component-manifest.json`](component-manifest.json)을 기계적으로 검증한다. 아래 standalone
저장소에는 새 변경을 반영하지 않으며, archive 전환은 저장소 소유자의 최종 확인 뒤 별도로 수행한다.

| 디렉터리 | 원본 저장소 | 기준 브랜치 | 기준 커밋 |
|---|---|---|---|
| `codegate-2026-local` | `dotenv-uploaded/codegate-2026-local` | `integration/local-sidecar-runtime` (draft PR #2) | `8b13434a8f440167bb46e0347a0bf54c9b138f77` |
| `codegate-2026-agent` | `dotenv-uploaded/codegate-2026-agent` | `agent/claude-answer-stability` (draft PR #11) | `aeb86c7bb810144a6b0857dc7d3643f5580171a0` |
| `codegate-2026-convert` | `dotenv-uploaded/codegate-2026-convert` | `main` | `beb1ca8e70a2e777fce5191b503d21f27d62b939` |
| `codegate-2026-backend` | `dotenv-uploaded/codegate-2026-backend` | `main` | `497d7fce557d2250a7957650b024a1379a5b035e` |

이 표의 SHA는 현재 기준선이 아니라 과거 유입 지점을 뜻한다.
